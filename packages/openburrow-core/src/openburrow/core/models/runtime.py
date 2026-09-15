"""Runtime state: checkpoints, session metrics, and the reel manifest.

These are the models that make a session *durable* and *measurable*. Nothing
here is user-facing in the domain sense — but without them, crash recovery,
replay, and the honest reporting in ``burrow report`` are all impossible.

:class:`Checkpoint` deserves a note. It snapshots two things together: the
harness's own conversation state, and the A2A task-lifecycle position. Restoring
only the first gives you an agent that resumes mid-thought with no memory of what
it was negotiating; restoring only the second gives you a bus that thinks a task
is in flight with nobody doing it. They have to move as one unit, which is why
they share a row.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from pydantic import Field, computed_field

from openburrow.core.models.base import BurrowModel, now


class Checkpoint(BurrowModel):
    """A resumable snapshot of one lane's work and its bus position."""

    id_kind: ClassVar[str] = "checkpoint"

    session_id: str = ""
    lane_id: str = ""
    task_id: str = ""
    step_id: str = ""

    # --- harness side --------------------------------------------------------
    #: Opaque to OpenBurrow; whatever the adapter needs to resume its harness.
    harness_state: dict[str, Any] = Field(default_factory=dict)
    harness_session_id: str = ""
    #: Bytes of the harness's conversation/context, when the harness exposes it.
    context_bytes: int = 0

    # --- bus side ------------------------------------------------------------
    #: The A2A task state at snapshot time.
    task_state: str = ""
    #: Id of the last bus event included in this checkpoint.
    last_event_id: str = ""
    #: Messages already delivered, so resume does not re-deliver them.
    delivered_message_ids: list[str] = Field(default_factory=list)
    #: Negotiation position, so an interrupted ACP exchange resumes mid-thread
    #: rather than restarting from zero (item 169).
    negotiation_id: str = ""
    negotiation_move_index: int = 0

    # --- git ------------------------------------------------------------------
    head_commit: str = ""
    dirty: bool = False
    stash_ref: str = ""

    # --- integrity ------------------------------------------------------------
    sequence: int = 0
    #: Set once this checkpoint has been used to resume, for idempotency (item 160).
    consumed_at: datetime | None = None
    checksum: str = ""

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_consumed(self) -> bool:
        return self.consumed_at is not None

    def consume(self) -> None:
        self.consumed_at = now()
        self.touch()

    def render(self) -> str:
        return (
            f"checkpoint {self.id} seq={self.sequence} "
            f"task={self.task_state} commit={self.head_commit[:8]}"
        )


class LaneMetrics(BurrowModel):
    """Per-lane counters, rolled up into the session report."""

    id_kind: ClassVar[str] = "lane"

    session_id: str = ""
    lane_id: str = ""
    harness: str = ""

    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0

    messages_sent: int = 0
    messages_received: int = 0
    #: Messages that visibly changed what this lane did (item 54).
    messages_that_changed_output: int = 0

    tasks_submitted: int = 0
    tasks_completed: int = 0
    tasks_failed: int = 0
    retries: int = 0
    replans: int = 0

    negotiations_entered: int = 0
    negotiations_won: int = 0
    negotiations_lost: int = 0

    lessons_injected: int = 0
    lessons_that_helped: int = 0

    tool_calls: int = 0
    files_changed: int = 0
    lines_added: int = 0
    lines_removed: int = 0

    uptime_seconds: float = 0.0
    crash_count: int = 0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def message_reply_rate(self) -> float:
        """Bus health signal (item 218): are lanes actually answering each other?"""
        if self.messages_received == 0:
            return 0.0
        return round(self.messages_sent / self.messages_received, 4)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def message_effectiveness(self) -> float:
        """Share of inbound messages that changed behaviour (item 54).

        This is the metric that answers "is the collaboration real or
        decorative?" — a bus with high volume and near-zero effectiveness is
        just expensive noise, and this number makes that visible.
        """
        if self.messages_received == 0:
            return 0.0
        return round(self.messages_that_changed_output / self.messages_received, 4)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def task_success_rate(self) -> float:
        attempted = self.tasks_completed + self.tasks_failed
        if attempted == 0:
            return 0.0
        return round(self.tasks_completed / attempted, 4)

    def render_line(self) -> str:
        return (
            f"{self.lane_id:<16} {self.harness:<12} "
            f"msgs {self.messages_sent:>4}/{self.messages_received:<4} "
            f"tasks {self.tasks_completed:>3}/{self.tasks_submitted:<3} "
            f"${self.cost_usd:>7.4f}  crash×{self.crash_count}"  # noqa: RUF001 - display only
        )


