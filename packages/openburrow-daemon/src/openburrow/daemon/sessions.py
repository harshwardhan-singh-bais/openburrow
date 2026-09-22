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
from openburrow.adapters import AdapterRegistry, HarnessAdapter, HarnessOutput, SpawnSpec
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
    A2ATask,
    BusMessage,
    Lane,
    LaneRole,
    LaneStatus,
    Session,
    SessionStatus,
    TrustBoundary,
    now,
)
from openburrow.core.paths import repo_id_for
from openburrow.daemon import sandbox
from openburrow.daemon.bus import EventBus
from openburrow.daemon.chaos import ChaosEngine
from openburrow.daemon.context import LaneBriefing, assemble_lane_briefing
from openburrow.daemon.filewatch import FileChange, WatcherPool
from openburrow.daemon.observability import (
    record_lane_output,
    record_lesson_hit,
    record_lesson_injection,
    record_retry,
    record_usage,
    span,
)
from openburrow.daemon.recovery import RecoveryManager
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


def _briefing_message(lane: Lane, session: Session, briefing: LaneBriefing) -> BusMessage:
    """Wrap a briefing as a bus message.

    A message rather than a bespoke delivery call, because ``inject_message`` is
    the one path that already knows how each harness accepts input — and because
    it puts the briefing on the bus, where a reel can show what a lane was told.

    The sender is left empty on purpose. This is not from a lane and not from the
    operator, and the rendered attribution reads "from another agent", which is
    what the briefing text itself already says about where the knowledge came
    from. Naming a lane here would be a lie a reader could not detect.
    """
    return BusMessage(
        session_id=lane.session_id,
        thread_id=session.thread_id,
        recipients=[lane.id],
        subject="Project briefing",
        body=briefing.text,
        payload={"openburrow:briefing": briefing.as_dict()},
    )


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
        # Built in start(), alongside the lifecycle manager: recovery needs the
        # same wiring (db, bus, this manager) and is consulted by the supervisor
        # from the first heartbeat.
        self.recovery: RecoveryManager | None = None
        #: The chaos engine is handed in by the daemon after construction (it
        #: exists only when chaos_enabled); None means faults are never injected.
        self._chaos_engine: ChaosEngine | None = None

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
            self.recovery = RecoveryManager(self.config, self.database, self.bus, self)
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
            # Optional keys only when set, so a spec never carries empty
            # defaults that would shadow the adapter's own behaviour.
            if template.command:
                specs[-1]["command"] = template.command
            if template.use_pty:
                specs[-1]["use_pty"] = True
            if template.env:
                specs[-1]["env"] = dict(template.env)
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
        command: list[str] | str = "",
        use_pty: bool = False,
        env: dict[str, str] | None = None,
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
        # The custom adapter reads its command, PTY preference and env from
        # these keys; every other adapter ignores them in favour of its own
        # binary. Carried on the lane rather than resolved here so the adapter
        # stays the single place that knows what its harness needs.
        if command:
            lane.metadata["command"] = command
        if use_pty:
            lane.metadata["use_pty"] = True
        if env:
            lane.metadata["env"] = dict(env)

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

        # --- sandbox ------------------------------------------------------
        # After the gate and before the spawn. After the gate so a denied
        # command is refused rather than wrapped and then refused — a sandbox
        # availability check on the path of a command that was never going to
        # run would turn a policy denial into a confusing "backend missing"
        # error. Before the spawn so the object that runs is the object that was
        # gated and then wrapped, in that order, with nothing rebuilt in between.
        #
        # ``wrap`` raises when sandboxing was asked for and cannot be provided.
        # That is the intended behaviour, not a rough edge: an operator who sets
        # sandbox_enabled=true and gets no sandbox has been told something
        # false, and will act on it.
        spec = sandbox.wrap(self.config.settings, spec, repo_root=self.config.paths.repo_root)

        with bind_context(session_id=session.id, lane_id=lane.id, harness=harness):
            try:
                # The span opens *after* the gate, so a refused spawn produces no
                # lane span at all. A span that exists for a lane which never
                # started reads, in a trace viewer, exactly like a lane that
                # started and did nothing — which is the opposite of the truth.
                with span("lane.start", **{"lane.name": name, "lane.harness": harness}):
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
                    fetch_task=self.fetch_task,
                    cancel_task=self.cancel_task,
                )
                await server.start()
                lane.declared_skills = [s.id for s in adapter.skills()]
                lane.authority_scope = lane.authority_scope or _default_scope(role)

            # --- output pump ---------------------------------------------
            reader_task = asyncio.create_task(self._pump_output(lane, adapter))

            self._running[lane.id] = RunningLane(
                lane=lane, adapter=adapter, server=server, reader_task=reader_task
            )

        # --- briefing -------------------------------------------------------
        # After the adapter started, because delivery goes through the harness's
        # own input path; before the lane is persisted, so the ids the briefing
        # spent are written alongside the lane that spent them.
        await self._brief_lane(lane, adapter, session)

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

    async def _brief_lane(
        self, lane: Lane, adapter: HarnessAdapter, session: Session
    ) -> LaneBriefing:
        """Put the repository's accumulated knowledge in front of a starting lane.

        This is the integration point Stages 18 and 19 never had. The Brain and
        the lesson store both expose a ranked ``select_for_injection``, both are
        populated by their promotion paths, and until this method existed no code
        path called either one — so a lane started with an empty context no matter
        what the session had already learned. Complete, correct, unreachable: the
        declared-and-inert shape this project keeps finding in its own work.

        Two things are deliberately *not* done here.

        **The briefing is not a spawn argument.** It goes out through
        :meth:`~openburrow.adapters.base.HarnessAdapter.inject_message`, the same
        path an inbound A2A message takes. That path already knows how each
        harness accepts input — a PTY write for one, a structured hook for another
        — so a new harness gets briefings for free instead of needing a second
        implementation. It also puts the briefing on the bus, where a reel can
        show what a lane was told.

        **Nothing is invented when there is nothing to say.** An empty briefing is
        published as empty rather than filled with "no knowledge recorded yet".
        A lane told nothing and a lane told a placeholder are in the same
        position, and the placeholder would make the reel show an injection that
        carried no information — a number wrong in a plausible direction, which is
        the one thing this codebase consistently refuses to produce.

        Delivery failure does not stop the lane. A harness that accepts no input
        is a real problem, but not one this call should turn into a failed start:
        the lane is discoverable and the failure is published, so the operator
        sees a lane that started and was never briefed rather than no lane at all.

        The budgets are not parameters. Each store enforces its own hard cap and
        documents why it is enforced there rather than left to the caller to
        remember, so a second cap here would be a second policy that could
        disagree with the first.
        """
        with bind_context(session_id=lane.session_id, lane_id=lane.id):
            briefing = await assemble_lane_briefing(
                self.database,
                repo_id=repo_id_for(self.config.paths.repo_root),
                session_id=session.id,
                claims=list(lane.metadata.get("claims") or []),
            )
            # Written with the lane, so a later "did this lesson help?" can be
            # attributed without re-deriving what the lane was shown.
            lane.metadata["briefing"] = briefing.as_dict()

            payload: dict[str, object] = {**briefing.as_dict(), "delivered": False}
            if briefing.is_empty:
                payload["reason"] = "nothing recorded for this repository yet"
            else:
                try:
                    delivered = await adapter.inject_message(
                        _briefing_message(lane, session, briefing)
                    )
                except Exception as exc:
                    payload["reason"] = f"injection failed: {type(exc).__name__}"
                else:
                    payload["delivered"] = delivered
                    if delivered:
                        # Item 54's denominator, per lane: briefings (or inbound
                        # injections) that actually reached this lane. The pump
                        # pairs it with ``injections_actioned`` for the report.
                        lane.metadata["injections_delivered"] = (
                            int(lane.metadata.get("injections_delivered") or 0) + 1
                        )
                    if delivered:
                        # Item 217's denominator, driven from the only place that
                        # actually injects. It was a metric with no producer
                        # before this call site existed, which is how a dashboard
                        # reads zero and looks like a quiet system.
                        #
                        # Note the two counts measure different moments, on
                        # purpose. ``select_for_injection`` bumps each lesson's
                        # ``injection_count`` when it is *chosen*, because the
                        # store cannot know whether delivery will succeed and its
                        # eviction threshold is about being spent. This counter is
                        # bumped only on *arrival*. The two therefore differ by
                        # exactly the failed deliveries, which is a number worth
                        # being able to see rather than one worth hiding behind a
                        # shared name.
                        record_lesson_injection(len(briefing.lesson_ids))
                    else:
                        payload["reason"] = "the harness accepted no input"

            await self.bus.emit(
                event_type="lane.briefed",
                session_id=lane.session_id,
                thread_id=session.thread_id,
                lane_id=lane.id,
                summary=(
                    f"briefed {lane.name} with {len(briefing.brain_entry_ids)} brain "
                    f"entr{'y' if len(briefing.brain_entry_ids) == 1 else 'ies'} and "
                    f"{len(briefing.lesson_ids)} lesson(s)"
                ),
                payload=payload,
            )
        return briefing

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
                if self.recovery is not None:
                    await self.recovery.requeue_orphans(older_than_s=self._orphan_threshold_s())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("supervisor.error", error=str(exc))

    def _orphan_threshold_s(self) -> int:
        """Stale-heartbeat window: the widest lane timeout, with a floor.

        Requeueing a lane that is merely slow is the failure mode to avoid — a
        lane with a 900s idle timeout must not be declared orphaned at 60s — so
        the threshold is derived from the running lanes rather than hard-coded.
        """
        timeouts = [r.lane.idle_timeout_s for r in self._running.values() if r.lane.idle_timeout_s]
        return max(timeouts, default=60)

    def _chaos(self) -> ChaosEngine | None:
        """The daemon's chaos engine, when armed. Consulted at injection points."""
        return self._chaos_engine

    async def _check_lane(self, running: RunningLane) -> None:
        lane = running.lane
        lane.heartbeat()

        # The beat has to reach the *record*, not just the object. Orphan
        # detection reads lanes back from the database, so a heartbeat that only
        # ever lived in memory was invisible to the one check meant to notice a
        # lane going quiet — and it concluded "dead". Suppressed and logged
        # rather than raised, because a database hiccup must not end supervision
        # for every other lane.
        with contextlib.suppress(Exception):
            await self._record_heartbeat(lane)

        # Chaos injection point: a scripted kill is indistinguishable from a
        # real crash to everything downstream, which is the point (item 249).
        # The stop is via the adapter's own `stop`, not a private attribute —
        # poking `_started` from outside the adapter is how the mock and the
        # real adapters come to disagree about what "crashed" means.
        chaos = self._chaos_engine
        if chaos is not None and chaos.should_kill(lane.id):
            log.warning("chaos.kill_lane", lane_id=lane.id)
            with contextlib.suppress(Exception):
                await running.adapter.stop(force=True)

        if self.recovery is not None:
            await self.recovery.check_lane(running)

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
        # Item 215: retry rate is the signal that a harness or provider is
        # unreliable; it is only meaningful if counted at the single choke point
        # every retry passes through. Labelled by outcome so "we restart a lot"
        # and "restarting stopped working" are separable — the second is the one
        # that needs a human, and an unlabelled counter hides it inside the
        # first.
        record_retry("attempted")
        if self.recovery is not None:
            # Item 166: a structured crash record goes through the recovery
            # manager, which also keeps the per-lane ring buffer the CLI reads.
            await self.recovery.record_crash(
                running,
                reason="harness process exited",
                exit_code=running.adapter.returncode,
            )

        await self.bus.emit(
            event_type="lane.crashed",
            session_id=lane.session_id,
            lane_id=lane.id,
            summary=f"harness exited with code {running.adapter.returncode}",
        )

        if policy == "never" or lane.restarts >= self.config.adapters.max_restarts:
            log.error("lane.dead", lane_id=lane.id, restarts=lane.restarts, policy=policy)
            record_retry("exhausted")
            lane.status = LaneStatus.STOPPED
            await self._persist_lane(lane)
            # The restart policy is exhausted: the dead-letter path (item 163)
            # records the failure and stops trying. A dead-lettered lane waits
            # for a human — `burrow session resume` — not for another attempt.
            if self.recovery is not None:
                await self.recovery.record_task_failure(
                    lane, error=f"restart policy exhausted ({policy})"
                )
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
            record_retry("succeeded")
            if self.recovery is not None:
                self.recovery.record_task_success(lane)
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
                # Chaos injection point: corrupt structured output so the
                # fallback parser and the dead-letter path get real traffic
                # (item 251). Applied to the copy that gets classified, not to
                # the buffered original — the record of what the harness
                # actually said must stay honest.
                chaos = self._chaos_engine
                if chaos is not None and output.text:
                    corrupted = chaos.malform_output(lane.id, output.text)
                    if corrupted != output.text:
                        # Mutated in place: everything downstream — the buffer,
                        # the classifier, the bus — should see the corrupted
                        # stream exactly as a real malformed harness would have
                        # produced it, with no second copy to drift.
                        output.text = corrupted
                adapter.buffer_output(output)
                if output.kind == "tool-call" and output.text.strip():
                    # Item 190's evidence trail: the detector compares what a
                    # lane *touched* against what it *said* it would touch, and
                    # the pump is the only place that sees the touches. Rolling
                    # window — the last N calls describe current behaviour; the
                    # full history describes a session that no longer exists.
                    recent = lane.metadata.setdefault("tool_calls", [])
                    recent.append(output.text[:500])
                    del recent[:-50]
                if output.kind == "plan" and output.text.strip():
                    # Item 190's *disclosure* half, finally recorded. The
                    # adversarial-intent check reads ``stated_intent`` from this
                    # metadata key, and until the pump wrote it the detector
                    # compared tool calls against an empty string — noisy by
                    # construction, per its own comment. A lane's plan is what
                    # it said it would do; first plan wins, later ones refine.
                    lane.metadata.setdefault("stated_intent", output.text[:500])
                if output.artifacts:
                    lane.metadata.setdefault("artifacts", [])
                    lane.metadata["artifacts"] = [
                        *(lane.metadata.get("artifacts") or []),
                        *[a.model_dump(mode="json") for a in output.artifacts],
                    ][-100:]

                usage = adapter.parse_usage(output.text)
                if usage and lane is not None:
                    tokens = int(usage.get("input", usage.get("prompt", 0)) or 0) + int(
                        usage.get("output", usage.get("completion", 0)) or 0
                    )
                    lane.record_usage(
                        tokens_in=int(usage.get("input", usage.get("prompt", 0)) or 0),
                        tokens_out=int(usage.get("output", usage.get("completion", 0)) or 0),
                    )
                    # Item 214: per-session token/cost attribution. Exported via
                    # Prometheus; the per-lane ledger stays the source of truth.
                    record_usage(lane.session_id, tokens)

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
                if output.terminal and output.kind in {"result", "status"}:
                    # Item 162's trigger: the pump marks "this lane just claimed
                    # success", and the supervisor's silent-failure check reads
                    # it on the next heartbeat. Kept out of the recovery module
                    # because only the pump sees harness output arrive.
                    lane.metadata["just_finished"] = True
                await self.bus.emit(
                    event_type=f"lane.output.{output.kind}",
                    session_id=lane.session_id,
                    thread_id=lane.session_id,
                    lane_id=lane.id,
                    summary=output.text[:200],
                    payload=output.to_bus_payload(),
                )
                # Item 218: harness output volume, by kind. Counted after the
                # emit succeeds, so the metric reflects what actually shipped.
                record_lane_output(output.kind)
                self._maybe_record_lesson_hit(lane, output)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("lane.output_pump_failed", lane_id=lane.id, error=str(exc))

    # --- metrics -----------------------------------------------------------
    @staticmethod
    def _maybe_record_lesson_hit(lane: Lane, output: HarnessOutput) -> None:
        """Attribute a lane's later output to the lessons it was briefed with.

        This is the producer item 217's numerator never had: ``record_lesson_hit``
        existed, and nothing called it, so the hit-rate metric read 0 forever and
        looked like lessons never helped. The attribution is a *detector* in the
        ADR 0009 sense — recall-oriented, allowed to be noisy. A hit is a
        structured output (a plan, a diff, a result — not progress chatter) whose
        text shares a distinctive token with a lesson the lane was briefed on.
        Recorded once per lane per lesson, because the same fix appearing in ten
        later outputs is one lesson working, not ten.

        The alternative — declaring the metric unreachable and deleting it —
        would be honest about the wiring but would discard the one signal that
        says whether Stage 10 is worth its cost. This is the middle path: a
        number that can over-count, never one that fabricates.
        """
        briefing = lane.metadata.get("briefing") or {}
        titles = [str(t) for t in (briefing.get("lesson_titles") or []) if t]
        if not titles:
            return
        text = output.text.casefold()
        if not text.strip() or output.kind not in {"plan", "diff", "result", "error"}:
            return
        counted: list[str] = list(lane.metadata.get("lesson_hits") or [])
        matched = False
        for title in titles:
            if title in counted:
                continue
            tokens = {t for t in title.casefold().split() if len(t) >= 6}
            if tokens and any(token in text for token in tokens):
                counted.append(title)
                matched = True
        if not matched:
            return
        lane.metadata["lesson_hits"] = counted
        lane.metadata["injections_actioned"] = (
            int(lane.metadata.get("injections_actioned") or 0) + 1
        )
        record_lesson_hit()

    async def _on_inbound(self, lane: Lane, adapter: HarnessAdapter, message: BusMessage) -> None:
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

    async def fetch_task(self, task_id: str) -> A2ATask | None:
        async with self.database.session() as db_session:
            return await Repository(db_session).get(A2ATask, task_id)

    async def cancel_task(self, task_id: str, reason: str) -> A2ATask | None:
        async with self.database.session() as db_session:
            repo = Repository(db_session)
            task = await repo.get(A2ATask, task_id)
            if task is None:
                return None
            await self.lifecycle.cancel(task, reason=reason)
            return task

    async def _on_file_change(self, lane: Lane, change: FileChange) -> None:
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

    async def _record_heartbeat(self, lane: Lane) -> None:
        """Persist one lane's heartbeat as a single-column update.

        Not ``_persist_lane``: this runs every supervision tick for every running
        lane, and rewriting the whole row on that cadence is how a supervision
        pass clobbers a field another coroutine just wrote. The repository method
        touches only ``last_heartbeat``.
        """
        async with self.database.session() as db_session:
            await Repository(db_session).record_heartbeat(lane.id, when=lane.last_heartbeat)

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
