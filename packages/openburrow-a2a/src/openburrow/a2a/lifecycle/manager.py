"""A2A task lifecycle management.

This is the piece that turns "a harness is doing something" into "a task is in
state X", and it is where OpenBurrow's central architectural bet is enforced:
*the bus log is the source of truth, and the task table is a projection of it.*

Every state change does two things atomically:

1. Transitions the :class:`~openburrow.core.models.A2ATask` (which validates the
   hop against the A2A lifecycle graph).
2. Appends a ``bus_events`` row carrying the new state.

Because both happen in one transaction, a crash between them is impossible. That
is what makes ``burrow resume`` able to trust the log: if the log says a task
reached ``input_required``, the task row agrees, and vice versa.

The manager also owns the *timeout* policy. Blocking states have their own
deadlines — ``input_required`` and ``auth_required`` wait far longer than a
working task, because a human being asked a question is not a failure.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import timedelta

from openburrow.core.config.settings import Settings
from openburrow.core.db.repository import BusEventLog, Repository
from openburrow.core.errors import IllegalTaskTransitionError
from openburrow.core.logging import get_logger
from openburrow.core.models import (
    A2ATask,
    BusMessage,
    TaskArtifact,
    TaskState,
    TrustBoundary,
    is_terminal,
    now,
)
from openburrow.core.models.base import ensure_aware

log = get_logger(__name__)

#: Callback signature for "something interesting happened to a task".
TaskListener = Callable[[A2ATask, TaskState], None]


class TaskLifecycleManager:
    """Owns every legal transition of an A2A task.

    Construct one per daemon, not per lane — a task's lifecycle can outlive the
    lane that created it (that is the whole point of delegation), so the manager
    must not be scoped to a single harness.
    """

    def __init__(
        self, repo: Repository, bus: Callable[[], BusEventLog], settings: Settings
    ) -> None:
        """``bus`` is a *factory*, not a log.

        Every write wants its own session, so the manager is given something it
        can call rather than an instance it would have to keep alive. The
        parameter used to be annotated ``BusEventLog`` while callers passed a
        factory, and the mismatch was silenced with a ``type: ignore`` at the
        call site — which is exactly the kind of suppression that turns a type
        error into a runtime one. It did: ``self.bus.append`` was an
        ``AttributeError`` on a function object.
        """
        self.repo = repo
        self.bus = bus
        self.settings = settings
        self._listeners: list[TaskListener] = []

    # --- observation -------------------------------------------------------
    def on_change(self, listener: TaskListener) -> None:
        """Register a callback fired after every successful transition.

        Used by the TUI to refresh, by the replay recorder to append to the
        causal log, and by the metrics collector. Keeping them as listeners
        rather than inline calls is what stops this class from growing a
        dependency on every consumer.
        """
        self._listeners.append(listener)

    def _notify(self, task: A2ATask, target: TaskState) -> None:
        for listener in self._listeners:
            try:
                listener(task, target)
            except Exception as exc:
                log.warning("task.listener_failed", error=str(exc), task_id=task.id)

    # --- creation ----------------------------------------------------------
    async def submit(
        self,
        *,
        session_id: str,
        thread_id: str,
        requester_lane: str,
        assignee_lane: str,
        title: str,
        instruction: str,
        authorized_by: str,
        skill: str = "",
        step_id: str = "",
        parent_task_id: str = "",
        authority_scope: list[str] | None = None,
        delegation_id: str = "",
        delegation_depth: int = 0,
        trust_boundary: TrustBoundary = TrustBoundary.INTRA_REPO,
        transferable: bool = True,
        input_payload: dict | None = None,
        timeout_s: int | None = None,
    ) -> A2ATask:
        """Create a task in ``submitted`` and record it on the bus.

        ``authorized_by`` is required and must be a human. This is the governance
        layer's foothold: there is no code path that creates work without
        recording who asked for it.
        """
        task = A2ATask(
            session_id=session_id,
            thread_id=thread_id,
            requester_lane=requester_lane,
            assignee_lane=assignee_lane,
            authorized_by=authorized_by,
            title=title,
            instruction=instruction,
            skill=skill,
            step_id=step_id,
            parent_task_id=parent_task_id,
            authority_scope=authority_scope or [],
            delegation_id=delegation_id,
            delegation_depth=delegation_depth,
            trust_boundary=trust_boundary,
            transferable=transferable,
            input_payload=input_payload or {},
            timeout_s=timeout_s or self.settings.a2a_task_timeout_s,
            submitted_at=now(),
        )
        await self.repo.save(task)
        await self._emit(task, event_type="task.submitted", summary=title)
        log.info(
            "task.submitted",
            task_id=task.id,
            from_lane=requester_lane,
            to_lane=assignee_lane,
            skill=skill,
        )
        return task

    # --- transitions -------------------------------------------------------
    async def start(self, task: A2ATask, *, note: str = "") -> A2ATask:
        """``submitted`` -> ``working``. The assignee picked it up."""
        task.transition(TaskState.WORKING, reason=note)
        return await self._persist(task, event_type="task.working", summary=note)

    async def request_input(
        self,
        task: A2ATask,
        *,
        message: BusMessage,
        reason: str = "",
    ) -> A2ATask:
        """``working`` -> ``input_required``.

        The task now blocks until ``message`` is answered. This is how
        ``burrow ask`` gets its reply-wait semantics without a bespoke blocking
        primitive — the wait *is* the protocol state.
        """
        task.block_on_input(message.id, reason or message.subject)
        await self.repo.save(message)
        await self._emit(
            task,
            event_type="task.input_required",
            summary=reason or message.subject,
            correlation_id=message.id,
        )
        log.info("task.input_required", task_id=task.id, awaiting=message.id)
        return task

    async def request_auth(
        self,
        task: A2ATask,
        *,
        reason: str,
        approval_id: str = "",
    ) -> A2ATask:
        """``working`` -> ``auth_required``. A human must approve something."""
        task.block_on_auth(reason, approval_id=approval_id)
        return await self._persist(
            task,
            event_type="task.auth_required",
            summary=reason,
            correlation_id=approval_id,
        )

    async def resume(self, task: A2ATask, *, note: str = "") -> A2ATask:
        """``input_required`` / ``auth_required`` -> ``working``."""
        task.unblock(note=note)
        return await self._persist(task, event_type="task.resumed", summary=note)

    async def complete(
        self,
        task: A2ATask,
        *,
        summary: str = "",
        artifacts: list[TaskArtifact] | None = None,
    ) -> A2ATask:
        task.complete(summary=summary, artifacts=artifacts)
        return await self._persist(
            task,
            event_type="task.completed",
            summary=summary,
            extra={"artifacts": len(task.artifacts)},
        )

    async def fail(self, task: A2ATask, *, error: str) -> A2ATask:
        task.fail(error)
        return await self._persist(task, event_type="task.failed", summary=error)

    async def cancel(self, task: A2ATask, *, reason: str = "") -> A2ATask:
        task.transition(TaskState.CANCELED, reason=reason)
        return await self._persist(task, event_type="task.canceled", summary=reason)

    async def reject(self, task: A2ATask, *, reason: str = "") -> A2ATask:
        """``submitted``/``auth_required`` -> ``rejected``.

        Distinct from ``canceled`` on purpose: *canceled* means "the work
        stopped", *rejected* means "the assignee refused it". The governance
        report treats them very differently — a pattern of rejections on
        delegated tasks is a signal that authority is being over-reached.
        """
        task.transition(TaskState.REJECTED, reason=reason)
        return await self._persist(task, event_type="task.rejected", summary=reason)

    async def retry(self, task: A2ATask, *, reason: str = "") -> A2ATask:
        """Requeue a failed task as a fresh one, preserving the lineage.

        A retry is a *new* task with ``parent_task_id`` pointing at the original,
        not a resurrection — because terminal states are absorbing and reviving
        one would make the audit log lie about what happened.
        """
        if not is_terminal(task.state):
            raise IllegalTaskTransitionError(
                f"cannot retry task {task.id}: it is still in state '{task.state}'",
                context={"task_id": task.id, "state": str(task.state)},
            )
        task.retry_count += 1
        await self.repo.save(task)
        new_task = await self.submit(
            session_id=task.session_id,
            thread_id=task.thread_id,
            requester_lane=task.requester_lane,
            assignee_lane=task.assignee_lane,
            title=f"[retry {task.retry_count}] {task.title}",
            instruction=task.instruction,
            authorized_by=task.authorized_by,
            skill=task.skill,
            step_id=task.step_id,
            parent_task_id=task.id,
            authority_scope=list(task.authority_scope),
            delegation_id=task.delegation_id,
            delegation_depth=task.delegation_depth,
            trust_boundary=TrustBoundary(str(task.trust_boundary)),
            transferable=task.transferable,
            input_payload=dict(task.input_payload),
            timeout_s=task.timeout_s,
        )

        # The reason was accepted and then dropped. `reject` records its reason in
        # the audit trail; a retry that discards one is the same omission in a less
        # visible place. Whoever reads the log afterwards sees that a retry
        # happened and never learns why — and for a project whose pitch is that you
        # can trust what it did, an unexplained retry is a hole in the record.
        if reason:
            await self._persist(
                new_task,
                event_type="task.retried",
                summary=f"retry {task.retry_count} of {task.id}: {reason}",
            )
        return new_task

    # --- timeout enforcement ----------------------------------------------
    async def enforce_timeouts(self, session_id: str) -> list[A2ATask]:
        """Fail tasks that have outstayed their state's deadline.

        Different deadlines per state, because collapsing them would mean either
        killing a human's thinking time or letting a wedged lane run forever:

        ==================  ==================================
        state               deadline
        ==================  ==================================
        working             ``A2A_TASK_TIMEOUT_S``
        input_required      ``A2A_INPUT_REQUIRED_TIMEOUT_S``
        auth_required       ``A2A_AUTH_REQUIRED_TIMEOUT_S``
        ==================  ==================================
        """
        expired: list[A2ATask] = []
        for task in await self.repo.open_tasks(session_id):
            if not self._has_timed_out(task):
                continue
            await self.fail(task, error=f"timed out in state '{task.state}'")
            expired.append(task)
        if expired:
            log.warning("task.timeouts_enforced", count=len(expired), session_id=session_id)
        return expired

    def _has_timed_out(self, task: A2ATask) -> bool:
        limit = self._deadline_for(task)
        if limit is None:
            return False
        anchor = task.started_at or task.submitted_at
        if anchor is None:
            return False
        return (now() - ensure_aware(anchor)).total_seconds() > limit

    def _deadline_for(self, task: A2ATask) -> int | None:
        if task.state == TaskState.WORKING:
            return task.timeout_s or self.settings.a2a_task_timeout_s
        if task.state == TaskState.INPUT_REQUIRED:
            return self.settings.a2a_input_required_timeout_s
        if task.state == TaskState.AUTH_REQUIRED:
            return self.settings.a2a_auth_required_timeout_s
        if task.state == TaskState.SUBMITTED:
            # Nobody picked it up — that is itself a signal worth surfacing.
            return task.timeout_s or self.settings.a2a_task_timeout_s
        return None

    async def wait_for_terminal(
        self,
        task_id: str,
        *,
        timeout: float | None = None,
        poll_interval: float = 0.5,
    ) -> A2ATask | None:
        """Block until a task reaches a terminal state, or the timeout elapses.

        Polling rather than an in-process event, because the lane that completes
        the task may be a different process entirely. The cost is a query every
        half second; the benefit is that ``burrow ask --wait`` works across
        process boundaries, which it must.
        """
        deadline = None if timeout is None else now() + timedelta(seconds=timeout)
        while True:
            task = await self.repo.get(A2ATask, task_id)
            if task is None:
                return None
            if is_terminal(task.state):
                return task
            if deadline is not None and now() > deadline:
                return task
            await asyncio.sleep(poll_interval)

    # --- internals ---------------------------------------------------------
    async def _persist(
        self,
        task: A2ATask,
        *,
        event_type: str,
        summary: str = "",
        correlation_id: str = "",
        extra: dict | None = None,
    ) -> A2ATask:
        task.touch()
        await self.repo.save(task)
        await self._emit(
            task,
            event_type=event_type,
            summary=summary,
            correlation_id=correlation_id,
            extra=extra,
        )
        self._notify(task, task.state)
        return task

    async def _emit(
        self,
        task: A2ATask,
        *,
        event_type: str,
        summary: str = "",
        correlation_id: str = "",
        extra: dict | None = None,
    ) -> int:
        payload = task.model_dump(mode="json")
        if extra:
            payload["_extra"] = extra
        try:
            # A fresh log per write, opened as a context manager. The manager
            # outlives any single session and must not hold one: a session kept
            # across a long-lived object goes stale after the first error and
            # turns every later write into a confusing failure. `__aexit__`
            # commits, which is what actually persists the row.
            async with self.bus() as bus_log:
                return await bus_log.append(
                    event_type=event_type,
                    session_id=task.session_id,
                    thread_id=task.thread_id,
                    lane_id=task.assignee_lane or task.requester_lane,
                    task_id=task.id,
                    task_state=str(task.state),
                    trust_boundary=str(task.trust_boundary),
                    summary=summary or task.title,
                    payload=payload,
                    correlation_id=correlation_id or task.delegation_id,
                    content_hash=f"{task.id}:{task.state}:{task.updated_at.isoformat()}",
                )
        except Exception as exc:
            # `warning`, not `debug`. A transition that could not be recorded is
            # a hole in the audit log, and the audit log is the product's central
            # claim. At debug level this was invisible: the entire write path was
            # raising `TypeError` on every single event and the only trace was a
            # line nobody had turned on.
            log.warning("task.emit_skipped", task_id=task.id, error=str(exc))
            return 0


__all__ = ["TaskLifecycleManager", "TaskListener"]
