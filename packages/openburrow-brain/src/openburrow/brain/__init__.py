"""The shared Brain and lesson propagation.

Two kinds of memory, kept separate because they have different lifetimes and
different failure modes:

:mod:`openburrow.brain.store`
    **Durable knowledge.** Decisions, gotchas, and conventions that stay true
    until the code changes. Anchored to a file and a commit so staleness is
    measurable rather than guessed.

:mod:`openburrow.brain.lessons`
    **Operational knowledge.** What to do differently next time. Perishable by
    design, evicted automatically when the evidence says nobody is learning from
    it.

:mod:`openburrow.brain.agents_md`
    **Interop.** Reads and writes ``AGENTS.md`` so a team's existing conventions
    work with OpenBurrow instead of needing a parallel copy.

:mod:`openburrow.brain.crdt`
    **A projection, not a record.** Exists so the web frontend can show the
    Brain and the plan with live sync. The bus log remains the source of truth.

The one rule that ties them together: nothing enters a lane's prompt without
having earned its place. Corroboration for durable entries, hit rate for lessons,
and a hard budget for both — because context is the scarcest resource in the
system and an unbounded memory layer would crowd out the task itself.
"""

from openburrow.brain.agents_md import (
    BEGIN_MARKER,
    END_MARKER,
    extract_candidates,
    merge_into,
    parse_sections,
)
from openburrow.brain.anchors import AnchorChecker, AnchorState, AnchorStatus
from openburrow.brain.crdt import BrainDoc, ChangeSet, CrdtUnavailableError
from openburrow.brain.lessons import (
    Classification,
    EvictionReport,
    LessonCandidate,
    LessonClassifier,
    LessonError,
    LessonStore,
)
from openburrow.brain.store import (
    BrainError,
    BrainStore,
    Candidate,
    PromotionResult,
)

__all__ = [
    "BEGIN_MARKER",
    "END_MARKER",
    "AnchorChecker",
    "AnchorState",
    "AnchorStatus",
    "BrainDoc",
    "BrainError",
    "BrainStore",
    "Candidate",
    "ChangeSet",
    "Classification",
    "CrdtUnavailableError",
    "EvictionReport",
    "LessonCandidate",
    "LessonClassifier",
    "LessonError",
    "LessonStore",
    "PromotionResult",
    "extract_candidates",
    "merge_into",
    "parse_sections",
]
