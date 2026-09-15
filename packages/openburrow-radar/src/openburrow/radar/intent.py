"""What each lane is trying to do, stated precisely enough to compare.

The Radar's job is to notice that two lanes are about to collide *before* they
do. That requires knowing what each lane intends to touch — and the interesting
design question is where that knowledge comes from.

There are two sources and they are not equally trustworthy:

**Claims and plan steps are facts.** A lane that called ``claim`` on
``src/api/routes.py`` told us it is editing that file. This is a record of a
decision the lane made, not an inference about what it might do. File overlap
derived from claims is therefore treated as *certain* — the predictor reports it
regardless of any confidence threshold, because suppressing a known collision
behind a score is how you get a merge conflict you could have prevented.

**Language models are guesses.** Asking a model "what is this lane working on"
produces a plausible description, and plausible is exactly the wrong property
here: a description that reads well but names the wrong subsystem will cause the
Radar to miss real conflicts and invent fake ones. So the model is only ever
allowed to fill in ``description``, ``symbols``, and ``risk_tier`` — never
``files``. A lane's file set is either claimed or unknown, and the Radar says
which.

That restriction is the single most important line in this module. The moment an
LLM can add a file to an intent, the deterministic half of the Radar stops being
deterministic, and every downstream claim about "we detected this collision"
becomes unauditable.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import PurePosixPath

from openburrow.core.logging import get_logger
from openburrow.core.models import Claim, ClaimKind, PlanStep

log = get_logger(__name__)

#: Default risk tier names. These are the keys ``policy.risk_tiers`` uses, so a
#: project can define its own tiers in ``openburrow.yaml`` and the Radar will
#: carry the name through without needing to know what it means.
LOW, MEDIUM, HIGH, CRITICAL = "low", "medium", "high", "critical"


def _normalise(path: str) -> str:
    """Normalise a path for comparison.

    Windows-style separators, leading ``./``, and redundant slashes all describe
    the same file. Two lanes that claim the same file with different spellings
    are in conflict, and a string comparison that misses that would report "no
    conflict" while the collision is already happening.
    """
    text = (path or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return str(PurePosixPath(text)) if text else ""


@dataclass(frozen=True, slots=True)
class Intent:
    """A lane's declared working set.

    Immutable, and every derivation returns a new instance. An intent is a
    snapshot of what a lane said at a moment in time; mutating one in place would
    make it impossible to explain afterwards why the Radar flagged a collision.
    """

    lane_id: str
    session_id: str = ""
    description: str = ""
    files: frozenset[str] = frozenset()
    directories: frozenset[str] = frozenset()
    step_ids: frozenset[str] = frozenset()
    symbols: frozenset[str] = frozenset()
    risk_tier: str = MEDIUM
    #: "claims" | "plan" | "claims+plan" | "llm-refined"
    source: str = "claims"
    confidence: float = 1.0

    @property
    def is_empty(self) -> bool:
        return not (self.files or self.directories or self.step_ids)

    @property
    def is_certain(self) -> bool:
        """True when the file set came from claims or the plan, not a model."""
        return self.confidence >= 1.0 and self.source != "llm-refined"

    def touches(self, path: str) -> bool:
        """Does this intent cover ``path``, by file or by directory?"""
        target = _normalise(path)
        if target in self.files:
            return True
        return any(
            target == directory or target.startswith(directory.rstrip("/") + "/")
            for directory in self.directories
        )

    def shared_files(self, other: Intent) -> frozenset[str]:
        """Concrete files both lanes intend to edit.

        Directory-versus-file overlap is *not* included here, because it is a
        weaker signal: a lane owning ``src/`` and another owning ``src/x.py``
        may be perfectly coordinated. :meth:`shared_scope` handles that case
        separately so the two can carry different severities.
        """
        return self.files & other.files

    def shared_scope(self, other: Intent) -> frozenset[str]:
        """Paths covered by one intent's directory claim and the other's files."""
        hits: set[str] = set()
        for path in other.files:
            if self.touches(path) and path not in self.files:
                hits.add(path)
        for path in self.files:
            if other.touches(path) and path not in other.files:
                hits.add(path)
        return frozenset(hits)

    def summary(self) -> str:
        parts = [f"lane {self.lane_id[:12]}"]
        if self.files:
            sample = sorted(self.files)[:3]
            more = f" (+{len(self.files) - len(sample)})" if len(self.files) > 3 else ""
            parts.append("files: " + ", ".join(sample) + more)
        if self.directories:
            parts.append("dirs: " + ", ".join(sorted(self.directories)[:3]))
        if self.step_ids:
            parts.append(f"steps: {len(self.step_ids)}")
        parts.append(f"risk: {self.risk_tier}")
        return "  ".join(parts)


class IntentExtractor:
    """Builds intents from deterministic records, optionally refined by a model."""

    @staticmethod
    def from_claims(
        lane_id: str,
        claims: list[Claim],
        *,
        session_id: str = "",
        risk_tier: str = MEDIUM,
    ) -> Intent:
        """Files and directories a lane has claimed.

        Only active claims count. A released or expired claim describes what a
        lane *used* to be doing, and carrying it forward would make the Radar
        report conflicts between work that has already finished.
        """
        files: set[str] = set()
        directories: set[str] = set()
        steps: set[str] = set()
        intents: list[str] = []

        for claim in claims:
            if not claim.is_active or claim.is_expired:
                continue
            if claim.lane_id and claim.lane_id != lane_id:
                continue
            if claim.kind == ClaimKind.FILE:
                if claim.resource:
                    files.add(_normalise(claim.resource))
                files.update(_normalise(p) for p in claim.patterns if p)
            elif claim.kind == ClaimKind.DIRECTORY:
                if claim.resource:
                    directories.add(_normalise(claim.resource))
            elif claim.kind == ClaimKind.STEP and claim.step_id:
                steps.add(claim.step_id)
            if claim.intent:
                intents.append(claim.intent)

        return Intent(
            lane_id=lane_id,
            session_id=session_id,
            description="; ".join(intents[:3]),
            files=frozenset(f for f in files if f),
            directories=frozenset(d for d in directories if d),
            step_ids=frozenset(steps),
            risk_tier=risk_tier,
            source="claims",
            confidence=1.0,
        )

    @staticmethod
    def from_plan_steps(
        lane_id: str,
        steps: list[PlanStep],
        *,
        session_id: str = "",
        risk_tier: str = MEDIUM,
    ) -> Intent:
        """Target paths of the plan steps a lane owns.

        Steps in a terminal state are excluded for the same reason released
        claims are: the work is done, so a conflict with it is not a conflict.
        """
        files: set[str] = set()
        steps_ids: set[str] = set()
        titles: list[str] = []

        for step in steps:
            if step.owner_lane != lane_id or step.is_done:
                continue
            steps_ids.add(step.id)
            titles.append(step.title)
            files.update(_normalise(p) for p in step.target_paths if p)

        return Intent(
            lane_id=lane_id,
            session_id=session_id,
            description="; ".join(titles[:3]),
            files=frozenset(f for f in files if f),
            step_ids=frozenset(steps_ids),
            risk_tier=risk_tier,
            source="plan",
            confidence=1.0,
        )

    @staticmethod
    def merge(*intents: Intent) -> Intent:
        """Combine intents from different sources for one lane.

        Files are unioned, never intersected: a lane that claimed a file and also
        owns a step targeting it is doing one thing, and reporting the overlap as
        a conflict would be the Radar arguing with itself.
        """
        real = [i for i in intents if i is not None]
        if not real:
            raise ValueError("merge requires at least one intent")
        if len(real) == 1:
            return real[0]

        sources = sorted({i.source for i in real})
        return Intent(
            lane_id=real[0].lane_id,
            session_id=next((i.session_id for i in real if i.session_id), ""),
            description="; ".join(filter(None, (i.description for i in real))),
            files=frozenset().union(*(i.files for i in real)),
            directories=frozenset().union(*(i.directories for i in real)),
            step_ids=frozenset().union(*(i.step_ids for i in real)),
            symbols=frozenset().union(*(i.symbols for i in real)),
            # The highest tier wins. If any source says this is critical, it is
            # critical — understating risk to keep an average tidy is not a
            # trade anyone wants made on their behalf.
            risk_tier=_max_risk([i.risk_tier for i in real]),
            source="+".join(sources),
            confidence=min(i.confidence for i in real),
        )

    @staticmethod
    def with_model_hints(
        intent: Intent,
        *,
        description: str = "",
        symbols: list[str] | None = None,
        risk_tier: str = "",
    ) -> Intent:
        """Attach model-derived hints to an intent.

        Note what this cannot do: there is no ``files`` parameter, and that is
        not an oversight. The model may enrich the *description* of an intent; it
        may not change the set of files the Radar treats as fact.
        """
        return replace(
            intent,
            description=description or intent.description,
            symbols=frozenset(symbols or intent.symbols),
            risk_tier=risk_tier or intent.risk_tier,
            source=intent.source + "+llm",
        )


#: Ordering used when merging intents. Unknown tier names sort below ``high`` but
#: above ``low``, so a project-defined tier is treated as non-trivial without
#: being able to outrank an explicit critical.
_RISK_ORDER: dict[str, int] = {LOW: 0, MEDIUM: 1, HIGH: 2, CRITICAL: 3}


def _max_risk(tiers: list[str]) -> str:
    return max(tiers, key=lambda tier: _RISK_ORDER.get(tier, 1))


__all__ = [
    "CRITICAL",
    "HIGH",
    "LOW",
    "MEDIUM",
    "Intent",
    "IntentExtractor",
]
