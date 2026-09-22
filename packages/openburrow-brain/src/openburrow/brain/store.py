"""The Brain: durable, anchored knowledge about this repository.

The store's job is not to remember things. Any text file remembers things. Its
job is to decide **what deserves to be remembered**, and to be honest about how
much it knows.

That shows up as a corroboration rule with one deliberate asymmetry:

* A **session-scoped** observation is trusted on first sight. The whole point of
  the Brain inside a running session is that lane A learns something at 10:04 and
  lane B stops making the same mistake at 10:05. Requiring a second witness would
  make it too slow to be useful, and the blast radius of a wrong session lesson is
  one session.
* A **repository-scoped** entry needs corroboration before it is injected.
  It outlives the session and will be read by people who were not there to see
  where it came from. A single harness's confident assertion about project
  conventions is exactly the kind of thing that becomes folklore.

The asymmetry is not a heuristic bolted on. It falls out of asking "what is the
cost of being wrong, and who pays it?" — the same question the governance layer
asks about authority, applied here to belief.
"""

from __future__ import annotations

from dataclasses import dataclass

from openburrow.brain.anchors import AnchorChecker
from openburrow.core.db.engine import Database
from openburrow.core.db.repository import Repository
from openburrow.core.errors import OpenBurrowError
from openburrow.core.logging import get_logger
from openburrow.core.models import BrainEntry, BrainEntryStatus, BrainEntryType

log = get_logger(__name__)

#: Confidence assigned to an entry nobody has confirmed yet.
UNCORROBORATED_CONFIDENCE = 0.45

#: Confidence once a second, independent lane reports the same thing.
CORROBORATED_CONFIDENCE = 0.8

#: How many distinct source lanes count as corroboration.
CORROBORATION_THRESHOLD = 2

#: Below this, an entry is listed and searchable but not injected into a prompt.
INJECTION_CONFIDENCE_FLOOR = 0.5


class BrainError(OpenBurrowError):
    """The Brain could not satisfy a request."""

    code = "openburrow.brain_error"
    hint = "Check `burrow brain list` for what the Brain currently holds."


@dataclass(frozen=True, slots=True)
class Candidate:
    """A proposed entry, before the store decides whether to believe it.

    Candidates arrive from three places — a harness tagging its own output, the
    classifier reading the bus, and AGENTS.md ingestion — and they all go through
    the same promotion path. There is no side door, because the moment there is
    one, the corroboration rule stops meaning anything.
    """

    title: str
    body: str
    entry_type: BrainEntryType = BrainEntryType.CONVENTION
    anchor_path: str = ""
    anchor_symbol: str = ""
    anchor_commit: str = ""
    source_lane: str = ""
    source_harness: str = ""
    source_message_id: str = ""
    promoted_by: str = "classifier"
    tags: list[str] | None = None


@dataclass(frozen=True, slots=True)
class PromotionResult:
    """What the store did with a candidate."""

    entry: BrainEntry
    created: bool
    corroborated: bool
    reason: str


