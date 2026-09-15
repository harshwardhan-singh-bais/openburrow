"""Shared model behaviour.

Every OpenBurrow domain object inherits :class:`BurrowModel`, which supplies
three things that would otherwise be copy-pasted a dozen times:

* a **prefixed ULID** primary key generated on construction
* **timezone-aware UTC timestamps** that serialise to ISO-8601
* a **content hash** so the bus log can dedupe and the CRDT layer can reconcile

The timestamps are always UTC and always aware. A naive ``datetime`` in a
distributed audit log is a bug waiting to happen, so :meth:`now` is the only
sanctioned way to get one and it never returns naive.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, ClassVar, Self

from pydantic import BaseModel, ConfigDict, Field, computed_field

from openburrow.core.models.ids import new_id


def now() -> datetime:
    """Current time, UTC, timezone-aware. The only clock OpenBurrow reads."""
    return datetime.now(UTC)


def ensure_aware(value: datetime) -> datetime:
    """Coerce a possibly-naive datetime to UTC-aware.

    SQLite round-trips naive datetimes, so anything loaded from the DB passes
    through here on the way back into a model.
    """
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class BurrowModel(BaseModel):
    """Base for every persisted domain object."""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=True,
        ser_json_timedelta="float",
        arbitrary_types_allowed=False,
    )

    #: Subclasses set this; it selects the id prefix from ``models.ids``.
    id_kind: ClassVar[str] = ""

    id: str = Field(default="", description="Prefixed ULID primary key.")
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)

    #: Free-form extension point. Kept off the hot path but available so an
    #: adapter or plugin can attach vendor-specific metadata without a schema bump.
    metadata: dict[str, Any] = Field(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:
        """Fill in an id when the caller did not supply one."""
        if not self.id and self.id_kind:
            object.__setattr__(self, "id", new_id(self.id_kind))

    # --- timestamps --------------------------------------------------------
    def touch(self) -> Self:
        """Mark the object modified. Returns ``self`` so it chains."""
        self.updated_at = now()
        return self

    @property
    def age_seconds(self) -> float:
        return (now() - ensure_aware(self.created_at)).total_seconds()

    @property
    def idle_seconds(self) -> float:
        return (now() - ensure_aware(self.updated_at)).total_seconds()

    # --- hashing -----------------------------------------------------------
    @computed_field  # type: ignore[prop-decorator]
    @property
    def content_hash(self) -> str:
        """Stable hash of the *semantic* content — ids and timestamps excluded.

        Two messages with identical meaning hash identically, which is what lets
        the bus dedupe a re-sent A2A message (item: ``A2A_DEDUPE_WINDOW_S``)
        and what lets the CRDT layer tell "no-op" from "changed".
        """
        payload = self.model_dump(
            mode="json",
            exclude={"id", "created_at", "updated_at", "content_hash"},
        )
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.blake2b(canonical.encode("utf-8"), digest_size=16).hexdigest()

    def to_row(self) -> dict[str, Any]:
        """Flat dict suitable for SQLite insertion (JSON-encoded nested fields)."""
        return json.loads(self.model_dump_json())

    def to_json(self, *, indent: int | None = None) -> str:
        return self.model_dump_json(indent=indent)

    def __hash__(self) -> int:  # models are used as dict keys in the bus
        return hash(self.id)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, BurrowModel):
            return type(self) is type(other) and self.id == other.id
        return NotImplemented


__all__ = ["BurrowModel", "ensure_aware", "now"]
