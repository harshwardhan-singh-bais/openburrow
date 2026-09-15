"""Controlled vocabularies.

Every enum here exists because a string literal somewhere would otherwise drift.
The A2A and ACP members are **not** invented — they mirror the actual protocol
specifications, and the docstrings say which. That is the whole point of the
project: build on the standard rather than beside it.
"""

from __future__ import annotations

from enum import StrEnum


class TaskState(StrEnum):
    """A2A task lifecycle states.

    Mirrors the eight states A2A defines. The three terminal states
    (``COMPLETED``, ``FAILED``, ``CANCELED``) plus ``REJECTED`` are absorbing:
    once a task reaches one, it never leaves. :func:`is_terminal` encodes that.

    ``INPUT_REQUIRED`` and ``AUTH_REQUIRED`` are the two *blocking* states, and
    they are what makes OpenBurrow's reply-wait and approval mechanics fall out
    of the protocol instead of needing a bespoke blocking call.
    """

    SUBMITTED = "submitted"
    WORKING = "working"
    INPUT_REQUIRED = "input_required"
    AUTH_REQUIRED = "auth_required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    REJECTED = "rejected"


TERMINAL_TASK_STATES: frozenset[TaskState] = frozenset(
    {TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELED, TaskState.REJECTED}
)

BLOCKING_TASK_STATES: frozenset[TaskState] = frozenset(
    {TaskState.INPUT_REQUIRED, TaskState.AUTH_REQUIRED}
)

#: Legal transitions. Anything not listed here raises ``IllegalTaskTransitionError``.
TASK_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.SUBMITTED: frozenset(
        {TaskState.WORKING, TaskState.REJECTED, TaskState.CANCELED, TaskState.FAILED}
    ),
    TaskState.WORKING: frozenset(
        {
            TaskState.INPUT_REQUIRED,
            TaskState.AUTH_REQUIRED,
            TaskState.COMPLETED,
            TaskState.FAILED,
            TaskState.CANCELED,
        }
    ),
    TaskState.INPUT_REQUIRED: frozenset({TaskState.WORKING, TaskState.CANCELED, TaskState.FAILED}),
    TaskState.AUTH_REQUIRED: frozenset(
        {TaskState.WORKING, TaskState.CANCELED, TaskState.FAILED, TaskState.REJECTED}
    ),
    TaskState.COMPLETED: frozenset(),
    TaskState.FAILED: frozenset(),
    TaskState.CANCELED: frozenset(),
    TaskState.REJECTED: frozenset(),
}


def is_terminal(state: TaskState) -> bool:
    return state in TERMINAL_TASK_STATES


def can_transition(source: TaskState, target: TaskState) -> bool:
    return target in TASK_TRANSITIONS[source]


class Performative(StrEnum):
    """ACP-style negotiation performatives.

    Borrowed verbatim from ACP (which derives them from FIPA-ACL) rather than
    invented, so OpenBurrow's negotiation exchanges are readable to anyone who
    already knows the protocol family.

    * ``PROPOSE``  — here is a concrete change I want to make
    * ``COUNTER``  — I want a different concrete change
    * ``ACCEPT``   — agreed, proceed
    * ``REJECT``   — no, and here is why
    * ``INFORM``   — a statement of fact, not a request for action
    * ``WITHDRAW`` — I am retracting an outstanding proposal
    """

    PROPOSE = "propose"
    COUNTER = "counter"
    ACCEPT = "accept"
    REJECT = "reject"
    INFORM = "inform"
    WITHDRAW = "withdraw"


#: Which performatives may legitimately answer which.
PERFORMATIVE_REPLIES: dict[Performative, frozenset[Performative]] = {
    Performative.PROPOSE: frozenset(
        {Performative.ACCEPT, Performative.REJECT, Performative.COUNTER, Performative.WITHDRAW}
    ),
    Performative.COUNTER: frozenset(
        {Performative.ACCEPT, Performative.REJECT, Performative.COUNTER, Performative.WITHDRAW}
    ),
    Performative.ACCEPT: frozenset({Performative.INFORM}),
    Performative.REJECT: frozenset({Performative.PROPOSE, Performative.INFORM}),
    Performative.INFORM: frozenset(
        {Performative.PROPOSE, Performative.COUNTER, Performative.INFORM}
    ),
    Performative.WITHDRAW: frozenset({Performative.PROPOSE, Performative.INFORM}),
}


class MessagePriority(StrEnum):
    """Whether a message may interrupt a working harness.

    ``BLOCKING`` puts the receiving task into ``INPUT_REQUIRED`` and stops the
    harness until answered. ``INFORMATIVE`` is delivered at the next natural
    boundary. The distinction is what stops low-value chatter from thrashing a
    harness mid-task (roadmap item 48).
    """

    INFORMATIVE = "informative"
    BLOCKING = "blocking"
    URGENT = "urgent"


