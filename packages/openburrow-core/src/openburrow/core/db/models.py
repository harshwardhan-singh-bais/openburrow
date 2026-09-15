"""SQLModel table definitions — schema v1.

Each table is a thin persistence shell around a domain model. The domain models
stay free of ORM concerns; these rows handle the impedance mismatch (JSON columns
for nested structures, string columns for enums, indexes for the queries that
actually run).

The one table that is *not* a mirror is :class:`BusEventRow` — it is the
append-only log described in the package docstring, and its shape is designed
for replay and audit rather than for object round-tripping.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Column, Index, Text, UniqueConstraint
from sqlalchemy.types import JSON
from sqlmodel import Field, SQLModel

from openburrow.core.models.base import now


class _Timestamped(SQLModel):
    """Shared audit columns. Every table gets them; no exceptions."""

    created_at: datetime = Field(default_factory=now, index=True)
    updated_at: datetime = Field(default_factory=now)


# ---------------------------------------------------------------------------
# Sessions & lanes
# ---------------------------------------------------------------------------
class SessionRow(_Timestamped, table=True):
    __tablename__ = "sessions"

    id: str = Field(primary_key=True, max_length=64)
    name: str = Field(index=True, max_length=200)
    description: str = Field(default="", sa_column=Column(Text))
    owner: str = Field(default="", index=True, max_length=200)
    status: str = Field(default="created", index=True, max_length=32)

    repo_root: str = Field(default="", max_length=1024)
    branch: str = Field(default="", index=True, max_length=300)
    base_branch: str = Field(default="main", max_length=300)
    base_commit: str = Field(default="", max_length=64)
    head_commit: str = Field(default="", max_length=64)
    extra_branches: list[str] = Field(default_factory=list, sa_column=Column(JSON))

    lanes: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    participants: list[str] = Field(default_factory=list, sa_column=Column(JSON))

    started_at: datetime | None = Field(default=None, index=True)
    ended_at: datetime | None = Field(default=None)

    plan_id: str = Field(default="", max_length=64)
    thread_id: str = Field(default="", index=True, max_length=64)
    tags: list[str] = Field(default_factory=list, sa_column=Column(JSON))

    total_cost_usd: float = Field(default=0.0)
    total_tokens: int = Field(default=0)
    negotiations_run: int = Field(default=0)
    collisions_avoided: int = Field(default=0)
    governance_flags: int = Field(default=0)
    approvals_requested: int = Field(default=0)

    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column("metadata", JSON))


class LaneRow(_Timestamped, table=True):
    __tablename__ = "lanes"
    __table_args__ = (Index("ix_lanes_session_status", "session_id", "status"),)

    id: str = Field(primary_key=True, max_length=64)
    session_id: str = Field(index=True, max_length=64)
    name: str = Field(index=True, max_length=200)
    harness: str = Field(index=True, max_length=64)
    role: str = Field(default="implementer", max_length=32)
    status: str = Field(default="starting", index=True, max_length=32)

    pid: int | None = Field(default=None)
    worktree_path: str = Field(default="", max_length=1024)
    branch: str = Field(default="", max_length=300)
    base_commit: str = Field(default="", max_length=64)
    head_commit: str = Field(default="", max_length=64)
    command: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    env_passthrough: list[str] = Field(default_factory=list, sa_column=Column(JSON))

    agent_card_url: str = Field(default="", max_length=1024)
    a2a_endpoint: str = Field(default="", max_length=1024)
    declared_skills: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    observed_skills: list[str] = Field(default_factory=list, sa_column=Column(JSON))

    owner: str = Field(default="", index=True, max_length=200)
    trust_boundary: str = Field(default="intra_repo", max_length=32)
    can_delegate: bool = Field(default=True)
    transferable: bool = Field(default=True)
    authority_scope: list[str] = Field(default_factory=list, sa_column=Column(JSON))

    max_runtime_s: int = Field(default=0)
    idle_timeout_s: int = Field(default=900)
    started_at: datetime | None = Field(default=None)
    stopped_at: datetime | None = Field(default=None)

    tokens_in: int = Field(default=0)
    tokens_out: int = Field(default=0)
    cost_usd: float = Field(default=0.0)
    restarts: int = Field(default=0)
    messages_sent: int = Field(default=0)
    messages_received: int = Field(default=0)
    last_heartbeat: datetime | None = Field(default=None, index=True)

    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column("metadata", JSON))


# ---------------------------------------------------------------------------
# A2A tasks & the append-only bus log
# ---------------------------------------------------------------------------
class TaskRow(_Timestamped, table=True):
    __tablename__ = "a2a_tasks"
    __table_args__ = (
        Index("ix_tasks_session_state", "session_id", "state"),
        Index("ix_tasks_assignee_state", "assignee_lane", "state"),
    )

    id: str = Field(primary_key=True, max_length=64)
    session_id: str = Field(index=True, max_length=64)
    thread_id: str = Field(default="", index=True, max_length=64)
    requester_lane: str = Field(default="", index=True, max_length=64)
    assignee_lane: str = Field(default="", index=True, max_length=64)
    authorized_by: str = Field(default="", max_length=200)

    title: str = Field(default="", max_length=500)
    instruction: str = Field(default="", sa_column=Column(Text))
    input_payload: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    skill: str = Field(default="", max_length=200)
    step_id: str = Field(default="", index=True, max_length=64)
    parent_task_id: str = Field(default="", index=True, max_length=64)

    state: str = Field(default="submitted", index=True, max_length=32)
    state_history: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    blocking_reason: str = Field(default="", sa_column=Column(Text))
    pending_message_id: str = Field(default="", max_length=64)

    artifacts: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    result_summary: str = Field(default="", sa_column=Column(Text))
    error: str = Field(default="", sa_column=Column(Text))

    delegation_id: str = Field(default="", index=True, max_length=64)
    trust_boundary: str = Field(default="intra_repo", max_length=32)
    authority_scope: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    transferable: bool = Field(default=True)
    delegation_depth: int = Field(default=0)

    submitted_at: datetime | None = Field(default=None, index=True)
    started_at: datetime | None = Field(default=None)
    ended_at: datetime | None = Field(default=None)
    timeout_s: int = Field(default=900)
    tokens_used: int = Field(default=0)
    cost_usd: float = Field(default=0.0)
    retry_count: int = Field(default=0)

    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column("metadata", JSON))


class BusEventRow(SQLModel, table=True):
    """The append-only bus log. Never updated in place.

    ``seq`` is a monotonic integer assigned by SQLite on insert and is the
    canonical ordering — ULIDs sort by millisecond, which is not fine-grained
    enough when several lanes emit in the same millisecond. Everything that
    replays reads ``ORDER BY seq``.

    ``payload`` holds the full serialised domain object, so the log is
    self-contained: a replay needs nothing but this table.
    """

    __tablename__ = "bus_events"
    __table_args__ = (
        Index("ix_bus_session_seq", "session_id", "seq"),
        Index("ix_bus_thread_seq", "thread_id", "seq"),
        Index("ix_bus_type_seq", "event_type", "seq"),
    )

    seq: int | None = Field(default=None, primary_key=True)
    id: str = Field(index=True, unique=True, max_length=64)
    session_id: str = Field(default="", index=True, max_length=64)
    thread_id: str = Field(default="", index=True, max_length=64)
    lane_id: str = Field(default="", index=True, max_length=64)
    task_id: str = Field(default="", index=True, max_length=64)

    event_type: str = Field(index=True, max_length=80)
    #: Mirrors the A2A task state this event corresponds to, when applicable.
    task_state: str = Field(default="", index=True, max_length=32)
    #: INFORMATIVE | BLOCKING | URGENT — ordering hint for delivery.
    priority: str = Field(default="informative", max_length=16)
    trust_boundary: str = Field(default="intra_repo", max_length=32)

    summary: str = Field(default="", max_length=1000)
    payload: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))

    #: Correlates causally related events (a claim -> the message that announced it).
    correlation_id: str = Field(default="", index=True, max_length=64)
    #: Hash of the payload; lets the log reject an exact duplicate re-send.
    content_hash: str = Field(default="", index=True, max_length=64)

    created_at: datetime = Field(default_factory=now, index=True)


class MessageRow(_Timestamped, table=True):
    __tablename__ = "messages"

    id: str = Field(primary_key=True, max_length=64)
    session_id: str = Field(index=True, max_length=64)
    thread_id: str = Field(default="", index=True, max_length=64)
    task_id: str = Field(default="", index=True, max_length=64)

    sender_lane: str = Field(default="", index=True, max_length=64)
    sender_harness: str = Field(default="", max_length=64)
    recipients: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    broadcast: bool = Field(default=False)
    sender_human: str = Field(default="", index=True, max_length=200)
    reply_to: str = Field(default="", max_length=64)

    intent: str = Field(default="inform", index=True, max_length=32)
    priority: str = Field(default="informative", index=True, max_length=16)
    urgency: str = Field(default="normal", max_length=16)

    subject: str = Field(default="", max_length=500)
    body: str = Field(default="", sa_column=Column(Text))
    payload: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))

    trust_boundary: str = Field(default="intra_repo", max_length=32)
    delegation_id: str = Field(default="", max_length=64)
    signature: str = Field(default="", max_length=512)
    scrubbed: bool = Field(default=False)

    delivered_at: datetime | None = Field(default=None)
    acknowledged_at: datetime | None = Field(default=None)
    requires_reply: bool = Field(default=False, index=True)
    expires_at: datetime | None = Field(default=None)

    brain_worthy: bool = Field(default=False, index=True)
    lesson_worthy: bool = Field(default=False, index=True)

    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column("metadata", JSON))


class NegotiationRow(_Timestamped, table=True):
    __tablename__ = "negotiations"

    id: str = Field(primary_key=True, max_length=64)
    session_id: str = Field(index=True, max_length=64)
    task_id: str = Field(default="", max_length=64)
    trigger: str = Field(default="merge_radar", max_length=32)

    lane_a: str = Field(default="", index=True, max_length=64)
    lane_b: str = Field(default="", index=True, max_length=64)
    topic: str = Field(default="", max_length=500)
    description: str = Field(default="", sa_column=Column(Text))
    contested_refs: list[str] = Field(default_factory=list, sa_column=Column(JSON))

    moves: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    outcome: str = Field(default="pending", index=True, max_length=32)
    predicted_conflict_confidence: float = Field(default=0.0)
    collision_avoided: bool = Field(default=False)
    resolved_by: str = Field(default="", max_length=64)
    escalation_message_id: str = Field(default="", max_length=64)
    max_exchanges: int = Field(default=6)
    started_at: datetime = Field(default_factory=now)
    ended_at: datetime | None = Field(default=None)

    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column("metadata", JSON))


# ---------------------------------------------------------------------------
# Claims, plans
# ---------------------------------------------------------------------------
class ClaimRow(_Timestamped, table=True):
    __tablename__ = "claims"
    __table_args__ = (
        Index("ix_claims_session_status", "session_id", "status"),
        UniqueConstraint("session_id", "resource", "kind", "status", name="uq_claim_active"),
    )

    id: str = Field(primary_key=True, max_length=64)
    session_id: str = Field(index=True, max_length=64)
    lane_id: str = Field(index=True, max_length=64)
    owner: str = Field(default="", max_length=200)

    kind: str = Field(default="file", max_length=16)
    resource: str = Field(index=True, max_length=1024)
    patterns: list[str] = Field(default_factory=list, sa_column=Column(JSON))

    status: str = Field(default="active", index=True, max_length=16)
    intent: str = Field(default="", sa_column=Column(Text))
    step_id: str = Field(default="", max_length=64)

    expires_at: datetime | None = Field(default=None, index=True)
    released_at: datetime | None = Field(default=None)
    released_reason: str = Field(default="", max_length=300)
    forced: bool = Field(default=False)
    revoked_by: str = Field(default="", max_length=200)

    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column("metadata", JSON))


class PlanRow(_Timestamped, table=True):
    __tablename__ = "plans"

    id: str = Field(primary_key=True, max_length=64)
    session_id: str = Field(index=True, max_length=64)
    title: str = Field(default="", max_length=500)
    description: str = Field(default="", sa_column=Column(Text))
    version: int = Field(default=1)
    source_lane: str = Field(default="", max_length=64)
    source: str = Field(default="harness", max_length=32)
    source_artifact_id: str = Field(default="", max_length=64)
    history: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    approved_by: str = Field(default="", max_length=200)
    approved_at: datetime | None = Field(default=None)

    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column("metadata", JSON))


class StepRow(_Timestamped, table=True):
    __tablename__ = "plan_steps"
    __table_args__ = (Index("ix_steps_plan_status", "plan_id", "status"),)

    id: str = Field(primary_key=True, max_length=64)
    plan_id: str = Field(index=True, max_length=64)
    title: str = Field(default="", max_length=500)
    description: str = Field(default="", sa_column=Column(Text))

    depends_on: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    blocks: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    parent_step_id: str = Field(default="", max_length=64)
    order: int = Field(default=0)

    status: str = Field(default="pending", index=True, max_length=16)
    owner_lane: str = Field(default="", index=True, max_length=64)
    original_owner_lane: str = Field(default="", max_length=64)
    claimed_at: datetime | None = Field(default=None)

    target_paths: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    required_skills: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    estimated_effort: str = Field(default="", max_length=32)

    task_ids: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    artifact_refs: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    commit_shas: list[str] = Field(default_factory=list, sa_column=Column(JSON))

    started_at: datetime | None = Field(default=None)
    ended_at: datetime | None = Field(default=None)

    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column("metadata", JSON))


# ---------------------------------------------------------------------------
# Knowledge
# ---------------------------------------------------------------------------
class BrainRow(_Timestamped, table=True):
    __tablename__ = "brain_entries"
    __table_args__ = (Index("ix_brain_repo_status", "repo_id", "status"),)

    id: str = Field(primary_key=True, max_length=64)
    repo_id: str = Field(default="", index=True, max_length=128)
    session_id: str = Field(default="", index=True, max_length=64)
    entry_type: str = Field(default="convention", index=True, max_length=32)

    title: str = Field(default="", max_length=300)
    body: str = Field(default="", sa_column=Column(Text))

    anchor_path: str = Field(default="", index=True, max_length=1024)
    anchor_symbol: str = Field(default="", max_length=300)
    anchor_commit: str = Field(default="", max_length=64)
    superseded_by: str = Field(default="", max_length=64)
    status: str = Field(default="active", index=True, max_length=16)

    source_message_id: str = Field(default="", max_length=64)
    source_lane: str = Field(default="", max_length=64)
    source_harness: str = Field(default="", max_length=64)
    confirmed_by: str = Field(default="", max_length=200)
    promoted_by: str = Field(default="classifier", max_length=32)
    confidence: float = Field(default=0.7)

    injection_count: int = Field(default=0)
    last_injected_at: datetime | None = Field(default=None)
    retired_at: datetime | None = Field(default=None)
    retired_reason: str = Field(default="", max_length=500)

    tags: list[str] = Field(default_factory=list, sa_column=Column(JSON))

    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column("metadata", JSON))


class LessonRow(_Timestamped, table=True):
    __tablename__ = "lessons"
    __table_args__ = (Index("ix_lessons_scope_live", "scope", "retired_at"),)

    id: str = Field(primary_key=True, max_length=64)
    session_id: str = Field(default="", index=True, max_length=64)
    repo_id: str = Field(default="", index=True, max_length=128)
    scope: str = Field(default="session", index=True, max_length=16)

    title: str = Field(default="", max_length=300)
    body: str = Field(default="", sa_column=Column(Text))
    trigger: str = Field(default="", sa_column=Column(Text))
    remedy: str = Field(default="", sa_column=Column(Text))

    source_message_id: str = Field(default="", max_length=64)
    source_task_id: str = Field(default="", max_length=64)
    source_lane: str = Field(default="", max_length=64)
    source_harness: str = Field(default="", max_length=64)
    promoted_by: str = Field(default="classifier", max_length=32)
    confidence: float = Field(default=0.6)

    injection_count: int = Field(default=0)
    helped_lanes: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    ignored_by_lanes: list[str] = Field(default_factory=list, sa_column=Column(JSON))

    expires_at: datetime | None = Field(default=None, index=True)
    retired_at: datetime | None = Field(default=None)
    retired_reason: str = Field(default="", max_length=500)
    tags: list[str] = Field(default_factory=list, sa_column=Column(JSON))

    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column("metadata", JSON))


# ---------------------------------------------------------------------------
# Governance
# ---------------------------------------------------------------------------
class DelegationRow(_Timestamped, table=True):
    __tablename__ = "delegations"
    __table_args__ = (
        Index("ix_delg_session_status", "session_id", "status"),
        Index("ix_delg_parent", "parent_delegation_id"),
    )

    id: str = Field(primary_key=True, max_length=64)
    session_id: str = Field(index=True, max_length=64)
    task_id: str = Field(default="", index=True, max_length=64)

    authorized_by: str = Field(index=True, max_length=200)
    authorized_by_email: str = Field(default="", max_length=320)
    delegator_lane: str = Field(default="", index=True, max_length=64)
    delegatee_lane: str = Field(default="", index=True, max_length=64)
    delegatee_owner: str = Field(default="", max_length=200)

    parent_delegation_id: str = Field(default="", max_length=64)
    depth: int = Field(default=0, index=True)
    chain: list[str] = Field(default_factory=list, sa_column=Column(JSON))

    scope: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    parent_scope_snapshot: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    narrowed: bool = Field(default=True)

    purpose: str = Field(default="", max_length=500)
    task_description: str = Field(default="", sa_column=Column(Text))
    transferable: bool = Field(default=False)
    trust_boundary: str = Field(default="intra_repo", max_length=32)
    status: str = Field(default="proposed", index=True, max_length=32)

    explicit_consent: bool = Field(default=False)
    consent_evidence: str = Field(default="", sa_column=Column(Text))
    revoked_by: str = Field(default="", max_length=200)
    revoked_reason: str = Field(default="", sa_column=Column(Text))
    expires_at: datetime | None = Field(default=None)

    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column("metadata", JSON))


class AuditRow(SQLModel, table=True):
    """Append-only audit log. Never updated, never deleted before retention."""

    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_session_seq", "session_id", "seq"),
        Index("ix_audit_severity_seq", "severity", "seq"),
    )

    seq: int | None = Field(default=None, primary_key=True)
    id: str = Field(index=True, unique=True, max_length=64)
    session_id: str = Field(default="", index=True, max_length=64)
    lane_id: str = Field(default="", index=True, max_length=64)
    task_id: str = Field(default="", max_length=64)
    delegation_id: str = Field(default="", index=True, max_length=64)
    message_id: str = Field(default="", max_length=64)

    event: str = Field(index=True, max_length=80)
    severity: str = Field(default="info", index=True, max_length=16)
    trust_boundary: str = Field(default="intra_repo", max_length=32)
    strict: bool = Field(default=False, index=True)

    summary: str = Field(default="", max_length=1000)
    detail: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))

    actor_lane: str = Field(default="", max_length=64)
    actor_harness: str = Field(default="", max_length=64)
    on_behalf_of: str = Field(default="", index=True, max_length=200)
    affected_lanes: list[str] = Field(default_factory=list, sa_column=Column(JSON))

    allowed: bool = Field(default=True, index=True)
    reason: str = Field(default="", sa_column=Column(Text))
    correlation_id: str = Field(default="", index=True, max_length=64)

    created_at: datetime = Field(default_factory=now, index=True)


class ApprovalRow(_Timestamped, table=True):
    __tablename__ = "approvals"
    __table_args__ = (Index("ix_approvals_session_status", "session_id", "status"),)

    id: str = Field(primary_key=True, max_length=64)
    session_id: str = Field(index=True, max_length=64)
    lane_id: str = Field(index=True, max_length=64)
    task_id: str = Field(default="", max_length=64)
    step_id: str = Field(default="", max_length=64)

    action: str = Field(default="", sa_column=Column(Text))
    action_kind: str = Field(default="exec", max_length=32)
    risk_tier: str = Field(default="medium", index=True, max_length=16)
    reason: str = Field(default="", sa_column=Column(Text))
    affected_paths: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    preview: str = Field(default="", sa_column=Column(Text))

    requested_from: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    any_teammate: bool = Field(default=True)
    task_owner: str = Field(default="", max_length=200)

    status: str = Field(default="pending", index=True, max_length=32)
    responded_by: str = Field(default="", max_length=200)
    responded_at: datetime | None = Field(default=None)
    edited_action: str = Field(default="", sa_column=Column(Text))
    note: str = Field(default="", sa_column=Column(Text))

    requested_at: datetime = Field(default_factory=now, index=True)
    expires_at: datetime | None = Field(default=None, index=True)
    on_timeout: str = Field(default="deny", max_length=16)

    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column("metadata", JSON))


class CheckpointRow(SQLModel, table=True):
    __tablename__ = "checkpoints"
    __table_args__ = (Index("ix_ckpt_lane_seq", "lane_id", "sequence"),)

    id: str = Field(primary_key=True, max_length=64)
    session_id: str = Field(index=True, max_length=64)
    lane_id: str = Field(index=True, max_length=64)
    task_id: str = Field(default="", max_length=64)
    step_id: str = Field(default="", max_length=64)

    harness_state: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    harness_session_id: str = Field(default="", max_length=128)
    context_bytes: int = Field(default=0)

    task_state: str = Field(default="", max_length=32)
    last_event_id: str = Field(default="", max_length=64)
    delivered_message_ids: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    negotiation_id: str = Field(default="", max_length=64)
    negotiation_move_index: int = Field(default=0)

    head_commit: str = Field(default="", max_length=64)
    dirty: bool = Field(default=False)
    stash_ref: str = Field(default="", max_length=128)

    sequence: int = Field(default=0, index=True)
    consumed_at: datetime | None = Field(default=None)
    checksum: str = Field(default="", max_length=128)

    created_at: datetime = Field(default_factory=now, index=True)


class SchemaVersionRow(SQLModel, table=True):
    """Tracks the applied schema version so migrations are idempotent."""

    __tablename__ = "schema_version"

    version: int = Field(primary_key=True)
    applied_at: datetime = Field(default_factory=now)
    description: str = Field(default="", max_length=300)


#: Every table, in creation order (respects foreign-key-ish dependencies).
ALL_TABLES: tuple[type[SQLModel], ...] = (
    SessionRow,
    LaneRow,
    TaskRow,
    BusEventRow,
    MessageRow,
    NegotiationRow,
    ClaimRow,
    PlanRow,
    StepRow,
    BrainRow,
    LessonRow,
    DelegationRow,
    AuditRow,
    ApprovalRow,
    CheckpointRow,
    SchemaVersionRow,
)


__all__ = [
    "ALL_TABLES",
    "ApprovalRow",
    "AuditRow",
    "BrainRow",
    "BusEventRow",
    "CheckpointRow",
    "ClaimRow",
    "DelegationRow",
    "LaneRow",
    "LessonRow",
    "MessageRow",
    "NegotiationRow",
    "PlanRow",
    "SchemaVersionRow",
    "SessionRow",
    "StepRow",
    "TaskRow",
]