class BrainStore:
    """Persistence and selection over :class:`~openburrow.core.models.BrainEntry`.

    Every method that changes an entry also decides whether it is *believable*
    and *injectable*, because those two things are properties of the entry as a
    whole and computing them at the call site is how they drift apart.
    """

    def __init__(self, database: Database, *, repo_id: str, repo_root: str | None = None) -> None:
        self.database = database
        self.repo_id = repo_id
        self.anchors = AnchorChecker(repo_root) if repo_root else None

    # --- reading -----------------------------------------------------------
    async def list_entries(
        self, *, active_only: bool = True, path: str | None = None
    ) -> list[BrainEntry]:
        async with self.database.session() as session:
            return await Repository(session).brain_entries(
                repo_id=self.repo_id, path=path, active_only=active_only
            )

    async def get(self, entry_id: str) -> BrainEntry:
        """One entry by id. Raises rather than returning ``None``.

        The annotation said ``BrainEntry | None`` while the body raised, so
        every caller treated a value that cannot be None as optional, and the
        correct assumption at the call site read as a type error.
        """
        async with self.database.session() as session:
            entry = await Repository(session).get(BrainEntry, entry_id)
        if entry is None:
            raise BrainError(
                f"no Brain entry {entry_id!r}",
                hint="List what exists with `burrow brain list`.",
                context={"entry_id": entry_id},
            )
        return entry

    # --- writing -----------------------------------------------------------
    async def promote(self, candidate: Candidate) -> PromotionResult:
        """Turn a candidate into a durable entry, or corroborate an existing one.

        Duplicate detection is by ``(title, anchor_path)`` rather than by body
        text. Two lanes describing the same gotcha in different words are
        corroborating each other; two lanes using the same words about different
        files are not. Title-plus-anchor captures that, and a content hash of the
        body would miss the first case entirely.
        """
        existing = await self._find_match(candidate)
        if existing is not None:
            return await self._corroborate(existing, candidate)

        entry = BrainEntry(
            repo_id=self.repo_id,
            entry_type=candidate.entry_type,
            title=candidate.title,
            body=candidate.body,
            anchor_path=candidate.anchor_path,
            anchor_symbol=candidate.anchor_symbol,
            anchor_commit=candidate.anchor_commit,
            source_lane=candidate.source_lane,
            source_harness=candidate.source_harness,
            source_message_id=candidate.source_message_id,
            promoted_by=candidate.promoted_by,
            confidence=(1.0 if candidate.promoted_by == "human" else UNCORROBORATED_CONFIDENCE),
            confirmed_by="human" if candidate.promoted_by == "human" else "",
            tags=list(candidate.tags or []),
        )
        await self._save(entry)
        log.info(
            "brain.entry.promoted",
            entry_id=entry.id,
            entry_type=str(entry.entry_type),
            anchor=entry.anchor_path,
            confidence=entry.confidence,
        )
        return PromotionResult(
            entry=entry,
            created=True,
            corroborated=False,
            reason="new entry" if candidate.promoted_by != "human" else "human-confirmed",
        )

    async def confirm(self, entry_id: str, *, by: str) -> BrainEntry:
        """Record that a human or a trusted process vouches for an entry."""
        entry = await self.get(entry_id)
        entry.confirm(by=by)
        await self._save(entry)
        log.info("brain.entry.confirmed", entry_id=entry.id, by=by)
        return entry

    async def supersede(self, entry_id: str, *, by_entry_id: str, reason: str = "") -> BrainEntry:
        """Mark an entry stale because a better one replaces it.

        Distinct from drift-staleness: here we know what replaced it, so the old
        entry points at its successor and a reader can follow the chain instead of
        wondering why a fact disappeared.
        """
        entry = await self.get(entry_id)
        entry.mark_stale(superseded_by=by_entry_id, reason=reason)
        await self._save(entry)
        return entry

    async def retire(self, entry_id: str, *, reason: str = "") -> BrainEntry:
        entry = await self.get(entry_id)
        entry.retire(reason=reason)
        await self._save(entry)
        return entry

    async def check_anchors(self) -> list[BrainEntry]:
        """Sweep the whole Brain for drift. Returns the entries that went stale."""
        if self.anchors is None:
            return []
        entries = await self.list_entries()
        stale = self.anchors.sweep(entries)
        for entry in stale:
            await self._save(entry)
        return stale

    # --- selection ---------------------------------------------------------
    async def select_for_injection(
        self,
        *,
        paths: list[str] | None = None,
        budget: int = 8,
    ) -> list[BrainEntry]:
        """Choose the entries worth putting in front of a lane.

        Ranking, in order of precedence:

        1. **Relevant** — anchored to a file the lane is about to touch. A
           convention about a module you are not editing is trivia.
        2. **Believable** — confirmed, or corroborated above the floor.
        3. **Unscoped but confirmed** — a project-wide convention a human stood
           behind. These are few and load-bearing, so they survive the ranking.

        The budget is a hard cap. Context is the scarcest resource in the system;
        an unbounded Brain would crowd out the task itself, which is why the cap
        is enforced here rather than left to the caller to remember.
        """
        entries = await self.list_entries()
        wanted = {p for p in (paths or []) if p}

        scored: list[tuple[float, BrainEntry]] = []
        for entry in entries:
            if entry.confidence < INJECTION_CONFIDENCE_FLOOR:
                continue
            if entry.is_scoped:
                if wanted and entry.anchor_path not in wanted:
                    # Only filter when the caller told us what they are touching.
                    continue
                relevance = 1.0 if entry.anchor_path in wanted else 0.5
            else:
                relevance = 0.7 if entry.confirmed_by else 0.3
            score = relevance * entry.confidence
            scored.append((score, entry))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        chosen = [entry for _, entry in scored[:budget]]
        for entry in chosen:
            entry.record_injection()
            # Persisted, not merely mutated. `list_entries` reads rows out of the
            # database, so a counter bumped on the returned object lived only for
            # the duration of this call and every injection the Brain ever made was
            # invisible afterwards — `injection_count` was always 0 on the next
            # read. Same shape as `lane.heartbeat()`, which mutated an in-memory
            # lane and persisted nothing, leaving the stale-lane detector to read a
            # record that no code maintained.
            await self._save(entry)
        return chosen

    # --- internals ---------------------------------------------------------
    async def _find_match(self, candidate: Candidate) -> BrainEntry | None:
        entries = await self.list_entries(active_only=False)
        for entry in entries:
            if entry.status == BrainEntryStatus.RETIRED:
                continue
            if entry.title.strip().casefold() != candidate.title.strip().casefold():
                continue
            if entry.anchor_path != candidate.anchor_path:
                continue
            return entry
        return None

    async def _corroborate(self, entry: BrainEntry, candidate: Candidate) -> PromotionResult:
        """A second lane independently reported something we already hold.

        Corroboration from the *same* lane is not corroboration. A harness that
        repeats itself is not a second opinion, and counting it would let one
        confused agent manufacture consensus on its own.
        """
        same_source = bool(candidate.source_lane) and candidate.source_lane == entry.source_lane
        if same_source:
            return PromotionResult(
                entry=entry,
                created=False,
                corroborated=False,
                reason="repeat observation from the same lane; not counted as corroboration",
            )

        witnesses = {entry.source_lane, candidate.source_lane} - {""}
        if len(witnesses) < CORROBORATION_THRESHOLD:
            # Still worth recording that someone else said it, but not enough to
            # raise confidence past the injection floor on its own.
            entry.confidence = max(entry.confidence, min(CORROBORATED_CONFIDENCE, 0.6))
            entry.tags = sorted(set(entry.tags) | set(candidate.tags or []))
            await self._save(entry)
            return PromotionResult(
                entry=entry,
                created=False,
                corroborated=False,
                reason="one independent witness; awaiting corroboration",
            )

        entry.confidence = CORROBORATED_CONFIDENCE
        entry.confirmed_by = entry.confirmed_by or "corroborated"
        entry.promoted_by = "corroborated"
        entry.tags = sorted(set(entry.tags) | set(candidate.tags or []))
        await self._save(entry)
        log.info(
            "brain.entry.corroborated",
            entry_id=entry.id,
            witnesses=len(witnesses),
            confidence=entry.confidence,
        )
        return PromotionResult(
            entry=entry,
            created=False,
            corroborated=True,
            reason=f"corroborated by {len(witnesses)} independent lanes",
        )

    async def _save(self, entry: BrainEntry) -> None:
        async with self.database.session() as session:
            await Repository(session).save(entry)


__all__ = [
    "CORROBORATION_THRESHOLD",
    "INJECTION_CONFIDENCE_FLOOR",
    "BrainError",
    "BrainStore",
    "Candidate",
    "PromotionResult",
]