class SessionMetrics(BurrowModel):
    """Session-wide rollup — the numbers ``burrow report`` and the dashboard show."""

    id_kind: ClassVar[str] = "session"

    session_id: str = ""
    lanes: list[LaneMetrics] = Field(default_factory=list)

    total_messages: int = 0
    negotiations_issued: int = 0
    collisions_avoided: int = 0
    #: Conflicts the Radar predicted that did *not* materialise.
    false_positive_negotiations: int = 0
    approvals_requested: int = 0
    approvals_denied: int = 0
    governance_flags_raised: int = 0
    governance_flags_resolved: int = 0
    lessons_promoted: int = 0
    brain_entries_added: int = 0
    checkpoints_taken: int = 0
    resumes: int = 0
    dead_letters: int = 0

    started_at: datetime | None = None
    ended_at: datetime | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_cost_usd(self) -> float:
        return round(sum(lane.cost_usd for lane in self.lanes), 6)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_tokens(self) -> int:
        return sum(lane.tokens_in + lane.tokens_out for lane in self.lanes)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def negotiation_precision(self) -> float:
        """Of the negotiations we opened, what share prevented a real collision?

        Item 216. A low value here is the honest signal that the confidence
        threshold needs tuning, or that the Radar is not earning its keep.
        """
        issued = self.negotiations_issued
        if issued == 0:
            return 0.0
        return round(self.collisions_avoided / issued, 4)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def governance_resolution_rate(self) -> float:
        raised = self.governance_flags_raised
        if raised == 0:
            return 1.0
        return round(self.governance_flags_resolved / raised, 4)

    @property
    def duration_seconds(self) -> float:
        if self.started_at is None:
            return 0.0
        end = self.ended_at or now()
        return (end - self.started_at).total_seconds()

    def lane(self, lane_id: str) -> LaneMetrics | None:
        return next((m for m in self.lanes if m.lane_id == lane_id), None)

    def summary(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "lanes": len(self.lanes),
            "duration_s": round(self.duration_seconds, 1),
            "messages": self.total_messages,
            "cost_usd": self.total_cost_usd,
            "tokens": self.total_tokens,
            "negotiations": self.negotiations_issued,
            "collisions_avoided": self.collisions_avoided,
            "negotiation_precision": self.negotiation_precision,
            "governance_flags": self.governance_flags_raised,
            "approvals": self.approvals_requested,
            "lessons": self.lessons_promoted,
            "brain_entries": self.brain_entries_added,
            "resumes": self.resumes,
        }


class ReelManifest(BurrowModel):
    """Index of a session's replay bundle (Stage 11).

    The bundle is what ``burrow export`` writes and what the Next.js viewer
    reads. Keeping the manifest as a first-class model — rather than deriving it
    from the files on disk — is what lets a shared link be validated and
    scoped before anything is downloaded.
    """

    id_kind: ClassVar[str] = "reel"

    session_id: str = ""
    session_name: str = ""
    exported_at: datetime = Field(default_factory=now)
    exporter: str = ""

    # --- contents -------------------------------------------------------------
    #: Relative path -> blake2b hash, so the viewer can verify integrity.
    cast_files: dict[str, str] = Field(default_factory=dict)
    a2a_trace_file: str = ""
    timeline_file: str = ""
    plan_file: str = ""
    audit_file: str = ""
    metrics_file: str = ""

    # --- summary for the landing page ----------------------------------------
    lane_count: int = 0
    event_count: int = 0
    negotiation_count: int = 0
    governance_event_count: int = 0
    duration_seconds: float = 0.0
    total_cost_usd: float = 0.0

    # --- sharing --------------------------------------------------------------
    signed: bool = False
    signature: str = ""
    expires_at: datetime | None = None
    #: Optional scoping: which org/team may open the link.
    allowed_orgs: list[str] = Field(default_factory=list)
    public: bool = False

    viewer_version: str = "1"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and now() > self.expires_at

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_viewable(self) -> bool:
        return not self.is_expired

    @computed_field  # type: ignore[prop-decorator]
    @property
    def size_hint_bytes(self) -> int:
        return len(self.cast_files) * 1_000_000  # rough; refined when files are written


__all__ = ["Checkpoint", "LaneMetrics", "ReelManifest", "SessionMetrics"]
