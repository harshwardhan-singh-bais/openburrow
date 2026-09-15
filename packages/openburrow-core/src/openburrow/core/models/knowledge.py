"""Shared Brain entries and propagated lessons.

These are two different things and the distinction matters:

* A **BrainEntry** is *durable, anchored knowledge about this codebase* — a
  decision, a gotcha, a convention — pinned to a file and a commit. Its value
  comes from being true and staying true; when the anchor commit is superseded,
  the entry is flagged stale rather than silently trusted.

* A **Lesson** is *operational knowledge about doing the work* — "this API needs
  a pagination token", "the test fixture must be reset first". It is scoped
  shorter (session, or repo) and expires. Its value comes from spreading fast.

Both carry an explicit provenance chain back to the A2A message that produced
them. That is not decoration: a poisoned lesson is one of the named attack
surfaces (item 189), and you cannot defend against an attack you cannot trace.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import ClassVar

from pydantic import Field, computed_field, field_validator

from openburrow.core.models.base import BurrowModel, ensure_aware, now
from openburrow.core.models.enums import (
    BrainEntryStatus,
    BrainEntryType,
    LessonScope,
)


class BrainEntry(BurrowModel):
    """One durable fact about the codebase, anchored to a file and a commit."""

    id_kind: ClassVar[str] = "brain"

    repo_id: str = ""
    session_id: str = ""
    entry_type: BrainEntryType = BrainEntryType.CONVENTION

    title: str = ""
    body: str = ""

    # --- anchoring ---------------------------------------------------------
    #: Repo-relative path this knowledge is about. Empty = repo-wide.
    anchor_path: str = ""
    #: Symbol, function, or line range within the anchor, when applicable.
    anchor_symbol: str = ""
    #: Commit the entry was true at. Staleness is computed against this.
    anchor_commit: str = ""
    #: Commit that superseded the anchor, set when staleness is detected.
    superseded_by: str = ""

    status: BrainEntryStatus = BrainEntryStatus.ACTIVE

    # --- provenance --------------------------------------------------------
    #: The A2A message that produced this entry. The audit trail's anchor.
    source_message_id: str = ""
    source_lane: str = ""
    source_harness: str = ""
    #: Human who confirmed it, when a human did.
    confirmed_by: str = ""
    #: "harness-tagged" | "classifier" | "human" | "agents-md-seed"
    promoted_by: str = "classifier"
    confidence: float = 0.7

    # --- lifecycle ---------------------------------------------------------
    #: How many times this entry was injected into a lane's context.
    injection_count: int = 0
    last_injected_at: datetime | None = None
    retired_at: datetime | None = None
    retired_reason: str = ""

    # --- search ------------------------------------------------------------
    tags: list[str] = Field(default_factory=list)
    #: Populated only when embeddings are enabled; keyword search works without it.
    embedding: list[float] = Field(default_factory=list, exclude=True)

    @field_validator("title", mode="before")
    @classmethod
    def _title_required(cls, value: object) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("brain entry requires a title")
        return text[:200]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_active(self) -> bool:
        return self.status == BrainEntryStatus.ACTIVE

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_scoped(self) -> bool:
        """True when the entry claims to be about a specific file."""
        return bool(self.anchor_path)

    def mark_stale(self, *, superseded_by: str, reason: str = "") -> None:
        self.status = BrainEntryStatus.STALE
        self.superseded_by = superseded_by
        self.retired_reason = reason or f"anchor commit superseded by {superseded_by[:8]}"
        self.touch()

    def confirm(self, *, by: str) -> None:
        self.confirmed_by = by
        self.confidence = 1.0
        self.touch()

    def retire(self, *, reason: str = "") -> None:
        self.status = BrainEntryStatus.RETIRED
        self.retired_at = now()
        self.retired_reason = reason
        self.touch()

    def record_injection(self) -> None:
        self.injection_count += 1
        self.last_injected_at = now()
        self.touch()

    def to_agents_md(self) -> str:
        """Render as an ``AGENTS.md``-compatible bullet (item 109).

        The point of this method is that a repository which never installs
        OpenBurrow still benefits from what the participating lanes learned —
        the knowledge lands in a file every other tool already reads.
        """
        scope = f" (`{self.anchor_path}`)" if self.anchor_path else ""
        marker = {"decision": "DECISION", "gotcha": "GOTCHA", "convention": "CONVENTION"}[
            str(self.entry_type)
        ]
        return f"- **{marker}**{scope}: {self.title} — {self.body}".rstrip()

    def render_for_injection(self) -> str:
        """Compact form injected into a lane's context before it starts."""
        scope = f" [{self.anchor_path}]" if self.anchor_path else ""
        return f"[{str(self.entry_type).upper()}]{scope} {self.title}: {self.body}"


