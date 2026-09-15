"""The OpenBurrow domain vocabulary.

Everything in the system — the CLI, the daemon, the relay, the web viewer —
speaks in these types. The package is organised by *domain concern* rather than
by layer, so a reader looking for "how do we model delegation" finds
:mod:`~openburrow.core.models.governance` rather than hunting through a generic
``schemas.py``.

Layout::

    base.py       BurrowModel — ids, timestamps, content hashing
    enums.py      controlled vocabularies (A2A states, ACP performatives)
    ids.py        prefixed ULID generation
    session.py    Session, Lane
    task.py       A2ATask, TaskArtifact
    message.py    BusMessage, NegotiationExchange
    claim.py      Claim
    plan.py       Plan, PlanStep
    knowledge.py  BrainEntry, Lesson
    governance.py Delegation, AuthorityScope, AuditRecord, GovernanceFlag
    approval.py   ApprovalRequest
    runtime.py    Checkpoint, LaneMetrics, SessionMetrics, ReelManifest
"""

from openburrow.core.models.approval import ApprovalRequest
from openburrow.core.models.base import BurrowModel, ensure_aware, now
from openburrow.core.models.claim import Claim
from openburrow.core.models.enums import (
    BLOCKING_TASK_STATES,
    PERFORMATIVE_REPLIES,
    TASK_TRANSITIONS,
    TERMINAL_TASK_STATES,
    ApprovalStatus,
    AuditSeverity,
    BrainEntryStatus,
    BrainEntryType,
    ClaimKind,
    ClaimStatus,
    DelegationStatus,
    HookEvent,
    LaneRole,
    LaneStatus,
    LessonScope,
    MessagePriority,
    NegotiationOutcome,
    NegotiationTrigger,
    Performative,
    SessionStatus,
    StepStatus,
    TaskState,
    TrustBoundary,
    Urgency,
    can_transition,
    is_terminal,
)
from openburrow.core.models.governance import (
    FLAG_KINDS,
    AuditRecord,
    AuthorityScope,
    Delegation,
    GovernanceFlag,
)
from openburrow.core.models.ids import id_kind, is_valid_id, new_id, new_ulid, ulid_to_datetime
from openburrow.core.models.knowledge import BrainEntry, Lesson
from openburrow.core.models.message import (
    INLINE_BODY_LIMIT,
    BusMessage,
    NegotiationExchange,
    NegotiationMove,
)
from openburrow.core.models.plan import Plan, PlanStep
from openburrow.core.models.runtime import (
    Checkpoint,
    LaneMetrics,
    ReelManifest,
    SessionMetrics,
)
from openburrow.core.models.session import Lane, Session
from openburrow.core.models.task import A2ATask, TaskArtifact

__all__ = [
    "BLOCKING_TASK_STATES",
    "FLAG_KINDS",
    "INLINE_BODY_LIMIT",
    "PERFORMATIVE_REPLIES",
    "TASK_TRANSITIONS",
    "TERMINAL_TASK_STATES",
    "A2ATask",
    "ApprovalRequest",
    "ApprovalStatus",
    "AuditRecord",
    "AuditSeverity",
    "AuthorityScope",
    "BrainEntry",
    "BrainEntryStatus",
    "BrainEntryType",
    "BurrowModel",
    "BusMessage",
    "Checkpoint",
    "Claim",
    "ClaimKind",
    "ClaimStatus",
    "Delegation",
    "DelegationStatus",
    "GovernanceFlag",
    "HookEvent",
    "Lane",
    "LaneMetrics",
    "LaneRole",
    "LaneStatus",
    "Lesson",
    "LessonScope",
    "MessagePriority",
    "NegotiationExchange",
    "NegotiationMove",
    "NegotiationOutcome",
    "NegotiationTrigger",
    "Performative",
    "Plan",
    "PlanStep",
    "ReelManifest",
    "Session",
    "SessionMetrics",
    "SessionStatus",
    "StepStatus",
    "TaskArtifact",
    "TaskState",
    "TrustBoundary",
    "Urgency",
    "can_transition",
    "ensure_aware",
    "id_kind",
    "is_terminal",
    "is_valid_id",
    "new_id",
    "new_ulid",
    "now",
    "ulid_to_datetime",
]
