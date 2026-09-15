"""Pairwise conflict prediction, with certainty kept separate from guesswork.

Every prediction this module produces carries a :attr:`ConflictPrediction.certain`
flag, and that flag is the most important field in the structure.

A conflict derived from two lanes claiming the same file is a **fact**. Two lanes
said it; the claims are in the database; anyone can go and look. A conflict
derived from a model reading two descriptions is a **prediction**. It might be
right, it might be wrong, and no amount of model confidence changes which of
those two things it is.

Systems that blur that line are the ones that get switched off. If the Radar
reports "possible conflict" with the same visual weight as "both lanes are
editing routes.py", the true positives stop being believed within a week. So the
severity ladder here is driven by *evidence*, not by a score:

``file``
    Shared concrete file. Certain. Reported regardless of any threshold —
    suppressing a known collision behind a confidence score is how you get a
    merge conflict you had already detected and chose not to mention.

``scope``
    A directory claim overlapping another lane's files. Probabilistic: lanes can
    legitimately share a directory. Escalated only when a judge agrees or the
    risk tier is high.

``semantic``
    No structural overlap, but a judge thinks one change invalidates the other —
    an interface change, duplicated work, a dependency inversion. Pure
    prediction, and always reported as such.

The judge is consulted only after a cheap prefilter, because the naive
implementation is quadratic in both latency and money: eight lanes is 28 pairs,
and 28 model calls per scan is a cost that grows with the square of the team.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePosixPath

from openburrow.core.logging import get_logger
from openburrow.radar.intent import CRITICAL, HIGH, Intent
from openburrow.radar.judge import ConflictJudge, JudgeVerdict

log = get_logger(__name__)

#: A judge verdict at or above this triggers a prediction. Deliberately above the
#: judge's own floor: the judge's floor says "the model asserted something", this
#: says "it asserted something strongly enough to interrupt two agents".
DEFAULT_SEMANTIC_THRESHOLD = 0.7

#: Scope overlaps are noisier than semantic ones, so they need more from the
#: judge before they are worth a negotiation.
DEFAULT_SCOPE_THRESHOLD = 0.8

#: How many files to carry as evidence. A prediction naming 400 files is not
#: evidence, it is a directory listing, and it makes the negotiation impossible
#: to reason about.
MAX_EVIDENCE_FILES = 25


class Signal(StrEnum):
    """What kind of evidence produced a prediction."""

    FILE = "file"
    SCOPE = "scope"
    SEMANTIC = "semantic"


class Severity(StrEnum):
    INFO = "info"
    WARN = "warn"
    BLOCK = "block"


@dataclass(frozen=True, slots=True)
class ConflictPrediction:
    """A predicted collision between two lanes."""

    session_id: str
    lane_a: str
    lane_b: str
    signal: Signal
    severity: Severity
    reason: str
    confidence: float
    #: True when this rests on records rather than on a model's reading.
    certain: bool
    files: tuple[str, ...] = ()
    recommended_action: str = ""
    judge: JudgeVerdict | None = None
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def pair(self) -> tuple[str, str]:
        """Canonical, order-independent pair key for deduplication."""
        return tuple(sorted((self.lane_a, self.lane_b)))  # type: ignore[return-value]

    @property
    def kind(self) -> str:
        """Short label used by the CLI and the metrics."""
        return f"{self.signal.value}:{'certain' if self.certain else 'predicted'}"

    def render(self) -> str:
        marker = "[bold red]conflict[/bold red]" if self.certain else "[yellow]possible[/yellow]"
        files = ", ".join(self.files[:3])
        suffix = f" ({files})" if files else ""
        return f"{marker} {self.lane_a[:12]} ↔ {self.lane_b[:12]}{suffix} — {self.reason}"


class ConflictPredictor:
    """Decides whether a pair of intents is worth a negotiation."""

    def __init__(
        self,
        judge: ConflictJudge | None = None,
        *,
        semantic_threshold: float = DEFAULT_SEMANTIC_THRESHOLD,
        scope_threshold: float = DEFAULT_SCOPE_THRESHOLD,
    ) -> None:
        self.judge = judge
        self.semantic_threshold = semantic_threshold
        self.scope_threshold = scope_threshold
        #: Counters kept here rather than in the detector so that the cost of the
        #: judge is attributable to the thing that spends it.
        self.judge_calls = 0
        self.judge_unknown = 0

    # --- entry point -------------------------------------------------------
    async def predict(self, a: Intent, b: Intent) -> ConflictPrediction | None:
        """Compare two intents. Returns ``None`` when there is nothing to say."""
        if a.lane_id == b.lane_id or a.is_empty or b.is_empty:
            return None

        # 1. Certain: the same concrete file.
        shared = a.shared_files(b)
        if shared:
            return self._structural(a, b, shared)

        # 2. Probabilistic: a directory claim covering the other lane's files.
        scope = a.shared_scope(b)
        if scope:
            return await self._scope(a, b, scope)

        # 3. Speculative: only if the pair looks related enough to be worth a call.
        if not self._worth_judging(a, b):
            return None
        return await self._semantic(a, b)

    # --- signals -----------------------------------------------------------
    def _structural(self, a: Intent, b: Intent, shared: frozenset[str]) -> ConflictPrediction:
        """Both lanes named the same file. This is not a prediction."""
        both_certain = a.is_certain and b.is_certain
        files = tuple(sorted(shared)[:MAX_EVIDENCE_FILES])
        severity = Severity.BLOCK if _is_high_risk(a, b) else Severity.WARN
        return ConflictPrediction(
            session_id=a.session_id or b.session_id,
            lane_a=a.lane_id,
            lane_b=b.lane_id,
            signal=Signal.FILE,
            severity=severity,
            reason=(
                f"both lanes claim {len(shared)} of the same file(s)"
                if len(shared) > 1
                else f"both lanes claim {files[0]}"
            ),
            confidence=1.0,
            certain=both_certain,
            files=files,
            recommended_action="settle who owns the file before either writes to it",
            metadata={"risk": _max_tier(a, b)},
        )

    async def _scope(
        self, a: Intent, b: Intent, scope: frozenset[str]
    ) -> ConflictPrediction | None:
        """A directory claim overlaps the other lane's files."""
        verdict = await self._consult(a, b)
        if verdict is not None and verdict.known and verdict.conflict:
            if verdict.confidence >= self.scope_threshold:
                return self._from_judge(a, b, Signal.SCOPE, scope, verdict)
            return None

        # No judge, or the judge could not tell. A high-risk scope overlap is
        # still worth raising, because the cost of being wrong is asymmetric:
        # a spurious negotiation wastes minutes, a missed one can waste a day.
        if _is_high_risk(a, b):
            return ConflictPrediction(
                session_id=a.session_id or b.session_id,
                lane_a=a.lane_id,
                lane_b=b.lane_id,
                signal=Signal.SCOPE,
                severity=Severity.WARN,
                reason=(
                    f"directory claim overlaps {len(scope)} file(s) and the work is "
                    f"high-risk; not verified by a judge"
                ),
                confidence=0.55,
                certain=False,
                files=tuple(sorted(scope)[:MAX_EVIDENCE_FILES]),
                recommended_action="confirm the boundaries of each lane's work",
                judge=verdict,
                metadata={"risk": _max_tier(a, b), "escalated_without_judge": "true"},
            )
        return None

    async def _semantic(self, a: Intent, b: Intent) -> ConflictPrediction | None:
        verdict = await self._consult(a, b)
        if verdict is None or not verdict.known or not verdict.conflict:
            return None
        if verdict.confidence < self.semantic_threshold:
            return None
        return self._from_judge(a, b, Signal.SEMANTIC, frozenset(), verdict)

    def _from_judge(
        self,
        a: Intent,
        b: Intent,
        signal: Signal,
        files: frozenset[str],
        verdict: JudgeVerdict,
    ) -> ConflictPrediction:
        return ConflictPrediction(
            session_id=a.session_id or b.session_id,
            lane_a=a.lane_id,
            lane_b=b.lane_id,
            signal=signal,
            severity=Severity.WARN if _is_high_risk(a, b) else Severity.INFO,
            reason=verdict.reason or f"judge reported {verdict.kind}",
            confidence=verdict.confidence,
            certain=False,
            files=tuple(sorted(files)[:MAX_EVIDENCE_FILES]),
            recommended_action=verdict.action or "negotiate the boundary before proceeding",
            judge=verdict,
            metadata={"risk": _max_tier(a, b), "judge_kind": verdict.kind},
        )

    # --- cost control ------------------------------------------------------
    async def _consult(self, a: Intent, b: Intent) -> JudgeVerdict | None:
        """Call the judge, or return ``None`` when there is no judge at all."""
        if self.judge is None or not self.judge.available:
            return None
        self.judge_calls += 1
        verdict = await self.judge.judge(a, b)
        if verdict.is_unknown:
            self.judge_unknown += 1
        return verdict

    @staticmethod
    def _worth_judging(a: Intent, b: Intent) -> bool:
        """Cheap prefilter, run before spending a model call.

        The rule is deliberately generous — anything that shares a top-level
        directory, a symbol, or a plan step is worth asking about — because the
        cost of the filter being too loose is money, and the cost of it being too
        tight is silence. Silence is the failure mode that gets people hurt.
        """
        if a.step_ids & b.step_ids:
            return True
        if a.symbols & b.symbols:
            return True
        if a.directories & b.directories:
            return True
        for left in a.files | a.directories:
            if any(_top_level(left) == _top_level(right) for right in b.files | b.directories):
                return True
        # Two lanes with nothing recorded at all may still be about to collide;
        # asking is cheap relative to the collision.
        return a.is_empty and b.is_empty


def _top_level(path: str) -> str:
    parts = PurePosixPath(path).parts
    return parts[0] if parts else ""


def _is_high_risk(a: Intent, b: Intent) -> bool:
    return _max_tier(a, b) in {HIGH, CRITICAL}


def _max_tier(a: Intent, b: Intent) -> str:
    order = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    return a.risk_tier if order.get(a.risk_tier, 1) >= order.get(b.risk_tier, 1) else b.risk_tier


__all__ = [
    "DEFAULT_SCOPE_THRESHOLD",
    "DEFAULT_SEMANTIC_THRESHOLD",
    "MAX_EVIDENCE_FILES",
    "ConflictPrediction",
    "ConflictPredictor",
    "Severity",
    "Signal",
]