class Lesson(BurrowModel):
    """Operational knowledge, promoted from a tagged A2A message.

    Deliberately short-lived by default. A lesson that outlives its usefulness
    is worse than no lesson, because it will be injected into a future lane and
    mislead it — so ``ttl_days`` exists and expiry is the default outcome.
    """

    id_kind: ClassVar[str] = "lesson"

    session_id: str = ""
    repo_id: str = ""
    scope: LessonScope = LessonScope.SESSION

    title: str = ""
    body: str = ""
    #: The concrete situation that triggers this lesson.
    trigger: str = ""
    #: What to do about it.
    remedy: str = ""

    # --- provenance --------------------------------------------------------
    source_message_id: str = ""
    source_task_id: str = ""
    source_lane: str = ""
    source_harness: str = ""
    #: "harness-tagged" | "classifier" | "human"
    promoted_by: str = "classifier"
    confidence: float = 0.6

    # --- effect measurement (item 217) ------------------------------------
    injection_count: int = 0
    #: Lanes that avoided the problem after receiving this lesson.
    helped_lanes: list[str] = Field(default_factory=list)
    #: Lanes that received it and still hit the problem — a signal it is wrong.
    ignored_by_lanes: list[str] = Field(default_factory=list)

    # --- lifecycle ---------------------------------------------------------
    expires_at: datetime | None = None
    retired_at: datetime | None = None
    retired_reason: str = ""
    tags: list[str] = Field(default_factory=list)
    embedding: list[float] = Field(default_factory=list, exclude=True)

    @field_validator("title", mode="before")
    @classmethod
    def _title_required(cls, value: object) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("lesson requires a title")
        return text[:200]

    @classmethod
    def with_ttl(
        cls,
        *,
        title: str,
        body: str,
        ttl_days: int,
        scope: LessonScope = LessonScope.SESSION,
        **extra: object,
    ) -> Lesson:
        expires = now() + timedelta(days=ttl_days) if ttl_days > 0 else None
        # No `type: ignore` here. There used to be one claiming the `**extra`
        # splat needed an arg-type suppression; mypy reports it as unused, which
        # means the comment was asserting a problem that does not exist. An
        # unused suppression is not harmless — it reads as "this was checked and
        # needed an escape hatch", and that is a claim nobody verified.
        return cls(title=title, body=body, scope=scope, expires_at=expires, **extra)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and now() > ensure_aware(self.expires_at)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_live(self) -> bool:
        return self.retired_at is None and not self.is_expired

    @computed_field  # type: ignore[prop-decorator]
    @property
    def hit_rate(self) -> float:
        """Share of injections that appear to have helped.

        Surfaced in ``burrow report`` as the answer to "is lesson propagation
        actually doing anything, or are we just printing text?" (item 217)
        """
        total = self.injection_count
        if total == 0:
            return 0.0
        return round(len(self.helped_lanes) / total, 4)

    def record_injection(self, lane_id: str) -> None:
        self.injection_count += 1
        self.touch()

    def record_helped(self, lane_id: str) -> None:
        if lane_id not in self.helped_lanes:
            self.helped_lanes.append(lane_id)
        self.touch()

    def record_ignored(self, lane_id: str) -> None:
        if lane_id not in self.ignored_by_lanes:
            self.ignored_by_lanes.append(lane_id)
        self.touch()

    def retire(self, *, reason: str = "") -> None:
        self.retired_at = now()
        self.retired_reason = reason
        self.touch()

    def render_for_injection(self) -> str:
        parts = [f"[LESSON] {self.title}"]
        if self.trigger:
            parts.append(f"When: {self.trigger}")
        if self.remedy:
            parts.append(f"Do: {self.remedy}")
        elif self.body:
            parts.append(self.body)
        return " | ".join(parts)


__all__ = ["BrainEntry", "Lesson"]
