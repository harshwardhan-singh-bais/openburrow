"""Session and lane orchestration.

:class:`SessionManager` is where a session stops being a data structure and
becomes running processes. It owns the lifecycle:

1. Create the session row and the git branch.
2. For each lane: create a worktree, spawn the harness, start its A2A server,
   register it on the bus, begin watching its worktree.
3. Keep them alive: heartbeats, crash detection, restart policy.
4. Tear them down: stop harnesses, close A2A servers, clean worktrees.

Everything that touches an external process lives here, and nothing above this
module spawns anything. That containment is what makes ``burrow daemon restart``
safe: there is exactly one place that can leak a process.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
from dataclasses import dataclass
from pathlib import Path

from openburrow.a2a.lifecycle import TaskLifecycleManager
from openburrow.a2a.server import LaneA2AServer
from openburrow.adapters import AdapterRegistry, HarnessAdapter, SpawnSpec
from openburrow.core.config.load import ResolvedConfig
from openburrow.core.db.engine import Database
from openburrow.core.db.repository import AuditLog, BusEventLog, Repository
from openburrow.core.errors import (
    AdapterError,
    ConfigError,
    LaneNotFoundError,
    PolicyViolation,
    SessionNotFoundError,
)
from openburrow.core.logging import bind_context, get_logger
from openburrow.core.models import (
    Lane,
    LaneRole,
    LaneStatus,
    Session,
    SessionStatus,
    TrustBoundary,
    now,
)
from openburrow.daemon.bus import EventBus
from openburrow.daemon.filewatch import WatcherPool
from openburrow.governance import DelegationLedger, PolicyGate

log = get_logger(__name__)

#: How often to check lane heartbeats and restart crashed harnesses.
HEARTBEAT_INTERVAL_S = 5.0


@dataclass(slots=True)
class RunningLane:
    """A lane plus everything keeping it alive."""

    lane: Lane
    adapter: HarnessAdapter
    server: LaneA2AServer | None = None
    reader_task: asyncio.Task[None] | None = None

    @property
    def lane_id(self) -> str:
        return self.lane.id


class SessionManager:
    """Owns running sessions and their lanes."""

    def __init__(
        self,
        config: ResolvedConfig,
        database: Database,
        bus: EventBus,
        registry: AdapterRegistry,
        *,
        human_id: str = "",
        human_email: str = "",
    ) -> None:
        self.config = config
        self.database = database
        self.bus = bus
        self.registry = registry
        self.human_id = human_id or config.settings.governance_human_id
        self.human_email = human_email or config.settings.governance_human_email

        self._running: dict[str, RunningLane] = {}  # lane_id -> RunningLane
        self._sessions: dict[str, Session] = {}  # session_id -> Session
        self._watchers = WatcherPool()
        self._supervisor: asyncio.Task[None] | None = None
        self._lifecycle: TaskLifecycleManager | None = None
        self._ledger: DelegationLedger | None = None
        self._gate: PolicyGate | None = None

    # --- wiring ------------------------------------------------------------
    @property
    def lifecycle(self) -> TaskLifecycleManager:
        if self._lifecycle is None:
            raise RuntimeError("SessionManager.start() must run before use")
        return self._lifecycle

    @property
    def ledger(self) -> DelegationLedger:
        if self._ledger is None:
            raise RuntimeError("SessionManager.start() must run before use")
        return self._ledger

    async def start(self) -> None:
        """Attach the lifecycle manager to the bus and begin supervising."""
        async with self.database.session() as session:
            repo = Repository(session)
            audit = AuditLog(session)

            def log_writer() -> BusEventLog:
                # Each emit needs a fresh session; the bus resolves one lazily.
                # A plain function, not `async def`: the manager calls this and
                # then awaits the *log*. An async factory would hand back a
                # coroutine that nothing awaits, which is one of the ways this
                # path was broken before — the other call site passed a sync
                # factory into the same broken `async with`.
                return BusEventLog(self.database.session_factory())

            self._lifecycle = TaskLifecycleManager(repo, log_writer, self.config.settings)
            self._ledger = DelegationLedger(
                repo,
                audit,
                self.config.settings,
                human_id=self.human_id,
                human_email=self.human_email,
                max_depth=self.config.governance.max_delegation_depth,
                allow_redelegation=self.config.governance.allow_redelegation,
                authority_inheritance=self.config.governance.authority_inheritance,
            )

        self._supervisor = asyncio.create_task(self._supervise())
        log.info("sessions.manager_started")

    async def stop(self) -> None:
        if self._supervisor is not None:
            self._supervisor.cancel()
            # Awaiting a task we just cancelled raises the cancellation we caused,
            # so suppressing it is the point rather than an oversight.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._supervisor
            self._supervisor = None
        await self._watchers.stop_all()
        for lane_id in list(self._running):
            await self.stop_lane(lane_id, reason="daemon shutdown")
        log.info("sessions.manager_stopped")

    # --- session lifecycle -------------------------------------------------
    async def create_session(
        self,
        *,
        name: str,
        description: str = "",
        branch: str = "",
        base_branch: str = "",
        owner: str = "",
        tags: list[str] | None = None,
        lanes: list[dict] | None = None,
    ) -> Session:
        """Create a session and bring up its lanes.

        Lanes come either from the caller's explicit list or from the repo's
        ``openburrow.yaml`` templates. Explicit wins, so a one-off session does
        not require editing committed configuration.
        """
        owner = owner or self.human_id or "local"
        repo_root = self.config.paths.repo_root
        branch = branch or f"{self.config.repo.project.branch_prefix}{name}"

        session = Session(
            name=name,
            description=description,
            owner=owner,
            repo_root=str(repo_root),
            branch=branch,
            base_branch=base_branch or self.config.repo.project.default_branch,
            status=SessionStatus.CREATED,
            tags=tags or [],
            started_at=now(),
        )
        session.thread_id = session.id

        async with self.database.session() as db_session:
            repo = Repository(db_session)
            await repo.save(session)

        self._sessions[session.id] = session
        await self.bus.emit(
            event_type="session.created",
            session_id=session.id,
            thread_id=session.thread_id,
            summary=f"session '{name}' created on branch {branch}",
            payload=session.model_dump(mode="json"),
        )

        lane_specs = lanes if lanes is not None else self._lanes_from_config()
        for spec in lane_specs:
            try:
                await self.start_lane(session, **spec)
            except Exception as exc:
                log.error("session.lane_failed", session_id=session.id, spec=spec, error=str(exc))
                await self.bus.emit(
                    event_type="lane.failed",
                    session_id=session.id,
                    summary=f"lane '{spec.get('name')}' failed to start: {exc}",
                )

        session.status = SessionStatus.ACTIVE if session.lanes else SessionStatus.CREATED
        await self._persist_session(session)
        log.info(
            "session.created",
            session_id=session.id,
            name=name,
            lanes=len(session.lanes),
        )
        return session

    def _lanes_from_config(self) -> list[dict]:
        """Turn ``openburrow.yaml`` lane templates into start specs."""
        specs: list[dict] = []
        for template in self.config.repo.lanes:
            specs.append(
                {
                    "name": template.name,
                    "harness": template.harness,
                    "role": template.role,
                    "owner": self.human_id or template.name,
                    "claims": list(template.claims),
                    "model": template.model,
                    "env_passthrough": list(template.env_passthrough),
                    "max_runtime_s": template.max_runtime_s,
                    "idle_timeout_s": template.idle_timeout_s,
                    "can_delegate": template.can_delegate,
                    "transferable": template.transferable,
                    "extra_args": list(template.extra_args),
                }
            )
        return specs

    async def start_lane(
        self,
        session: Session,
        *,
        name: str,
        harness: str,
        role: str = "implementer",
        owner: str = "",
        claims: list[str] | None = None,
        model: str = "",
        env_passthrough: list[str] | None = None,
        max_runtime_s: int = 0,
        idle_timeout_s: int = 900,
        can_delegate: bool = True,
        transferable: bool = True,
        extra_args: list[str] | None = None,
        worktree: Path | None = None,
    ) -> Lane:
        """Create a worktree, spawn the harness, and expose it as an A2A peer."""
        owner = owner or session.owner
        lane = Lane(
            session_id=session.id,
            name=name,
            harness=harness,
            role=LaneRole(role) if role in {r.value for r in LaneRole} else LaneRole.IMPLEMENTER,
            owner=owner,
            status=LaneStatus.STARTING,
            base_commit=session.base_commit,
            env_passthrough=env_passthrough or [],
            max_runtime_s=max_runtime_s,
            idle_timeout_s=idle_timeout_s,
            can_delegate=can_delegate,
            transferable=transferable,
            trust_boundary=str(TrustBoundary.INTRA_REPO),
        )
        lane.metadata["claims"] = claims or []
        lane.metadata["extra_args"] = extra_args or []
        lane.metadata["model"] = model

        # --- worktree -----------------------------------------------------
        worktree_path = worktree or self.config.paths.lane_worktree(lane.id)
        lane.worktree_path = str(worktree_path)
        lane.branch = f"{session.branch}/{name}"
        worktree_path.mkdir(parents=True, exist_ok=True)

        # --- adapter ------------------------------------------------------
        adapter = self.registry.create(harness, lane=lane)

        # --- policy gate --------------------------------------------------
        # Between building the plan and executing it, which is the only place a
        # pre-execution gate can be. The plan is built here rather than inside
        # ``start`` so that the thing gated and the thing spawned are the same
        # object: a gate that inspects a separately-built plan is inspecting a
        # different command than the one that runs.
        #
        # ``build_spawn_spec`` is pure in every adapter — no mkdir, no open, no
        # write — and that purity is what makes this possible. A builder with
        # side effects would have to be gated *after* the side effect, which is
        # to say after the thing the gate exists to prevent.
        spec = adapter.prepare_spawn_spec(lane)
        await self._gate_spawn(lane, spec, role=role)

        with bind_context(session_id=session.id, lane_id=lane.id, harness=harness):
            try:
                await adapter.start(lane, spec=spec)
            except AdapterError as exc:
                lane.status = LaneStatus.CRASHED
                await self._persist_lane(lane)
                raise ConfigError(
                    f"could not start harness {harness!r} for lane {name!r}",
                    hint=exc.hint or "Run `burrow doctor` to check the harness is installed.",
                    context={"harness": harness, "lane": name},
                    cause=exc,
                ) from exc

            # --- A2A server ----------------------------------------------
            server: LaneA2AServer | None = None
            if self.config.bus.enabled:
                server = LaneA2AServer(
                    lane,
                    # ``effective_capabilities``, not ``capabilities``: an
                    # adapter that degraded during start — OpenCode falling back
                    # to PTY mode when its headless server will not come up —
                    # must not publish an Agent Card still claiming structured
                    # output. This call site read ``adapter.capabilities``, so
                    # the degradation hook was never consulted and the
                    # capability-mismatch detector had nothing to catch.
                    capabilities=adapter.effective_capabilities(),
                    skills=adapter.skills(),
                    host=self.config.settings.a2a_host,
                    port=self._next_port(),
                    card_path=self.config.settings.a2a_agent_card_path,
                    on_message=lambda message: self._on_inbound(lane, adapter, message),
                    fetch_task=self._fetch_task,
                    cancel_task=self._cancel_task,
                )
                await server.start()
                lane.declared_skills = [s.id for s in adapter.skills()]
                lane.authority_scope = lane.authority_scope or _default_scope(role)

            # --- output pump ---------------------------------------------
            reader_task = asyncio.create_task(self._pump_output(lane, adapter))

            self._running[lane.id] = RunningLane(
                lane=lane, adapter=adapter, server=server, reader_task=reader_task
            )

        await self._persist_lane(lane)
        session.attach_lane(lane)
        await self._persist_session(session)

        # --- file watching -------------------------------------------------
        if worktree_path.exists():
            await self._watchers.watch(
                lane_id=lane.id,
                worktree=worktree_path,
                on_change=lambda change: self._on_file_change(lane, change),
                extra_ignore=self.config.repo.project.ignore_paths,
            )

        await self.bus.emit(
            event_type="lane.started",
            session_id=session.id,
            thread_id=session.thread_id,
            lane_id=lane.id,
            summary=f"lane '{name}' started ({harness})",
            payload=lane.model_dump(mode="json"),
        )
        log.info(
            "lane.started",
            lane_id=lane.id,
            name=name,
            harness=harness,
            a2a=lane.a2a_endpoint,
        )
        return lane

    @property
    def policy_gate(self) -> PolicyGate:
        """The gate, built once from the merged config.

        Built lazily and cached rather than constructed per lane, so the policy is
        read at a known moment and a mid-session edit to ``openburrow.yaml`` does
        not half-apply. ``repo_root`` is passed because policy paths are
        repository-relative while a spawn plan's working directory is absolute.
        """
        if self._gate is None:
            self._gate = PolicyGate(self.config.policy, repo_root=self.config.paths.repo_root)
        return self._gate

    async def _gate_spawn(self, lane: Lane, spec: SpawnSpec, *, role: str) -> None:
        """Run the policy gate on a finished spawn plan, before it executes.

        This is the call that turns the gate from a report into a gate. It was
        previously reachable only from ``burrow governance policy test``, which
        meant ``policy.enforce`` was read by nothing and
        :class:`~openburrow.core.errors.PolicyViolation` — documented as "the
        pre-execution policy gate blocked an action" — was exported and never
        raised.

        What it gates, precisely: **what OpenBurrow launches.** The argv and
        working directory of the harness. It does not see the commands that
        harness's agent runs later inside its own process, because there is no
        tool-call interception point yet. ``FEATURE_STATUS.md`` records that
        limitation rather than letting the roadmap's "inspect a command before it
        runs" imply more than it delivers.

        A denial raises when ``policy.enforce`` is true, so the lane does not
        start at all. A denial with ``enforce`` false is logged and published but
        allowed through — that is what "advisory" means, and it is reported as
        advisory rather than silently downgraded.
        """
        gate = self.policy_gate
        verdict = gate.check_argv(spec.command, cwd=str(spec.cwd), role=role)
        denied = verdict.action == "deny"

        if not denied and not verdict.would_require_approval:
            return

        payload = {
            **verdict.as_dict(),
            "lane_id": lane.id,
            "lane_name": lane.name,
            "harness": lane.harness,
            "enforced": gate.enforce,
        }
        await self.bus.emit(
            event_type="governance.policy_denied" if denied else "governance.approval_required",
            session_id=lane.session_id,
            lane_id=lane.id,
            summary=(
                f"policy denied lane '{lane.name}' ({verdict.matched_rule})"
                if denied
                else f"lane '{lane.name}' starts at {verdict.risk_tier} risk"
            ),
            payload=payload,
        )

        if denied and gate.enforce:
            log.error(
                "governance.policy_denied",
                lane_id=lane.id,
                harness=lane.harness,
                command=verdict.command,
                matched_rule=verdict.matched_rule,
            )
            raise PolicyViolation(
                f"policy denied starting lane {lane.name!r}: {verdict.reason()}",
                hint=(
                    "Run `burrow governance policy test "
                    f"{verdict.command!r} --role {role}` to see the rule, or set "
                    "policy.enforce=false to make the gate advisory."
                ),
                context=payload,
            )

        if denied:
            log.warning(
                "governance.policy_denied_advisory",
                lane_id=lane.id,
                matched_rule=verdict.matched_rule,
                hint="policy.enforce is false, so the lane was started anyway.",
            )
        else:
            # The approval tier is recorded, not enforced: pausing a lane start
            # needs the approvals store wired into the daemon, and the CLI's
            # `approvals.*` methods are not registered on the IPC surface yet.
            # Reported as a flag so the gap is visible in the audit log.
            log.warning(
                "governance.approval_required",
                lane_id=lane.id,
                risk_tier=verdict.risk_tier,
                matched_rule=verdict.matched_rule,
                hint="Approval-gated spawns are recorded but not paused yet.",
            )

    def _next_port(self) -> int:
        base = self.config.settings.a2a_port_base
        used = {rl.server.port for rl in self._running.values() if rl.server is not None}
        for offset in range(self.config.settings.a2a_port_range):
            candidate = base + offset
            if candidate not in used:
                return candidate
        return 0  # let the OS pick rather than failing to start

    async def stop_lane(self, lane_id: str, *, reason: str = "") -> bool:
        running = self._running.pop(lane_id, None)
        if running is None:
            return False

        with bind_context(lane_id=lane_id):
            if running.reader_task is not None:
                running.reader_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await running.reader_task

            if running.server is not None:
                await running.server.stop()

            await self._watchers.unwatch(lane_id)

            try:
                await running.adapter.stop()
            except Exception as exc:
                log.warning("lane.stop_failed", lane_id=lane_id, error=str(exc))

            running.lane.status = LaneStatus.STOPPED
            running.lane.stopped_at = now()
            await self._persist_lane(running.lane)

        await self.bus.emit(
            event_type="lane.stopped",
            session_id=running.lane.session_id,
            lane_id=lane_id,
            summary=f"lane stopped: {reason or 'requested'}",
        )
        log.info("lane.stopped", lane_id=lane_id, reason=reason)
        return True

    async def close_session(
        self, session_id: str, *, status: SessionStatus = SessionStatus.COMPLETED
    ) -> Session:
        session = await self.get_session(session_id)
        for lane_id in list(session.lanes):
            await self.stop_lane(lane_id, reason="session closed")

        if self.config.settings.worktree_cleanup == "on-session-end":
            self._cleanup_worktrees(session)

        session.close(status)
        await self._persist_session(session)
        await self.bus.emit(
            event_type="session.closed",
            session_id=session.id,
            thread_id=session.thread_id,
            summary=f"session closed ({status})",
            payload=session.summary(),
        )
        return session

    def _cleanup_worktrees(self, session: Session) -> None:
        for lane_id in session.lanes:
            path = self.config.paths.lane_worktree(lane_id)
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)

    # --- lookups -----------------------------------------------------------
    async def get_session(self, reference: str) -> Session:
        if reference in self._sessions:
            return self._sessions[reference]
        async with self.database.session() as db_session:
            repo = Repository(db_session)
            session = await repo.find_session(reference)
        if session is None:
            raise SessionNotFoundError(
                f"no session matching {reference!r}",
                hint="Run `burrow session list` to see open sessions.",
                context={"reference": reference},
            )
        self._sessions[session.id] = session
        return session

    async def get_lane(self, session_id: str, reference: str) -> Lane:
        for running in self._running.values():
            if running.lane.session_id == session_id and reference in (
                running.lane.id,
                running.lane.name,
            ):
                return running.lane
        async with self.database.session() as db_session:
            repo = Repository(db_session)
            lane = await repo.find_lane(session_id, reference)
        if lane is None:
            raise LaneNotFoundError(
                f"no lane matching {reference!r} in session {session_id}",
                hint="Run `burrow session show <session>` to list lanes.",
                context={"reference": reference, "session_id": session_id},
            )
        return lane

    def running_lanes(self, session_id: str = "") -> list[RunningLane]:
        values = list(self._running.values())
        if session_id:
            return [r for r in values if r.lane.session_id == session_id]
        return values

    # --- supervision -------------------------------------------------------
    async def _supervise(self) -> None:
        """Heartbeat, restart, and timeout lanes.

        Runs forever. Every failure inside the loop is caught and logged rather
        than propagated, because a supervisor that dies on the first unexpected
        error is not a supervisor.
        """
        while True:
            try:
                await asyncio.sleep(HEARTBEAT_INTERVAL_S)
                for running in list(self._running.values()):
                    await self._check_lane(running)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("supervisor.error", error=str(exc))

    async def _check_lane(self, running: RunningLane) -> None:
        lane = running.lane
        lane.heartbeat()

        if not running.adapter.is_running:
            await self._handle_crash(running)
            return

        if lane.budget_exceeded:
            log.warning("lane.budget_exceeded", lane_id=lane.id, runtime=lane.runtime_seconds)
            await self.stop_lane(lane.id, reason="exceeded max runtime")

    async def _handle_crash(self, running: RunningLane) -> None:
        """Apply the configured restart policy when a harness dies.

        ``backoff`` is the default because the most common crash cause is a
        transient provider error, and immediate restart turns one outage into a
        tight loop that burns the remaining budget.
        """
        lane = running.lane
        policy = self.config.adapters.crash_restart
        lane.status = LaneStatus.CRASHED

        await self.bus.emit(
            event_type="lane.crashed",
            session_id=lane.session_id,
            lane_id=lane.id,
            summary=f"harness exited with code {running.adapter.returncode}",
        )

        if policy == "never" or lane.restarts >= self.config.adapters.max_restarts:
            log.error("lane.dead", lane_id=lane.id, restarts=lane.restarts, policy=policy)
            lane.status = LaneStatus.STOPPED
            await self._persist_lane(lane)
            return

        if policy == "once" and lane.restarts >= 1:
            lane.status = LaneStatus.STOPPED
            await self._persist_lane(lane)
            return

        delay = min(30.0, 2.0**lane.restarts) if policy == "backoff" else 0.0
        lane.restarts += 1
        log.warning("lane.restarting", lane_id=lane.id, attempt=lane.restarts, delay_s=delay)
        if delay:
            await asyncio.sleep(delay)

        try:
            await running.adapter.restart(lane)
            lane.status = LaneStatus.IDLE
            await self.bus.emit(
                event_type="lane.restarted",
                session_id=lane.session_id,
                lane_id=lane.id,
                summary=f"restart attempt {lane.restarts}",
            )
        except Exception as exc:
            log.error("lane.restart_failed", lane_id=lane.id, error=str(exc))
            lane.status = LaneStatus.STOPPED
        await self._persist_lane(lane)

    # --- bus wiring --------------------------------------------------------
    async def _pump_output(self, lane: Lane, adapter: HarnessAdapter) -> None:
        """Read harness output, buffer it, and publish the interesting parts."""
        try:
            async for output in adapter.read_output():
                adapter.buffer_output(output)
                if output.artifacts:
                    lane.metadata.setdefault("artifacts", [])
                    lane.metadata["artifacts"] = [
                        *(lane.metadata.get("artifacts") or []),
                        *[a.model_dump(mode="json") for a in output.artifacts],
                    ][-100:]

                usage = adapter.parse_usage(output.text)
                if usage and lane is not None:
                    lane.record_usage(
                        tokens_in=int(usage.get("input", usage.get("prompt", 0)) or 0),
                        tokens_out=int(usage.get("output", usage.get("completion", 0)) or 0),
                    )

                # The adapter owns the "is this worth broadcasting" decision.
                # ``translate_output`` is documented as exactly that hook — it
                # knows which kinds a harness can produce and which of those
                # carry information — and it was called by nothing in production
                # code, only by tests. This loop kept its own hardcoded set
                # instead, and the two had drifted: the adapter broadcasts
                # ``status``, the daemon did not, so a lane's state reports were
                # dropped at the last step after being classified correctly.
                #
                # The daemon still builds its own event shape, because it is
                # emitting to the *event* bus for observability rather than
                # sending an A2A message to another lane. So it uses the
                # adapter's decision, not the adapter's message. Two copies of
                # one decision is one copy too many, and the copy that drifts is
                # always the one nothing reads.
                if adapter.translate_output(output) is None:
                    continue
                await self.bus.emit(
                    event_type=f"lane.output.{output.kind}",
                    session_id=lane.session_id,
                    thread_id=lane.session_id,
                    lane_id=lane.id,
                    summary=output.text[:200],
                    payload=output.to_bus_payload(),
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("lane.output_pump_failed", lane_id=lane.id, error=str(exc))

    async def _on_inbound(self, lane: Lane, adapter: HarnessAdapter, message) -> None:
        """Handle a message arriving at a lane's A2A endpoint."""
        with bind_context(session_id=lane.session_id, lane_id=lane.id):
            lane.messages_received += 1
            await self.bus.emit(
                event_type="bus.message.received",
                session_id=lane.session_id,
                thread_id=message.thread_id or lane.session_id,
                lane_id=lane.id,
                summary=f"received {message.intent} from {message.sender_lane}",
                payload=message.model_dump(mode="json"),
                content_hash=message.content_hash,
            )

            # Governance: verify the sender is who it claims to be.
            from openburrow.governance import detect_impersonation

            detection = detect_impersonation(
                claimed_lane=message.sender_lane,
                actual_lane=message.sender_lane,
                claimed_harness=message.sender_harness,
            )
            if detection.flagged:
                await self.bus.emit(
                    event_type="governance.flag",
                    session_id=lane.session_id,
                    lane_id=lane.id,
                    summary=detection.summary,
                    payload=detection.detail,
                )

            try:
                await adapter.inject_message(message)
            except Exception as exc:
                log.warning("lane.inject_failed", lane_id=lane.id, error=str(exc))
            return

    async def _fetch_task(self, task_id: str):
        async with self.database.session() as db_session:
            from openburrow.core.models import A2ATask

            return await Repository(db_session).get(A2ATask, task_id)

    async def _cancel_task(self, task_id: str, reason: str):
        from openburrow.core.models import A2ATask

        async with self.database.session() as db_session:
            repo = Repository(db_session)
            task = await repo.get(A2ATask, task_id)
            if task is None:
                return None
            await self.lifecycle.cancel(task, reason=reason)
            return task

    async def _on_file_change(self, lane: Lane, change) -> None:
        """Translate a debounced file-change batch into a bus event."""
        await self.bus.emit(
            event_type="files.changed",
            session_id=lane.session_id,
            thread_id=lane.session_id,
            lane_id=lane.id,
            summary=f"{lane.name} changed {change.summary()}",
            payload=change.to_payload(),
        )

    # --- persistence -------------------------------------------------------
    async def _persist_session(self, session: Session) -> None:
        async with self.database.session() as db_session:
            await Repository(db_session).save(session)

    async def _persist_lane(self, lane: Lane) -> None:
        async with self.database.session() as db_session:
            await Repository(db_session).save(lane)

    # --- status ------------------------------------------------------------
    def status(self) -> dict:
        return {
            "sessions": len(self._sessions),
            "lanes": len(self._running),
            "watchers": self._watchers.count,
            "subscribers": self.bus.subscriber_count,
            "lane_detail": [
                {
                    "lane_id": r.lane.id,
                    "name": r.lane.name,
                    "harness": r.lane.harness,
                    "status": str(r.lane.status),
                    "a2a": r.lane.a2a_endpoint,
                    "pid": r.lane.pid,
                }
                for r in self._running.values()
            ],
        }


def _default_scope(role: str) -> list[str]:
    """Starting authority scope for a lane, by role."""
    mapping = {
        "implementer": ["read:*", "write:*", "exec:test"],
        "reviewer": ["read:*", "inform:*", "propose:*"],
        "coordinator": ["read:*", "delegate:*"],
        "observer": ["read:*"],
    }
    return mapping.get(str(role), ["read:*"])


__all__ = ["HEARTBEAT_INTERVAL_S", "RunningLane", "SessionManager"]