class Urgency(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


class LaneStatus(StrEnum):
    """Local lane state — richer than A2A's task states, projected onto them."""

    STARTING = "starting"
    IDLE = "idle"
    WORKING = "working"
    WAITING_INPUT = "waiting_input"
    WAITING_AUTH = "waiting_auth"
    WATCHING = "watching"
    BLOCKED = "blocked"
    NEGOTIATING = "negotiating"
    CRASHED = "crashed"
    STOPPED = "stopped"

    def to_task_state(self) -> TaskState:
        """Project a lane status onto the A2A task state it implies."""
        return _LANE_TO_TASK[self]


_LANE_TO_TASK: dict[LaneStatus, TaskState] = {
    LaneStatus.STARTING: TaskState.SUBMITTED,
    LaneStatus.IDLE: TaskState.SUBMITTED,
    LaneStatus.WORKING: TaskState.WORKING,
    LaneStatus.WAITING_INPUT: TaskState.INPUT_REQUIRED,
    LaneStatus.WAITING_AUTH: TaskState.AUTH_REQUIRED,
    LaneStatus.WATCHING: TaskState.WORKING,
    LaneStatus.BLOCKED: TaskState.INPUT_REQUIRED,
    LaneStatus.NEGOTIATING: TaskState.WORKING,
    LaneStatus.CRASHED: TaskState.FAILED,
    LaneStatus.STOPPED: TaskState.CANCELED,
}


class SessionStatus(StrEnum):
    CREATED = "created"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    ABANDONED = "abandoned"
    FAILED = "failed"


class LaneRole(StrEnum):
    IMPLEMENTER = "implementer"
    REVIEWER = "reviewer"
    OBSERVER = "observer"
    COORDINATOR = "coordinator"
    CUSTOM = "custom"


class StepStatus(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    REVIEW = "review"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class ClaimKind(StrEnum):
    FILE = "file"
    DIRECTORY = "directory"
    STEP = "step"
    RESOURCE = "resource"


class ClaimStatus(StrEnum):
    ACTIVE = "active"
    RELEASED = "released"
    EXPIRED = "expired"
    REVOKED = "revoked"


class BrainEntryType(StrEnum):
    """The three kinds of durable knowledge worth pinning to a file + commit."""

    DECISION = "decision"
    GOTCHA = "gotcha"
    CONVENTION = "convention"


class BrainEntryStatus(StrEnum):
    ACTIVE = "active"
    STALE = "stale"
    RETIRED = "retired"


class LessonScope(StrEnum):
    SESSION = "session"
    REPO = "repo"
    ORG = "org"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    APPROVED_EDITED = "approved_edited"
    DENIED = "denied"
    TIMED_OUT = "timed_out"
    ESCALATED = "escalated"


class DelegationStatus(StrEnum):
    PROPOSED = "proposed"
    AUTHORIZED = "authorized"
    ACTIVE = "active"
    COMPLETED = "completed"
    REVOKED = "revoked"
    DENIED = "denied"


class TrustBoundary(StrEnum):
    """What makes a message "cross-boundary" for audit purposes (item 184)."""

    INTRA_LANE = "intra_lane"  # same lane talking to itself (should not happen)
    INTRA_REPO = "intra_repo"  # two lanes, same repo, same human owner
    CROSS_HUMAN = "cross_human"  # two humans' lanes, same machine or relay
    CROSS_VENDOR = "cross_vendor"  # different harness vendors
    CROSS_ORG = "cross_org"  # different relay tenants entirely
    EXTERNAL = "external"  # a third-party A2A peer outside the session


class AuditSeverity(StrEnum):
    INFO = "info"
    NOTICE = "notice"
    WARNING = "warning"
    VIOLATION = "violation"
    CRITICAL = "critical"


class NegotiationOutcome(StrEnum):
    PENDING = "pending"
    AGREED = "agreed"
    REJECTED = "rejected"
    ESCALATED = "escalated"
    TIMED_OUT = "timed_out"
    ABANDONED = "abandoned"


class NegotiationTrigger(StrEnum):
    """What opened a negotiation — useful for measuring whether Radar earns its keep."""

    MERGE_RADAR = "merge_radar"
    CLAIM_COLLISION = "claim_collision"
    REVIEW_REQUEST = "review_request"
    HANDOFF_DISPUTE = "handoff_dispute"
    OVERLAP_DETECTED = "overlap_detected"
    MANUAL = "manual"


class HookEvent(StrEnum):
    SESSION_START = "session.start"
    SESSION_END = "session.end"
    LANE_START = "lane.start"
    LANE_STOP = "lane.stop"
    LANE_CRASH = "lane.crash"
    BUS_MESSAGE = "bus.message"
    NEGOTIATION_START = "negotiation.start"
    NEGOTIATION_END = "negotiation.end"
    CLAIM_CREATED = "claim.created"
    CLAIM_RELEASED = "claim.released"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_RESOLVED = "approval.resolved"
    GOVERNANCE_FLAG = "governance.flag"
    DELEGATION_CREATED = "delegation.created"
    STEP_COMPLETED = "step.completed"


__all__ = [
    "BLOCKING_TASK_STATES",
    "PERFORMATIVE_REPLIES",
    "TASK_TRANSITIONS",
    "TERMINAL_TASK_STATES",
    "ApprovalStatus",
    "AuditSeverity",
    "BrainEntryStatus",
    "BrainEntryType",
    "ClaimKind",
    "ClaimStatus",
    "DelegationStatus",
    "HookEvent",
    "LaneRole",
    "LaneStatus",
    "LessonScope",
    "MessagePriority",
    "NegotiationOutcome",
    "NegotiationTrigger",
    "Performative",
    "SessionStatus",
    "StepStatus",
    "TaskState",
    "TrustBoundary",
    "Urgency",
    "can_transition",
    "is_terminal",
]
