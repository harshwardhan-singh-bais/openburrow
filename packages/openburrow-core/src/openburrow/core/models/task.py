"""A2A tasks — the unit of delegated work.

Everything one lane asks another lane to do becomes a task with a full
lifecycle, not a fire-and-forget call. That single decision is what makes the
rest of the system possible: the reply-wait mechanic, the delegation ledger, the
governance chain-of-custody, and the replay viewer all read from the same
task records.

The states are A2A's, verbatim. The additions below the fold — delegation
provenance, authority scope, trust boundary — are OpenBurrow's, and they are
exactly the fields the protocol leaves unspecified. That is the gap this project
exists to fill.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from pydantic import Field, computed_field

from openburrow.core.errors import IllegalTaskTransitionError
from openburrow.core.models.base import BurrowModel, ensure_aware, now
from openburrow.core.models.enums import (
    TaskState,
    TrustBoundary,
    can_transition,
    is_terminal,
)


class TaskArtifact(BurrowModel):
    """A concrete output a task produced.

    Artifacts are typed because the translator and the replay viewer both need
    to know what they are looking at without heuristics: a ``diff`` renders one
    way, a ``plan`` another, a ``lesson`` another again.
    """

    id_kind: ClassVar[str] = "task"

    kind: str = "text"  # diff | plan | file | log | test-result | lesson | text | json
    name: str = ""
    #: Inline content for small artifacts (diffs, summaries, verdicts).
    content: str = ""
    #: Repo-relative path for file artifacts.
    path: str = ""
    mime_type: str = "text/plain"
    size_bytes: int = 0
    truncated: bool = False

    @classmethod
    def diff(cls, content: str, *, name: str = "changes.diff") -> TaskArtifact:
        return cls(kind="diff", name=name, content=content, mime_type="text/x-diff")

    @classmethod
    def plan(cls, content: str) -> TaskArtifact:
        return cls(kind="plan", name="plan", content=content, mime_type="application/json")

    @classmethod
    def test_result(cls, content: str, *, name: str = "pytest") -> TaskArtifact:
        return cls(kind="test-result", name=name, content=content)


class A2ATask(BurrowModel):
    """One unit of work delegated from one lane to another.

    Lifecycle::

        submitted ──▶ working ──┬─▶ completed
                                ├─▶ failed
                                ├─▶ canceled
                                ├─▶ input_required ──▶ working
                                └─▶ auth_required  ──▶ working

    Illegal transitions raise rather than warn. A task that silently goes
    ``completed -> working`` would corrupt every metric downstream, and the
    whole point of grounding on A2A is that the lifecycle means something.
    """

    id_kind: ClassVar[str] = "task"

    session_id: str = ""
    thread_id: str = ""

    # --- participants ------------------------------------------------------
    #: Lane that created the task.
    requester_lane: str = ""
    #: Lane expected to do the work.
    assignee_lane: str = ""
    #: Human who authorized the delegation. Empty means "the requester's owner".
    authorized_by: str = ""

    # --- what ---------------------------------------------------------------
    title: str = ""
    instruction: str = ""
    #: Free-form structured input, shaped by the adapter that will receive it.
    input_payload: dict[str, Any] = Field(default_factory=dict)
    #: Skill name from the assignee's Agent Card this task is exercising.
    skill: str = ""
    #: Plan step this task implements, when it came from a plan.
    step_id: str = ""
    #: Parent task, when this is a sub-step (roadmap item 125).
    parent_task_id: str = ""

    # --- lifecycle ---------------------------------------------------------
    state: TaskState = TaskState.SUBMITTED
    state_history: list[dict[str, Any]] = Field(default_factory=list)
    #: Why the task is in ``input_required`` / ``auth_required`` right now.
    blocking_reason: str = ""
    #: The A2A message that must be answered before work resumes.
    pending_message_id: str = ""

    # --- results -----------------------------------------------------------
    artifacts: list[TaskArtifact] = Field(default_factory=list)
    result_summary: str = ""
    error: str = ""

    # --- governance (the OpenBurrow-original part) -------------------------
    delegation_id: str = ""
    trust_boundary: TrustBoundary = TrustBoundary.INTRA_REPO
    #: Bounded subset of the requester's authority this task may exercise.
    authority_scope: list[str] = Field(default_factory=list)
    #: False once a task has been re-delegated — blocks further transfer (item 183).
    transferable: bool = True
    #: Depth in the delegation chain; compared against ``max_delegation_depth``.
    delegation_depth: int = 0

    # --- timing / cost -----------------------------------------------------
    submitted_at: datetime | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    timeout_s: int = 900
    tokens_used: int = 0
    cost_usd: float = 0.0
    retry_count: int = 0

    # ------------------------------------------------------------------ state
    def transition(self, target: TaskState, *, reason: str = "") -> None:
        """Move to ``target``, recording the hop in ``state_history``.

        Raises :class:`IllegalTaskTransitionError` on an illegal hop — including
        any attempt to leave a terminal state.
        """
        if target == self.state:
            return
        if not can_transition(self.state, target):
            raise IllegalTaskTransitionError(
                f"cannot move A2A task {self.id} from '{self.state}' to '{target}'",
                context={
                    "task_id": self.id,
                    "from": str(self.state),
                    "to": str(target),
                    "terminal": is_terminal(self.state),
                },
            )
        self.state_history.append(
            {
                "from": str(self.state),
                "to": str(target),
                "at": now().isoformat(),
                "reason": reason,
            }
        )
        self.state = target

        if target == TaskState.WORKING and self.started_at is None:
            self.started_at = now()
        if target in {TaskState.INPUT_REQUIRED, TaskState.AUTH_REQUIRED}:
            self.blocking_reason = reason
        elif self.blocking_reason:
            self.blocking_reason = ""
        if is_terminal(target):
            self.ended_at = now()
        self.touch()

    def block_on_input(self, message_id: str, reason: str = "") -> None:
        self.pending_message_id = message_id
        self.transition(TaskState.INPUT_REQUIRED, reason=reason or "waiting on a reply")

    def block_on_auth(self, reason: str, *, approval_id: str = "") -> None:
        if approval_id:
            self.metadata["approval_id"] = approval_id
        self.transition(TaskState.AUTH_REQUIRED, reason=reason)

    def unblock(self, *, note: str = "") -> None:
        self.pending_message_id = ""
        self.transition(TaskState.WORKING, reason=note or "unblocked")

    def complete(self, *, summary: str = "", artifacts: list[TaskArtifact] | None = None) -> None:
        if artifacts:
            self.artifacts.extend(artifacts)
        self.result_summary = summary
        self.transition(TaskState.COMPLETED, reason=summary[:200])

    def fail(self, error: str) -> None:
        self.error = error
        self.transition(TaskState.FAILED, reason=error[:200])

    # ------------------------------------------------------------------ views
    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_open(self) -> bool:
        return not is_terminal(self.state)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_blocking(self) -> bool:
        return self.state in {TaskState.INPUT_REQUIRED, TaskState.AUTH_REQUIRED}

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_cross_boundary(self) -> bool:
        return self.trust_boundary not in {TrustBoundary.INTRA_REPO, TrustBoundary.INTRA_LANE}

    @property
    def duration_seconds(self) -> float:
        if self.started_at is None:
            return 0.0
        end = ensure_aware(self.ended_at) if self.ended_at else now()
        return (end - ensure_aware(self.started_at)).total_seconds()

    @property
    def is_timed_out(self) -> bool:
        if self.is_open and self.started_at is not None:
            return self.duration_seconds > self.timeout_s
        if self.is_open and self.submitted_at is not None:
            return (now() - ensure_aware(self.submitted_at)).total_seconds() > self.timeout_s
        return False

    def authority_allows(self, capability: str) -> bool:
        """Does this task's bounded scope permit ``capability``?

        Called by the governance layer before a delegated action runs. An empty
        scope means "nothing was granted", not "everything is granted" — the
        fail-closed reading, because the fail-open reading is how authority
        creep happens.
        """
        return capability in self.authority_scope

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "state": str(self.state),
            "from": self.requester_lane,
            "to": self.assignee_lane,
            "title": self.title,
            "skill": self.skill,
            "depth": self.delegation_depth,
            "duration_s": round(self.duration_seconds, 1),
            "artifacts": len(self.artifacts),
        }


__all__ = ["A2ATask", "TaskArtifact"]
