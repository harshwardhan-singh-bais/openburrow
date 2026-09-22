"""CRDT projection for the web frontend.

Read this before changing anything here, because the obvious design is wrong.

**The CRDT is not the source of truth.** The append-only bus log and the SQLite
database are. This document exists so a browser can render the Brain, the plan,
and the claim board with live sync and offline-tolerant merging — and if it ever
disagrees with the log, the log wins and the document is rebuilt from it.

That ordering is deliberate. The tempting alternative is to make the CRDT
authoritative and let the database be a cache. It gives you lovely merge
semantics and a second source of truth that can silently diverge from the audit
log — and the moment a governance decision has to be justified, "the CRDT said
so" is not an answer anyone can audit. A CRDT is a *view*; the log is the record.

Practical consequences:

* Every mutation here is idempotent. Applying the same bus event twice is a
  no-op, because replay applies events twice by design.
* Nothing in the daemon's decision path reads from here. Governance, lifecycle,
  and claims all read the database. This document is write-only from the daemon's
  perspective and read-only from the browser's.
* ``rebuild`` is a supported operation, not a repair hack. If the document is
  lost or corrupted, rebuilding from the log is the correct response.

pycrdt is imported lazily and its surface is confined to :class:`_YDoc`. If the
API moves, that class is the only thing that needs to change, and the rest of the
Brain keeps working without it — which matters, because a session with no web
frontend is a perfectly normal session.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from openburrow.core.logging import get_logger
from openburrow.core.models import BrainEntry, Plan

log = get_logger(__name__)

#: Top-level shared types. These names are part of the frontend contract — the
#: Next.js client looks them up by name, so renaming one is a breaking change.
BRAIN_KEY = "brain"
PLAN_KEY = "plan"
CLAIMS_KEY = "claims"
META_KEY = "meta"


class CrdtUnavailableError(RuntimeError):
    """pycrdt is not installed, so no shared document can be built."""

    def __init__(self) -> None:
        super().__init__(
            "pycrdt is not installed, so live document sync is unavailable. "
            "Install it with: uv add 'openburrow-core[crdt]'"
        )


class _YDoc:
    """The only place in OpenBurrow that touches pycrdt's API.

    Kept deliberately thin. Every method is a one-liner over pycrdt, so a change
    upstream is a change in one file rather than a hunt through the codebase.
    """

    def __init__(self) -> None:
        try:
            from pycrdt import Doc, Map
        except ImportError as exc:  # pragma: no cover - depends on install
            raise CrdtUnavailableError() from exc

        self._Map = Map
        # Annotated because `Doc` is imported inside the try above, so
        # mypy has no module-level type to infer from.
        self.doc: Any = Doc()
        for key in (BRAIN_KEY, PLAN_KEY, CLAIMS_KEY, META_KEY):
            # `key in self.doc` is true at runtime — pycrdt's Doc serves
            # membership through __getitem__ — but it declares no __contains__,
            # so mypy rejects it. keys() is declared and asks the same question.
            # SIM118 wants the short form back; that is the form mypy refuses.
            if key not in self.doc.keys():  # noqa: SIM118 - see above
                self.doc[key] = Map()

    def map(self, key: str) -> Any:
        return self.doc[key]

    def set(self, key: str, item_id: str, value: Any) -> bool:
        """Set a value, returning whether it actually changed.

        The return value is what makes replay idempotent: the caller can skip
        downstream work when a replayed event produces no change.
        """
        target = self.map(key)
        if target.get(item_id) == value:
            return False
        target[item_id] = value
        return True

    def delete(self, key: str, item_id: str) -> bool:
        target = self.map(key)
        if item_id not in target:
            return False
        del target[item_id]
        return True

    def dump(self, key: str) -> dict[str, Any]:
        return dict(self.map(key))

    def get_update(self) -> bytes:
        return self.doc.get_update()

    def apply_update(self, data: bytes) -> None:
        self.doc.apply_update(data)

    def state_vector(self) -> bytes:
        return self.doc.get_state()


@dataclass(frozen=True, slots=True)
class ChangeSet:
    """What a mutation actually changed, so callers can decide whether to publish."""

    brain_upserts: int = 0
    brain_removals: int = 0
    plan_steps: int = 0
    claims: int = 0

    @property
    def is_empty(self) -> bool:
        return not (self.brain_upserts or self.brain_removals or self.plan_steps or self.claims)


class BrainDoc:
    """A shared document holding the Brain, the plan, and the claim board.

    Construct one per session. It is cheap and holds no database state; the
    authoritative copy is in SQLite and can rebuild this at any time.
    """

    def __init__(
        self, session_id: str, *, on_change: Callable[[ChangeSet], None] | None = None
    ) -> None:
        self.session_id = session_id
        self._doc = _YDoc()
        self._on_change = on_change
        self._doc.set(META_KEY, "session_id", session_id)
        self._doc.set(META_KEY, "schema", 1)

    # --- brain -------------------------------------------------------------
    def upsert_entry(self, entry: BrainEntry) -> bool:
        """Project a Brain entry into the document.

        The projection is deliberately lossy: embeddings are dropped and the
        entry is flattened to the fields the viewer renders. Shipping a float
        vector to a browser to render a bulleted list would be absurd, and the
        viewer never needs to compute similarity.
        """
        payload = {
            "id": entry.id,
            "title": entry.title,
            "body": entry.body,
            "entry_type": str(entry.entry_type),
            "status": str(entry.status),
            "anchor_path": entry.anchor_path,
            "anchor_commit": entry.anchor_commit,
            "confidence": entry.confidence,
            "confirmed_by": entry.confirmed_by,
            "tags": list(entry.tags),
            "updated_at": entry.updated_at.isoformat(),
        }
        changed = self._doc.set(BRAIN_KEY, entry.id, payload)
        self._notify(ChangeSet(brain_upserts=1 if changed else 0))
        return changed

    def remove_entry(self, entry_id: str) -> bool:
        changed = self._doc.delete(BRAIN_KEY, entry_id)
        self._notify(ChangeSet(brain_removals=1 if changed else 0))
        return changed

    # --- plan --------------------------------------------------------------
    def upsert_plan(self, plan: Plan) -> int:
        """Project a plan's steps. Returns how many steps changed."""
        changed = 0
        for step in plan.steps:
            payload = {
                "id": step.id,
                "title": step.title,
                "status": str(step.status),
                "owner_lane": step.owner_lane,
                "original_owner_lane": step.original_owner_lane,
                "depends_on": list(step.depends_on),
                "files": list(getattr(step, "files", []) or []),
            }
            if self._doc.set(PLAN_KEY, step.id, payload):
                changed += 1
        self._notify(ChangeSet(plan_steps=changed))
        return changed

    # --- claims ------------------------------------------------------------
    def upsert_claim(self, claim_id: str, payload: dict[str, Any]) -> bool:
        changed = self._doc.set(CLAIMS_KEY, claim_id, payload)
        self._notify(ChangeSet(claims=1 if changed else 0))
        return changed

    def release_claim(self, claim_id: str) -> bool:
        changed = self._doc.delete(CLAIMS_KEY, claim_id)
        self._notify(ChangeSet(claims=1 if changed else 0))
        return changed

    # --- sync --------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        """Plain-JSON view for the frontend and for export."""
        return {
            "session_id": self.session_id,
            "brain": self._doc.dump(BRAIN_KEY),
            "plan": self._doc.dump(PLAN_KEY),
            "claims": self._doc.dump(CLAIMS_KEY),
            "meta": self._doc.dump(META_KEY),
        }

    def encode_update(self) -> bytes:
        """A Yjs update the browser can apply directly.

        This is the whole point of choosing a CRDT the frontend already speaks:
        the bytes go straight into ``Y.applyUpdate`` on the other side, with no
        translation layer and therefore no translation bugs.
        """
        return self._doc.get_update()

    def apply_update(self, data: bytes) -> None:
        """Merge a client-originated update.

        The browser is allowed to edit the plan and the claim board, so this is a
        real merge rather than a trust boundary. It is safe because nothing in the
        daemon's decision path reads the result: a malicious client can make its
        own view wrong, and cannot make the system act on it.
        """
        self._doc.apply_update(data)
        self._notify(ChangeSet(plan_steps=1))

    def state_vector(self) -> bytes:
        return self._doc.state_vector()

    # --- lifecycle ---------------------------------------------------------
    def rebuild(self, *, entries: list[BrainEntry], plan: Plan | None = None) -> ChangeSet:
        """Replace the document's contents from authoritative state.

        Called when the log has moved past the document — after a resume, a
        replay, or a daemon restart. Rebuilding is cheap and always correct, so
        there is no attempt to reconcile incrementally.
        """
        before = self.snapshot()
        for entry_id in list(before["brain"]):
            self._doc.delete(BRAIN_KEY, entry_id)
        for entry_id in list(before["plan"]):
            self._doc.delete(PLAN_KEY, entry_id)

        changes = ChangeSet(brain_removals=len(before["brain"]))
        for entry in entries:
            if self._doc.set(BRAIN_KEY, entry.id, entry.model_dump(mode="json")):
                changes = ChangeSet(
                    brain_upserts=changes.brain_upserts + 1,
                    brain_removals=changes.brain_removals,
                    plan_steps=changes.plan_steps,
                )
        if plan is not None:
            steps = self.upsert_plan(plan)
            changes = ChangeSet(
                brain_upserts=changes.brain_upserts,
                brain_removals=changes.brain_removals,
                plan_steps=steps,
            )
        log.info(
            "brain.crdt.rebuilt",
            session_id=self.session_id,
            entries=len(entries),
            steps=changes.plan_steps,
        )
        return changes

    # --- internals ---------------------------------------------------------
    def _notify(self, changes: ChangeSet) -> None:
        if self._on_change is not None and not changes.is_empty:
            self._on_change(changes)


__all__ = [
    "BRAIN_KEY",
    "CLAIMS_KEY",
    "META_KEY",
    "PLAN_KEY",
    "BrainDoc",
    "ChangeSet",
    "CrdtUnavailableError",
]
