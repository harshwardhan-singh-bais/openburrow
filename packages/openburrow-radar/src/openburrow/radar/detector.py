"""The Radar loop: keep intents current, scan pairs, announce what is new.

Two behaviours here are worth more than the rest of the file.

**Announcements are deduplicated, and re-armed by new information.** Without
deduplication, a scan every few seconds would re-report the same collision until
somebody muted the Radar. With naive deduplication, a lane that changes what it
is working on would never have its conflicts re-examined — so the dedupe key
includes the file set, and updating a lane's intent clears that lane's entries.
The rule is: *the same two lanes in the same conflict are announced once; the
same two lanes in a different conflict are announced again.*

**Coverage is reported, not assumed.** :attr:`RadarStats.judge_coverage` is the
fraction of judge consultations that produced a usable verdict. If the judge is
misconfigured, rate-limited, or down, that number collapses — and the honest
consequence is that the Radar's semantic signal did not exist for that period. A
report that showed "0 semantic conflicts found" without showing "the judge was
unreachable for 100% of calls" would be actively misleading, and it is exactly
the kind of number that ends up in a slide deck.

The deterministic signal has no such caveat, which is why the Radar is useful
even with no model configured at all.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from itertools import combinations

from openburrow.core.logging import get_logger
from openburrow.radar.conflicts import ConflictPrediction, ConflictPredictor
from openburrow.radar.intent import Intent
from openburrow.radar.judge import ConflictJudge

log = get_logger(__name__)

#: How many scans a prediction stays suppressed before it may be re-announced
#: even if nothing changed. A conflict that nobody acted on is still a conflict,
#: and a reminder every few minutes is the difference between "the Radar noticed"
#: and "the Radar was ignored".
REANNOUNCE_AFTER_SCANS = 20


@dataclass
class RadarStats:
    """Honest accounting of what the Radar actually did."""

    scans: int = 0
    pairs_evaluated: int = 0
    structural_hits: int = 0
    scope_hits: int = 0
    semantic_hits: int = 0
    judge_calls: int = 0
    judge_unknown: int = 0
    suppressed: int = 0

    @property
    def judge_coverage(self) -> float:
        """Fraction of judge consultations that produced a usable verdict.

        ``1.0`` when no judge is configured would be a lie, so an unused judge
        reports ``0.0`` and :attr:`semantic_available` says whether that matters.
        """
        if self.judge_calls == 0:
            return 0.0
        return (self.judge_calls - self.judge_unknown) / self.judge_calls

    @property
    def semantic_available(self) -> bool:
        return self.judge_calls > 0 and self.judge_coverage > 0.5

    @property
    def conflicts(self) -> int:
        return self.structural_hits + self.scope_hits + self.semantic_hits

    def render(self) -> str:
        lines = [
            f"scans: {self.scans}  pairs: {self.pairs_evaluated}  "
            f"conflicts: {self.conflicts} (suppressed repeats: {self.suppressed})",
            f"  certain: {self.structural_hits}  scope: {self.scope_hits}  semantic: {self.semantic_hits}",
        ]
        if self.judge_calls == 0:
            lines.append("  judge: not used (deterministic signals only)")
        else:
            verdict = "usable" if self.semantic_available else "mostly unavailable"
            lines.append(
                f"  judge: {self.judge_calls} calls, {self.judge_unknown} unusable "
                f"({self.judge_coverage:.0%} coverage — {verdict})"
            )
        return "\n".join(lines)

    def summary(self) -> dict[str, object]:
        return {
            "scans": self.scans,
            "pairs_evaluated": self.pairs_evaluated,
            "conflicts": self.conflicts,
            "certain_conflicts": self.structural_hits,
            "scope_conflicts": self.scope_hits,
            "semantic_conflicts": self.semantic_hits,
            "suppressed": self.suppressed,
            "judge_calls": self.judge_calls,
            "judge_unknown": self.judge_unknown,
            "judge_coverage": round(self.judge_coverage, 4),
            "semantic_available": self.semantic_available,
        }


@dataclass
class _Announcement:
    """Bookkeeping for one previously-reported conflict."""

    at_scan: int
    files: frozenset[str]
    signal: str


class Radar:
    """Watches lane intents and predicts collisions before they land."""

    def __init__(
        self,
        *,
        judge: ConflictJudge | None = None,
        predictor: ConflictPredictor | None = None,
        on_conflict: Callable[[ConflictPrediction], None] | None = None,
        reannounce_after: int = REANNOUNCE_AFTER_SCANS,
    ) -> None:
        self.predictor = predictor or ConflictPredictor(judge)
        self.judge = judge or self.predictor.judge
        self.on_conflict = on_conflict
        self.reannounce_after = reannounce_after
        self.stats = RadarStats()

        self._intents: dict[str, Intent] = {}
        self._announced: dict[tuple[tuple[str, str], str, frozenset[str]], _Announcement] = {}
        self._scan_count = 0

    # --- intents -----------------------------------------------------------
    def set_intent(self, intent: Intent) -> None:
        """Record or replace a lane's intent.

        Replacing an intent clears that lane's announcement memory, because a new
        intent is genuinely new information: the lane may have moved off the file
        it was colliding on, or onto a different one. Keeping the old
        announcements would mean the Radar stayed silent about a collision it had
        never actually reported.
        """
        previous = self._intents.get(intent.lane_id)
        if (
            previous is not None
            and previous.files == intent.files
            and previous.directories == intent.directories
        ):
            # Nothing structural changed. Keep announcements so we do not spam
            # the bus because a lane re-claimed the same file.
            self._intents[intent.lane_id] = intent
            return

        self._intents[intent.lane_id] = intent
        self._forget_lane(intent.lane_id)
        log.debug(
            "radar.intent.updated",
            lane_id=intent.lane_id,
            files=len(intent.files),
            directories=len(intent.directories),
        )

    def remove_lane(self, lane_id: str) -> None:
        self._intents.pop(lane_id, None)
        self._forget_lane(lane_id)

    def intents(self) -> list[Intent]:
        return list(self._intents.values())

    def intent_for(self, lane_id: str) -> Intent | None:
        return self._intents.get(lane_id)

    # --- scanning ----------------------------------------------------------
    async def scan(self) -> list[ConflictPrediction]:
        """Evaluate every pair. Returns only predictions worth announcing."""
        self._scan_count += 1
        self.stats.scans += 1
        lanes = sorted(self._intents)

        fresh: list[ConflictPrediction] = []
        for left, right in combinations(lanes, 2):
            self.stats.pairs_evaluated += 1
            prediction = await self.predictor.predict(self._intents[left], self._intents[right])
            if prediction is None:
                continue
            self._tally(prediction)
            if self._is_new(prediction):
                fresh.append(prediction)

        self.stats.judge_calls = self.predictor.judge_calls
        self.stats.judge_unknown = self.predictor.judge_unknown

        for prediction in fresh:
            if self.on_conflict is not None:
                self.on_conflict(prediction)
        if fresh:
            log.info(
                "radar.conflicts.announced",
                count=len(fresh),
                certain=sum(1 for p in fresh if p.certain),
            )
        return fresh

    def _tally(self, prediction: ConflictPrediction) -> None:
        if prediction.signal.value == "file":
            self.stats.structural_hits += 1
        elif prediction.signal.value == "scope":
            self.stats.scope_hits += 1
        else:
            self.stats.semantic_hits += 1

    # --- deduplication -----------------------------------------------------
    def _is_new(self, prediction: ConflictPrediction) -> bool:
        """Has this exact conflict been announced recently?"""
        key = (prediction.pair, prediction.signal.value, frozenset(prediction.files))
        previous = self._announced.get(key)
        if previous is not None and self._scan_count - previous.at_scan < self.reannounce_after:
            self.stats.suppressed += 1
            return False

        self._announced[key] = _Announcement(
            at_scan=self._scan_count,
            files=frozenset(prediction.files),
            signal=prediction.signal.value,
        )
        return True

    def _forget_lane(self, lane_id: str) -> None:
        for key in [k for k in self._announced if lane_id in k[0]]:
            del self._announced[key]

    # --- reporting ---------------------------------------------------------
    def report(self) -> dict[str, object]:
        """Snapshot for ``burrow report`` and the web dashboard."""
        return {
            "stats": self.stats.summary(),
            "lanes_tracked": len(self._intents),
            "announced_pairs": len(self._announced),
            "intents": [
                {
                    "lane_id": intent.lane_id,
                    "files": sorted(intent.files),
                    "directories": sorted(intent.directories),
                    "risk_tier": intent.risk_tier,
                    "source": intent.source,
                    "summary": intent.summary(),
                }
                for intent in sorted(self._intents.values(), key=lambda i: i.lane_id)
            ],
        }


def pairs(intents: Iterable[Intent]) -> list[tuple[Intent, Intent]]:
    """Every unordered pair of intents. Exposed for tests and dry runs."""
    return list(combinations(list(intents), 2))


__all__ = ["REANNOUNCE_AFTER_SCANS", "Radar", "RadarStats", "pairs"]
