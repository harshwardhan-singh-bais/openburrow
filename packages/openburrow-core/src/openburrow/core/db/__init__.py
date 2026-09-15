"""Local state.

SQLite, one file per repo, WAL mode. Chosen because the access pattern is
*one writer, many readers, all local* — which is precisely what SQLite is best
at and what a client-server database would only complicate.

The schema has two halves and the distinction is load-bearing:

* **Tables** mirror the domain models and are updated in place — sessions,
  lanes, claims, plans.
* **``bus_events``** is an **append-only log** and is never updated or deleted
  (until retention prunes it). Every feature reads from it: the TUI feed, the
  replay viewer, the audit export, the metrics rollup. Because it mirrors the A2A
  task-lifecycle states, replaying the log reconstructs the session exactly.

Keeping the log authoritative is what makes crash recovery, replay, and audit
all the *same* feature rather than three parallel mechanisms.
"""

from openburrow.core.db.engine import (
    Database,
    create_engine,
    get_database,
    init_database,
    session_scope,
)
from openburrow.core.db.models import (
    ApprovalRow,
    AuditRow,
    BrainRow,
    BusEventRow,
    CheckpointRow,
    ClaimRow,
    DelegationRow,
    LaneRow,
    LessonRow,
    MessageRow,
    NegotiationRow,
    PlanRow,
    SessionRow,
    StepRow,
    TaskRow,
)
from openburrow.core.db.repository import BusEventLog, Repository

__all__ = [
    "ApprovalRow",
    "AuditRow",
    "BrainRow",
    "BusEventLog",
    "BusEventRow",
    "CheckpointRow",
    "ClaimRow",
    "Database",
    "DelegationRow",
    "LaneRow",
    "LessonRow",
    "MessageRow",
    "NegotiationRow",
    "PlanRow",
    "Repository",
    "SessionRow",
    "StepRow",
    "TaskRow",
    "create_engine",
    "get_database",
    "init_database",
    "session_scope",
]
