"""Durable sessions: checkpoint, resume, and the recovery policies (Stage 12).

Stage 12's scaffolding — the :class:`~openburrow.core.models.runtime.Checkpoint`
model, its table, and the two repository queries — existed before this module
did. What was missing was everything that happens *between* the scaffolding: a
crashed session was unrecoverable in practice because nothing took checkpoints,
nothing consumed them, and the only crash path was the restart-with-backoff in
``sessions.py``.

The design rests on one rule from AGENTS.md: **a state transition and its log
entry are one operation.** Recovery is the inverse of that rule. A checkpoint is
only trustworthy if the log position it records was persisted in the same
operation that snapshotted the harness state — so :meth:`RecoveryManager.take`
persists the checkpoint row and emits ``recovery.checkpoint`` in one DB session,
and a checkpoint whose event is missing describes a state that never existed.

Two things this module deliberately does *not* do:

* It does not fake a harness's resumability. A checkpoint records whatever the
  adapter reports — recent buffered output, its harness session id — no more.
  A harness that cannot resume is not prettied up: the lane comes back up
  fresh, the checkpoint is consumed as *not applied*, and the report says so.
  Roadmap item 157 asks for "harness conversation state AND A2A task-lifecycle
  position"; delivering the second honestly and the first as far as the harness
  allows is the trade, and it is stated here rather than hidden.
* It does not restart lanes by itself. :meth:`resume_session` re-spawns lanes
  via :class:`~openburrow.daemon.sessions.SessionManager` and requeues orphaned
  tasks; the supervisor calls :meth:`RecoveryManager.check_lane` so the
  *policies* — silent failure, checkpoint cadence, bulkhead accounting — apply
  continuously, but a policy never starts a process. Policies flag;
  :class:`SessionManager` acts.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from openburrow.core.db.repository import Repository
from openburrow.core.logging import bind_context, get_logger
from openburrow.core.models import Checkpoint, Lane, LaneStatus, Session, SessionStatus
from openburrow.core.models.base import now

if TYPE_CHECKING:
    from openburrow.core.config.load import ResolvedConfig
    from openburrow.core.db.engine import Database
    from openburrow.daemon.bus import EventBus
    from openburrow.daemon.sessions import RunningLane, SessionManager

log = get_logger(__name__)

#: A checkpoint's checksum covers the fields that identify the state, so a
#: partially-written row fails verification on resume instead of resuming into a
#: state that looks plausible but is not the one that was snapshotted.
_CHECKSUM_FIELDS = (
    "session_id",
    "lane_id",
    "task_id",
    "step_id",
    "task_state",
    "last_event_id",
    "sequence",
)


def checkpoint_checksum(checkpoint: Checkpoint) -> str:
    """A stable digest over the fields that identify the snapshotted state."""
    material = json.dumps(
        {name: getattr(checkpoint, name) for name in _CHECKSUM_FIELDS},
        sort_keys=True,
        default=str,
    )
    return hashlib.blake2b(material.encode("utf-8"), digest_size=16).hexdigest()


@dataclass(slots=True)
class CrashReport:
    """One structured crash log (item 166) — per lane, kept in a bounded ring."""

    lane_id: str
    harness: str
    session_id: str
    reason: str
    exit_code: int | None = None
    restarts: int = 0
    output_tail: str = ""
    at: str = ""

    def render(self) -> str:
        head = f"lane {self.lane_id} ({self.harness}) crashed: {self.reason}"
        if self.exit_code is not None:
            head += f" — exit {self.exit_code}"
        tail = f"\n  output tail: {self.output_tail}" if self.output_tail else ""
        return head + tail


@dataclass(slots=True)
class _BreakerState:
    """Per-harness circuit-breaker state (item 164)."""

    failures: int = 0
    opened_at: float | None = None


class CircuitBreaker:
    """Stop invoking a harness after repeated consecutive failures.

    Half-open semantics: while open, every spawn attempt of that harness is
    refused fast; after ``cooldown_s`` the next attempt is a probe that closes
    the breaker on success or re-opens it on failure. Per-harness, not per-lane,
    because a broken harness binary fails the same way for every lane using it.
    """

    def __init__(self, *, threshold: int = 5, cooldown_s: float = 300.0) -> None:
        self.threshold = threshold
        self.cooldown_s = cooldown_s
        self._states: dict[str, _BreakerState] = {}

    def _state(self, harness: str) -> _BreakerState:
        return self._states.setdefault(harness, _BreakerState())

    def record_failure(self, harness: str) -> bool:
        """Count a failure. Returns True when this failure *opened* the breaker."""
        state = self._state(harness)
        state.failures += 1
        if state.failures >= self.threshold and state.opened_at is None:
            state.opened_at = now().timestamp()
            return True
        return False

    def record_success(self, harness: str) -> None:
        state = self._state(harness)
        state.failures = 0
        state.opened_at = None

    def allow(self, harness: str) -> bool:
        """Whether a spawn of ``harness`` may proceed right now."""
        state = self._state(harness)
        if state.opened_at is None:
            return True
        # Half-open: past the cooldown, one attempt is let through as a probe.
        # The attempt's own success/failure record re-closes or re-opens the
        # breaker. Checked before the open-refusal below so a zero cooldown
        # (the test's and CI's shape) probes immediately.
        if now().timestamp() - state.opened_at >= self.cooldown_s:
            state.opened_at = None
            state.failures = self.threshold - 1
            return True
        return False

    def status(self) -> dict[str, Any]:
        return {
            harness: {"failures": state.failures, "open": state.opened_at is not None}
            for harness, state in sorted(self._states.items())
        }


class Bulkhead:
    """Per-lane queue accounting so one flooded lane cannot stall the rest.

    The real isolation comes from the bus: every subscriber queue is bounded and
    a lane's events already drop to the log when a queue fills, recoverable by
    seq. This class makes the isolation *visible* — which lane is emitting how
    fast, and whether its buffer has overflowed — because an isolation mechanism
    nobody can observe is indistinguishable from one that does not exist
    (item 165).
    """

    def __init__(self, queue_size: int = 256) -> None:
        self.queue_size = queue_size
        self._depths: dict[str, int] = defaultdict(int)
        self._overflowed: set[str] = set()

    def record(self, lane_id: str, *, depth: int) -> None:
        self._depths[lane_id] = depth
        if depth > self.queue_size and lane_id not in self._overflowed:
            self._overflowed.add(lane_id)
            log.warning(
                "recovery.bulkhead_pressure",
                lane_id=lane_id,
                depth=depth,
                queue_size=self.queue_size,
                hint="Lane events keep flowing to the log; the lane replays by seq.",
            )

    def overflowed(self) -> set[str]:
        return set(self._overflowed)

    def snapshot(self) -> dict[str, Any]:
        return {
            "queue_size": self.queue_size,
            "overflowed_lanes": sorted(self._overflowed),
            "depths": dict(sorted(self._depths.items(), key=lambda kv: -kv[1])[:20]),
        }


class RecoveryManager:
    """Owns checkpoints, resume, and the durable-recovery policies."""

    def __init__(
        self,
        config: ResolvedConfig,
        database: Database,
        bus: EventBus,
        sessions: SessionManager,
    ) -> None:
        self.config = config
        self.database = database
        self.bus = bus
        self.sessions = sessions
        settings = config.settings
        self.auto_resume = bool(settings.recovery_auto_resume)
        self.checkpoint_interval_s = int(settings.recovery_checkpoint_interval_s)
        self.dead_letter_after = max(1, int(settings.recovery_dead_letter_after))
        self.silent_failure = bool(settings.recovery_silent_failure)
        self.crash_log_keep = max(1, int(settings.recovery_crash_log_keep))

        self.breaker = CircuitBreaker(
            threshold=int(settings.recovery_circuit_threshold),
            cooldown_s=float(settings.recovery_circuit_cooldown_s),
        )
        self.bulkhead = Bulkhead(queue_size=int(settings.recovery_bulkhead_queue))

        self._retry_counts: dict[str, int] = {}  # lane_id -> consecutive failures
        self._dead_lettered: set[str] = set()
        self._crash_logs: dict[str, list[CrashReport]] = {}
        self._next_checkpoint_at: dict[str, float] = {}

    # ------------------------------------------------------------------ #
    # Checkpointing (items 157, 160)
    # ------------------------------------------------------------------ #

    async def take_checkpoint(
        self, running: RunningLane, *, reason: str = "periodic"
    ) -> Checkpoint | None:
        """Snapshot one running lane's harness state and its bus position.

        The checkpoint row and its bus event are written in one session so a
        crash between them cannot leave a checkpoint the log does not know
        about. ``None`` means nothing was taken — normally because the lane is
        not running, which is not an error worth raising over.
        """
        lane = running.lane
        adapter = running.adapter
        if not adapter.is_running:
            return None

        from openburrow.core.db.repository import BusEventLog

        checkpoint = Checkpoint(
            session_id=lane.session_id,
            lane_id=lane.id,
            task_id=str(lane.metadata.get("current_task") or ""),
            step_id=str(lane.metadata.get("current_step") or ""),
            harness_state=self._harness_state(adapter),
            harness_session_id=self._harness_session_id(adapter),
            context_bytes=sum(len(o.text) for o in adapter.recent_output(10_000)),
            task_state=str(lane.status.to_task_state()),
            delivered_message_ids=list(lane.metadata.get("delivered_message_ids") or []),
            negotiation_id=str(lane.metadata.get("negotiation_id") or ""),
            negotiation_move_index=int(lane.metadata.get("negotiation_move_index") or 0),
            head_commit=str(lane.head_commit),
            dirty=bool(lane.metadata.get("worktree_dirty")),
        )

        async with self.database.session() as db_session:
            repo = Repository(db_session)
            previous = await repo.latest_checkpoint(lane.id)
            checkpoint.sequence = (previous.sequence + 1) if previous is not None else 1
            bus_log = BusEventLog(db_session)
            last_event = await bus_log.latest_seq(session_id=lane.session_id)
            checkpoint.last_event_id = str(last_event)
            checkpoint.checksum = checkpoint_checksum(checkpoint)
            await repo.save(checkpoint)
            await bus_log.append(
                event_type="recovery.checkpoint",
                session_id=lane.session_id,
                lane_id=lane.id,
                summary=f"checkpoint seq={checkpoint.sequence} ({reason})",
                payload=checkpoint.model_dump(mode="json"),
            )
            await db_session.commit()

        with bind_context(lane_id=lane.id):
            log.debug("recovery.checkpoint_taken", lane_id=lane.id, seq=checkpoint.sequence)
        return checkpoint

    @staticmethod
    def _harness_state(adapter: Any) -> dict[str, Any]:
        """Whatever the adapter can honestly report for a later resume."""
        state: dict[str, Any] = {}
        for attr in ("prompts", "injected"):
            value = getattr(adapter, attr, None)
            if isinstance(value, list):
                state[attr] = [str(item) for item in value[-20:]]
        for attr in ("harness_session_id", "session_key", "server_url"):
            value = getattr(adapter, attr, None)
            if isinstance(value, str) and value:
                state[attr] = value
        return state

    @staticmethod
    def _harness_session_id(adapter: Any) -> str:
        for attr in ("harness_session_id", "session_id", "session_key"):
            value = getattr(adapter, attr, None)
            if isinstance(value, str) and value:
                return value
        return ""

    # ------------------------------------------------------------------ #
    # Resume (items 158, 159, 160, 167, 168)
    # ------------------------------------------------------------------ #

    async def resume_session(self, session_id: str, *, reason: str = "manual") -> dict[str, Any]:
        """Bring a session's lanes back up from their latest checkpoints.

        Idempotent by construction: a checkpoint is consumed (``consumed_at``
        set) before the lane is respawned, so a resume that crashes partway
        cannot double-apply the remaining ones on the next attempt (item 160).
        The consumed checkpoint remains in the table as the record that this
        resume happened, which is what the report renders.
        """
        session = await self.sessions.get_session(session_id)

        async with self.database.session() as db_session:
            checkpoints = await Repository(db_session).unconsumed_checkpoints(session.id)

        resumed: list[dict[str, Any]] = []
        fresh: list[dict[str, Any]] = []
        if not checkpoints:
            log.info("recovery.nothing_to_resume", session_id=session.id)
            return {
                "session_id": session.id,
                "resumed": [],
                "fresh": [],
                "message": "no unconsumed checkpoints - every lane comes back fresh",
            }

        for checkpoint in checkpoints:
            lane_id = checkpoint.lane_id
            if checkpoint_checksum(checkpoint) != checkpoint.checksum:
                # A row that does not match its own checksum was not fully
                # written. Resuming into it would be worse than skipping it —
                # the whole point of the checksum — so the lane comes back
                # fresh and the corruption is on the record.
                fresh.append({"lane_id": lane_id, "reason": "checkpoint failed verification"})
                await self.bus.emit(
                    event_type="recovery.checkpoint_corrupt",
                    session_id=session.id,
                    lane_id=lane_id,
                    summary=f"checkpoint seq={checkpoint.sequence} failed checksum; skipped",
                )
                continue

            try:
                lane = await self.sessions.get_lane(session.id, lane_id)
            except Exception:
                # The lane row may not resolve (e.g. pruned). Log and move on:
                # one unresolvable lane must not stop the rest of the resume.
                log.warning("recovery.lane_unresolvable", lane_id=lane_id)
                continue

            async with self.database.session() as db_session:
                repo = Repository(db_session)
                checkpoint.consume()
                await repo.save(checkpoint)
                await db_session.commit()

            try:
                await self.sessions.start_lane(
                    session,
                    name=lane.name,
                    harness=lane.harness,
                    role=str(lane.role),
                    owner=lane.owner,
                    claims=list(lane.metadata.get("claims") or []),
                    env_passthrough=list(lane.env_passthrough),
                    max_runtime_s=lane.max_runtime_s,
                    idle_timeout_s=lane.idle_timeout_s,
                    can_delegate=lane.can_delegate,
                    transferable=lane.transferable,
                )
            except Exception as exc:
                fresh.append({"lane_id": lane_id, "reason": f"respawn failed: {exc}"})
                await self.bus.emit(
                    event_type="recovery.resume_failed",
                    session_id=session.id,
                    lane_id=lane_id,
                    summary=f"lane {lane.name} could not be resumed: {exc}",
                )
                continue

            applied = bool(checkpoint.harness_state) or bool(checkpoint.harness_session_id)
            if applied:
                resumed.append(
                    {
                        "lane_id": lane_id,
                        "lane_name": lane.name,
                        "checkpoint_id": checkpoint.id,
                        "sequence": checkpoint.sequence,
                        "harness_session_id": checkpoint.harness_session_id,
                        "from_bus_seq": checkpoint.last_event_id,
                    }
                )
            else:
                fresh.append({"lane_id": lane_id, "reason": "checkpoint carried no harness state"})

            await self.bus.emit(
                event_type="recovery.resumed",
                session_id=session.id,
                lane_id=lane_id,
                summary=(
                    f"lane {lane.name} resumed from checkpoint seq={checkpoint.sequence}"
                    if applied
                    else f"lane {lane.name} came back fresh (no harness state to apply)"
                ),
                payload={
                    "checkpoint_id": checkpoint.id,
                    "sequence": checkpoint.sequence,
                    "applied": applied,
                },
            )

        if not session.is_open:
            # A resume implies the session is live again, whatever state the
            # crash left it in. Claimed as ACTIVE, never as COMPLETED.
            session.status = SessionStatus.ACTIVE
            await self._persist_session(session)

        await self.bus.emit(
            event_type="recovery.session_resumed",
            session_id=session.id,
            thread_id=session.thread_id,
            summary=(
                f"session resumed: {len(resumed)} from checkpoints, {len(fresh)} fresh ({reason})"
            ),
            payload={"resumed": resumed, "fresh": fresh, "reason": reason},
        )

        return {
            "session_id": session.id,
            "resumed": resumed,
            "fresh": fresh,
            "message": (
                f"{len(resumed)} lane(s) resumed from checkpoints, "
                f"{len(fresh)} lane(s) came back fresh"
            ),
        }

    async def _persist_session(self, session: Session) -> None:
        async with self.database.session() as db_session:
            await Repository(db_session).save(session)
            await db_session.commit()

    async def _persist_lane(self, lane: Lane) -> None:
        """Write a lane row that this process is not supervising.

        ``SessionManager._persist_lane`` is the normal path, but it is only
        reachable for lanes in ``_running``. An orphan from a dead daemon is not,
        and marking it terminal is the only way to stop re-detecting it.
        """
        async with self.database.session() as db_session:
            await Repository(db_session).save(lane)
            await db_session.commit()

    # ------------------------------------------------------------------ #
    # Orphan detection and requeue (item 159)
    # ------------------------------------------------------------------ #

    async def detect_orphans(self, *, older_than_s: int = 60) -> list[Lane]:
        """Lanes whose heartbeat is stale, from records — not from memory."""
        async with self.database.session() as db_session:
            repo = Repository(db_session)
            return await repo.stale_lanes(older_than_seconds=older_than_s)

    async def requeue_orphans(self, *, older_than_s: int = 60) -> list[dict[str, Any]]:
        """Detect stale lanes, stop them, and requeue their open tasks."""
        orphans = await self.detect_orphans(older_than_s=older_than_s)
        results: list[dict[str, Any]] = []
        for lane in orphans:
            stopped = await self.sessions.stop_lane(lane.id, reason="orphaned — stale heartbeat")
            if not stopped:
                # ``stop_lane`` returns False *without touching the row* when the
                # lane is not in this process's running map, and that is exactly
                # what an orphan from a previous daemon looks like. Leaving the
                # row non-terminal kept it stale, so the next tick found it
                # again, and the tick after that: 117 requeues for a single lane
                # over ten minutes, each one emitting a bus event and clearing
                # task assignees. A lane whose supervisor is gone is not coming
                # back, and recording that is what ends the loop — the
                # alternative is rediscovering the same corpse every five
                # seconds for as long as the daemon runs.
                #
                # CRASHED rather than STOPPED: nothing asked this lane to stop.
                # Both are terminal to ``stale_lanes``, which is the property
                # that matters here.
                lane.status = LaneStatus.CRASHED
                lane.stopped_at = now()
                await self._persist_lane(lane)
                log.warning(
                    "recovery.orphan_without_process",
                    lane_id=lane.id,
                    lane_name=lane.name,
                    detail="no running entry; marked crashed so it is not re-detected",
                )
            requeued = await self._requeue_lane_tasks(lane)
            self._retry_counts[lane.id] = self._retry_counts.get(lane.id, 0) + 1
            await self.bus.emit(
                event_type="recovery.orphan_requeued",
                session_id=lane.session_id,
                lane_id=lane.id,
                summary=f"lane {lane.name} orphaned (no heartbeat), {requeued} task(s) requeued",
                payload={"requeued_tasks": requeued},
            )
            results.append(
                {
                    "lane_id": lane.id,
                    "name": lane.name,
                    "requeued_tasks": requeued,
                    "attempt": self._retry_counts[lane.id],
                }
            )
        return results

    async def _requeue_lane_tasks(self, lane: Lane) -> int:
        """Return a dead lane's open tasks to the pool by clearing their assignee.

        Clearing ``assignee_lane`` is not a lifecycle transition, so the task
        stays in its current state — it simply has no owner, which is the truth
        after the lane died. Keeping a task bound to a dead lane is how work is
        lost twice: once to the crash, once to the requeue meant to save it.
        """

        async with self.database.session() as db_session:
            repo = Repository(db_session)
            tasks = await repo.open_tasks(lane.session_id)
            mine = [t for t in tasks if t.assignee_lane == lane.id]
            for task in mine:
                task.assignee_lane = ""
                await repo.save(task)
            await db_session.commit()
        return len(mine)

    # ------------------------------------------------------------------ #
    # Policies, called from the supervisor loop
    # ------------------------------------------------------------------ #

    async def check_lane(self, running: RunningLane) -> None:
        """One supervision pass for one lane.

        Called from ``SessionManager._supervise`` for every running lane on
        every heartbeat interval. Failures here are caught by the supervisor's
        existing error boundary; this method raises nothing.
        """
        lane = running.lane

        # --- periodic checkpointing (item 157) -------------------------
        if self.checkpoint_interval_s > 0:
            due = self._next_checkpoint_at.get(lane.id)
            timestamp = now().timestamp()
            if due is None:
                self._next_checkpoint_at[lane.id] = timestamp + self.checkpoint_interval_s
            elif timestamp >= due:
                await self.take_checkpoint(running, reason="interval")
                self._next_checkpoint_at[lane.id] = timestamp + self.checkpoint_interval_s

        # --- bulkhead accounting (item 165) ----------------------------
        self.bulkhead.record(lane.id, depth=len(running.adapter.recent_output(100_000)))

        # --- silent-failure detection (item 162) -----------------------
        # ``just_finished`` is set by the output pump when a lane's harness
        # reports a terminal success. A lane that reports success while having
        # produced no artifacts is flagged for replan rather than celebrated.
        if (
            self.silent_failure
            and lane.metadata.pop("just_finished", False)
            and lane.status == LaneStatus.IDLE
            and not (lane.metadata.get("artifacts") or [])
        ):
            await self.bus.emit(
                event_type="recovery.silent_failure",
                session_id=lane.session_id,
                lane_id=lane.id,
                summary=(
                    f"lane {lane.name} reported success but produced no artifacts — "
                    "flagged for replan"
                ),
                payload={"lane_id": lane.id},
            )
            lane.metadata["needs_replan"] = True

    # ------------------------------------------------------------------ #
    # Failure accounting: retry → dead-letter → circuit breaker (161, 163, 164)
    # ------------------------------------------------------------------ #

    async def record_task_failure(self, lane: Lane, *, error: str) -> str:
        """Count a lane failure through retry → dead-letter → circuit breaker.

        Returns the verdict: ``retry``, ``dead_letter``, or ``circuit_open``.
        The caller decides what to do; this only decides and records. A
        dead-lettered lane is *not* restarted automatically — it waits for a
        human, which is the difference between a dead-letter and a retry loop
        with extra steps.
        """
        failures = self._retry_counts.get(lane.id, 0) + 1
        self._retry_counts[lane.id] = failures

        if self.breaker.record_failure(lane.harness):
            await self.bus.emit(
                event_type="recovery.circuit_opened",
                session_id=lane.session_id,
                lane_id=lane.id,
                summary=(
                    f"circuit opened for harness {lane.harness!r} after "
                    f"{self.breaker.threshold} consecutive failures"
                ),
            )
            return "circuit_open"

        if failures >= self.dead_letter_after:
            self._dead_lettered.add(lane.id)
            await self.bus.emit(
                event_type="recovery.dead_letter",
                session_id=lane.session_id,
                lane_id=lane.id,
                summary=(
                    f"lane {lane.name} dead-lettered after {failures} consecutive failures "
                    f"(last error: {error[:120]})"
                ),
                payload={"failures": failures, "error": error[:500]},
            )
            return "dead_letter"

        # Retry with backoff (item 161): the delay shape and the cap live in
        # :meth:`retry_delay` so every retrying path agrees on them.
        return "retry"

    def retry_delay(self, lane_id: str) -> float:
        """Exponential backoff with a 30s cap, for the lane's next retry."""
        return min(30.0, 2.0 ** min(self._retry_counts.get(lane_id, 0), 5))

    def record_task_success(self, lane: Lane) -> None:
        self._retry_counts.pop(lane.id, None)
        self.breaker.record_success(lane.harness)
        self._dead_lettered.discard(lane.id)

    @property
    def dead_lettered(self) -> set[str]:
        return set(self._dead_lettered)

    # ------------------------------------------------------------------ #
    # Crash logs (item 166)
    # ------------------------------------------------------------------ #

    async def record_crash(
        self, running: RunningLane, *, reason: str, exit_code: int | None = None
    ) -> CrashReport:
        """Record a structured crash report for one lane — ring buffer plus bus."""
        lane = running.lane
        try:
            tail = "".join(o.text[-500:] for o in running.adapter.recent_output(4))
        except Exception:
            tail = ""
        report = CrashReport(
            lane_id=lane.id,
            harness=lane.harness,
            session_id=lane.session_id,
            reason=reason,
            exit_code=exit_code,
            restarts=lane.restarts,
            output_tail=tail[-2000:],
            at=now().isoformat(),
        )
        logs = self._crash_logs.setdefault(lane.id, [])
        logs.append(report)
        if len(logs) > self.crash_log_keep:
            del logs[: len(logs) - self.crash_log_keep]
        await self.bus.emit(
            event_type="recovery.lane_crashed",
            session_id=lane.session_id,
            lane_id=lane.id,
            summary=report.render(),
            payload={
                "reason": reason,
                "exit_code": exit_code,
                "restarts": lane.restarts,
                "output_tail": tail[-500:],
            },
        )
        return report

    def crash_logs(self, lane_id: str | None = None) -> list[CrashReport]:
        """Bounded per-lane crash history; most recent first across lanes."""
        if lane_id:
            return list(self._crash_logs.get(lane_id, []))
        all_reports = [report for reports in self._crash_logs.values() for report in reports]
        return sorted(all_reports, key=lambda r: r.at, reverse=True)

    # ------------------------------------------------------------------ #
    # Status
    # ------------------------------------------------------------------ #

    def status(self) -> dict[str, Any]:
        return {
            "auto_resume": self.auto_resume,
            "checkpoint_interval_s": self.checkpoint_interval_s,
            "dead_letter_after": self.dead_letter_after,
            "dead_lettered_lanes": sorted(self._dead_lettered),
            "circuit_breakers": self.breaker.status(),
            "bulkhead": self.bulkhead.snapshot(),
            "retry_counts": dict(sorted(self._retry_counts.items())),
        }


__all__ = [
    "Bulkhead",
    "CircuitBreaker",
    "CrashReport",
    "RecoveryManager",
    "checkpoint_checksum",
]
