"""The burrow daemon.

A single long-running asyncio process per machine (or per repo, when
``DAEMON_MULTI_REPO`` is off) that owns every piece of live state:

* the SQLite database and the append-only bus log
* the running lanes and their harness processes
* the per-lane A2A servers
* the file watchers
* the control-plane socket the CLI talks to

Why a daemon at all, rather than spawning everything per command? Because lanes
are *long-lived*: a harness mid-task cannot survive the CLI process exiting, and
a bus that only exists while a command runs cannot deliver an asynchronous
message between two lanes. The daemon is what makes "agents talking to each
other" possible at all rather than "agents taking turns inside one command".

Shutdown is graceful and bounded: stop accepting, drain in-flight tasks, flush
the bus log, then exit. A daemon that is killed mid-write is recoverable — that
is what the append-only log and checkpoints are for — but not being killed is
better.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from openburrow.adapters import AdapterRegistry, build_registry
from openburrow.core.config.load import ResolvedConfig, load_config
from openburrow.core.db.engine import Database, init_database
from openburrow.core.db.repository import BusEventLog, Repository
from openburrow.core.errors import (
    ConfigError,
    DaemonAlreadyRunningError,
    SessionNotFoundError,
)
from openburrow.core.logging import configure_logging, get_logger
from openburrow.core.models import A2ATask, SessionStatus, now
from openburrow.core.paths import BurrowPaths, is_windows
from openburrow.core.version import __version__
from openburrow.daemon.bus import EventBus
from openburrow.daemon.chaos import ChaosEngine
from openburrow.daemon.ipc import AnyHandler, IpcServer, socket_is_live
from openburrow.daemon.notify import Notifier
from openburrow.daemon.observability import setup_observability, shutdown_observability
from openburrow.daemon.recovery import RecoveryManager
from openburrow.daemon.relay_client import RelayClient
from openburrow.daemon.sessions import SessionManager
from openburrow.radar import Radar

log = get_logger(__name__)

PID_STALE_GRACE_S = 3.0


class Daemon:
    """The long-running burrow process."""

    def __init__(self, config: ResolvedConfig, *, allow_multiple_repos: bool = True) -> None:
        self.config = config
        self.paths: BurrowPaths = config.paths
        self.settings = config.settings
        self.allow_multiple_repos = allow_multiple_repos

        self.database: Database | None = None
        self.bus: EventBus | None = None
        self.registry: AdapterRegistry | None = None
        self.sessions: SessionManager | None = None
        self.ipc: IpcServer | None = None

        #: The Merge Radar's live state, built on first use by the request
        #: surface. Held on the daemon because its value is memory *across*
        #: requests — which pairs it has already announced, and how often the
        #: judge returned a usable verdict.
        self.radar: Radar | None = None

        #: Stage 12 durable recovery. Built after the session manager in
        #: start() (it needs the manager to respawn lanes) and exposed on the
        #: daemon for the same reason the Radar is: the CLI and the tests want
        #: the request surface without opening a lane.
        self.recovery: RecoveryManager | None = None

        self._started_at = now()
        self._stopping = asyncio.Event()
        self._tasks: set[asyncio.Task[Any]] = set()
        #: The in-flight shutdown task, if one was started. Held as a strong
        #: reference for the reason given in ``_h_stop``. Deliberately not part
        #: of ``_tasks``: ``stop()`` cancels everything in that set, so a shutdown
        #: task stored there would cancel itself on its first await.
        self._shutdown_task: asyncio.Task[Any] | None = None
        self._requests_served = 0

    # --- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        """Bring the daemon up: paths, db, bus, registry, sessions, IPC."""
        configure_logging(
            level=self.settings.log_level,
            fmt=self.settings.log_format,
            log_file=self.settings.log_file or str(self.paths.logs_dir / "daemon.log"),
        )
        self.paths.ensure()
        self._guard_already_running()
        self._write_pidfile()

        # --- observability (Stage 16, items 212-213) -----------------------
        # Before anything else so spans opened during startup are attributed to
        # this daemon. Both exporters fail closed to no-ops; a dead collector
        # must never take the session supervisor down with it.
        self.observability = setup_observability(self.settings)

        log.info(
            "daemon.starting",
            version=__version__,
            repo=str(self.paths.repo_root),
            pid=os.getpid(),
            env=self.settings.env,
        )

        # --- database -----------------------------------------------------
        self.database = await init_database(
            self.config.db_url,
            echo=self.settings.db_echo,
        )
        health = await self.database.healthcheck()
        if not health.get("ok"):
            raise ConfigError(
                f"database is not usable: {health.get('error')}",
                hint="Check OPENBURROW_DB_URL and that the directory is writable.",
                context={"url": self.config.db_url},
            )

        # --- bus ----------------------------------------------------------
        # The session factory is captured now rather than read from
        # `self.database` on each call. The lambda runs later, and nothing stops
        # shutdown from clearing that attribute in between — mypy refuses to
        # narrow through the closure, and it is right not to. Binding the
        # factory here makes the bus keep working for the object it was built
        # for instead of depending on an attribute that can move underneath it.
        session_factory = self.database.session_factory
        self.bus = EventBus(lambda: BusEventLog(session_factory()))

        # --- adapters -----------------------------------------------------
        self.registry = build_registry(self.settings)

        # --- sessions -----------------------------------------------------
        self.sessions = SessionManager(
            self.config,
            self.database,
            self.bus,
            self.registry,
            human_id=self.settings.governance_human_id,
            human_email=self.settings.governance_human_email,
        )
        await self.sessions.start()

        # --- recovery (Stage 12) -----------------------------------------
        # Built after sessions.start() because the manager wires its own
        # recovery reference; the daemon-level handle is the same object, kept
        # for the handlers and `daemon.health`.
        self.recovery = self.sessions.recovery

        # --- notifications (Stage 17) -------------------------------------
        # A bus subscriber, so every qualifying event reaches the configured
        # channels — desktop, webhooks, hooks — without any emitter having to
        # know the notifier exists. Detection (this subscription) stays separate
        # from enforcement (the policies that raise), per ADR 0009.
        self.notifier: Notifier | None = None
        self._notify_sub = None
        self._notify_task: asyncio.Task[None] | None = None

        # --- relay client (Stage 4, items 62-65) ---------------------------
        # Owns the outbound queue and the reconnect loop. The bus subscription
        # is wired in start() alongside the notifier's, and `attach()` gives it
        # the local re-emit path so relayed facts land on this bus with the
        # `relay` trust boundary rather than impersonating local ones.
        self.relay: RelayClient | None = (
            RelayClient(self.config.settings, room=self.config.settings.relay_workspace)
            if self.config.settings.relay_enabled
            else None
        )
        self._relay_sub = None

        #: Stage 20 chaos engine. Attached only when chaos_enabled is set (and
        #: production startup refuses that combination); advisory by design —
        #: the injection points decide, this engine only plans and records.
        self.chaos: ChaosEngine | None = (
            ChaosEngine(self.config.settings) if self.config.settings.chaos_enabled else None
        )

        # --- control plane ------------------------------------------------
        self.ipc = IpcServer(self.paths)
        self._register_handlers()
        endpoint = await self.ipc.start()

        self._install_signal_handlers()

        # --- chaos (Stage 20) ---------------------------------------------
        # Announced on the bus so a chaos run is always on the record — a fault
        # injector nobody was told about is indistinguishable from a bug.
        if self.chaos is not None:
            self.sessions._chaos_engine = self.chaos
            await self.bus.emit(
                event_type="chaos.armed",
                summary="chaos engine armed — fault injection is active",
                payload={
                    "scenario": self.chaos.run_scripted_scenario(),
                    "seed": self.config.settings.chaos_seed,
                },
            )

        # --- notifier subscription ----------------------------------------
        if self.config.settings.notify_enabled:
            self.notifier = Notifier(self.config.settings)
            self._notify_sub = self.bus.subscribe(event_types=None)
            self._notify_task = asyncio.create_task(self._notify_loop())

        # --- relay client (items 62-65) ------------------------------------
        if self.relay is not None and self.bus is not None:
            self._relay_sub = self.bus.subscribe(event_types=None)
            self.relay.attach(self._relay_reemit)
            self.relay.start()
            # Kept on the task set: an unreferenced feeder loop can be
            # garbage-collected mid-flight, silently cutting the relay off.
            self._tasks.add(asyncio.create_task(self._relay_loop()))

        log.info(
            "daemon.ready",
            endpoint=endpoint,
            adapters=list(self.registry.names()),
            a2a_enabled=self.config.bus.enabled,
            governance=self.config.governance.enabled,
        )
        await self.bus.emit(
            event_type="daemon.started",
            summary=f"daemon {__version__} ready",
            payload={"pid": os.getpid(), "version": __version__, "repo": str(self.paths.repo_root)},
        )

    async def _notify_loop(self) -> None:
        """Deliver bus events to the notifier until shutdown.

        A cancellation here is normal shutdown, so it exits quietly; anything
        else is logged with its error, because a notifier that dies on one
        malformed event must not take the daemon's event loop with it.
        """
        assert self.notifier is not None and self._notify_sub is not None
        assert self.bus is not None  # the loop only runs after start() wired the bus
        try:
            async for event in self.bus.listen(self._notify_sub, idle_timeout=None):
                try:
                    await self.notifier.handle_event(event)
                except Exception as exc:
                    log.warning("notify.event_failed", error=str(exc))
        except asyncio.CancelledError:
            return
        except Exception as exc:
            log.error("notify.loop_failed", error=str(exc))

    async def _relay_loop(self) -> None:
        """Feed bus events to the relay client's outbound queue until shutdown."""
        assert self.relay is not None and self._relay_sub is not None
        assert self.bus is not None
        try:
            async for event in self.bus.listen(self._relay_sub, idle_timeout=None):
                self.relay.offer(event)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            log.error("relay.loop_failed", error=str(exc))

    async def _relay_reemit(self, event: dict) -> None:
        """Re-emit a relayed event onto the local bus (durable, then fan-out)."""
        assert self.bus is not None
        with contextlib.suppress(Exception):
            await self.bus.emit(**event)

    async def serve_forever(self) -> None:
        """Block until a shutdown signal arrives."""
        await self._stopping.wait()

    async def stop(self, *, reason: str = "requested") -> None:
        """Graceful shutdown: drain, flush, clean up.

        The order matters. Lanes stop first so no new writes arrive, then the
        bus flushes, then the IPC socket closes so clients get a clean
        connection-refused rather than a half-dead socket, and only then does the
        database close.
        """
        if self._stopping.is_set():
            return
        self._stopping.set()

        grace = self.settings.daemon_shutdown_grace_s
        log.info("daemon.stopping", reason=reason, grace_s=grace)

        try:
            if self.sessions is not None:
                await asyncio.wait_for(self.sessions.stop(), timeout=grace)
        except TimeoutError:
            log.warning("daemon.session_drain_timeout", grace_s=grace)
        except Exception as exc:
            log.error("daemon.session_stop_failed", error=str(exc))

        for task in list(self._tasks):
            task.cancel()

        if self._notify_task is not None:
            self._notify_task.cancel()
            self._notify_task = None

        if self.relay is not None:
            await self.relay.stop()

        if self.bus is not None:
            try:
                await self.bus.emit(
                    event_type="daemon.stopped",
                    summary=f"daemon stopped: {reason}",
                    payload={"uptime_s": round(self.uptime_seconds, 1)},
                )
            except Exception as exc:
                log.warning("daemon.final_event_failed", error=str(exc))

        if self.ipc is not None:
            await self.ipc.stop()

        if self.database is not None:
            await self.database.close()

        self._remove_pidfile()
        shutdown_observability()
        log.info("daemon.stopped", reason=reason, uptime_s=round(self.uptime_seconds, 1))

    # --- process hygiene ---------------------------------------------------
    def _guard_already_running(self) -> None:
        pid_path = self.paths.pid_path
        if pid_path.exists():
            try:
                existing = int(pid_path.read_text(encoding="utf-8").strip())
            except (ValueError, OSError):
                existing = 0

            if existing and existing != os.getpid() and _pid_is_alive(existing):
                raise DaemonAlreadyRunningError(
                    f"a burrow daemon is already running (pid {existing})",
                    hint="Use `burrow daemon status` to inspect it, or `burrow daemon restart`.",
                    context={"pid": existing, "pidfile": str(pid_path)},
                )
            log.info("daemon.stale_pidfile", pid=existing)
            pid_path.unlink(missing_ok=True)

        if not is_windows() and self.paths.socket_path.exists():
            if socket_is_live(self.paths.socket_path):
                raise DaemonAlreadyRunningError(
                    "a burrow daemon is already listening on the control socket",
                    hint="Use `burrow daemon status`, or `burrow daemon stop`.",
                    context={"socket": str(self.paths.socket_path)},
                )
            self.paths.socket_path.unlink(missing_ok=True)

    def _write_pidfile(self) -> None:
        self.paths.pid_path.parent.mkdir(parents=True, exist_ok=True)
        self.paths.pid_path.write_text(str(os.getpid()), encoding="utf-8")

    def _remove_pidfile(self) -> None:
        self.paths.pid_path.unlink(missing_ok=True)

    def _install_signal_handlers(self) -> None:
        """Wire SIGINT/SIGTERM to a graceful stop.

        ``add_signal_handler`` is unavailable on Windows event loops, so the
        fallback is a plain KeyboardInterrupt path — the same graceful shutdown,
        just triggered one level up.
        """
        loop = asyncio.get_running_loop()
        for signal_name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, signal_name, None)
            if sig is None:
                continue
            # Windows does not implement add_signal_handler, and the loop may
            # already be closed by the time a late signal arrives. Neither is a
            # failure: this is a convenience over the IPC `daemon.stop` path.
            #
            # The callback is a bound method rather than a lambda. It routes
            # through _begin_shutdown so the signal path gets the same strong task
            # reference the IPC path does — it used to drop the task on the floor,
            # which is the same dangling-task bug in the one path that no test
            # exercises and no developer triggers by hand. A named method also
            # gives mypy a signature to check, which a default-argument lambda
            # does not: mypy reports "cannot infer type of lambda" and stops.
            with contextlib.suppress(
                NotImplementedError, RuntimeError
            ):  # pragma: no cover - Windows
                loop.add_signal_handler(sig, self._on_signal, sig)

    @property
    def uptime_seconds(self) -> float:
        return (now() - self._started_at).total_seconds()

    # --- control-plane handlers -------------------------------------------
    def handler_map(self) -> dict[str, AnyHandler]:
        """Method name -> handler, for this daemon.

        A mapping rather than a sequence of ``register`` calls, and that is the
        point: the CLI called 23 methods that nothing served, and the only way to
        notice was to run the daemon and read the error back. A mapping can be
        interrogated without a socket, so the drift alarm in
        ``openburrow-cli/tests/test_daemon_method_parity.py`` can assert that gap
        is empty rather than waiting for someone to hit it in the field.

        Public because the test is not the only legitimate reader: the relay and
        the web surface both want the request surface without opening a
        connection.
        """
        from openburrow.daemon.handlers import Handlers

        # Value type includes ``StreamHandler``: ``bus.stream`` answers with an
        # async generator, and the dispatcher branches on that at call time.
        handlers: dict[str, AnyHandler] = {
            # control plane
            "daemon.ping": self._h_ping,
            "daemon.status": self._h_status,
            "daemon.stop": self._h_stop,
            "daemon.health": self._h_health,
            # sessions
            "session.create": self._h_session_create,
            "session.list": self._h_session_list,
            "session.show": self._h_session_show,
            "session.close": self._h_session_close,
            "session.resume": self._h_session_resume,
            # lanes
            "lane.start": self._h_lane_start,
            "lane.stop": self._h_lane_stop,
            "lane.status": self._h_lane_status,
            "lane.prompt": self._h_lane_prompt,
            # tasks
            "task.list": self._h_task_list,
            "task.get": self._h_task_get,
            # bus
            "bus.stream": self._h_bus_stream,
            "bus.tail": self._h_bus_tail,
            "notify.test": self._h_notify_test,
            "hook.list": self._h_hook_list,
            "standup.generate": self._h_standup,
            # adapters and policy
            "adapters.list": self._h_adapters_list,
            "governance.policy_check": self._h_policy_check,
            "governance.policy_show": self._h_policy_show,
        }

        # The coordination, knowledge, audit, export and Radar surface lives in
        # ``handlers.py``. Those were the 23 methods the CLI called and nothing
        # served: the engines were complete and correct, and every one of those
        # commands failed with `no handler for '<method>'`. Keeping the request
        # surface out of this file also leaves the lifecycle logic — which is
        # what the rest of this module is — readable on its own.
        handlers.update(Handlers(self).registrations())
        return handlers

    def _register_handlers(self) -> None:
        assert self.ipc is not None
        for method, handler in self.handler_map().items():
            self.ipc.register(method, handler)

    # --- daemon handlers ---------------------------------------------------
    async def _h_ping(self, params: dict) -> dict:
        self._requests_served += 1
        return {"pong": True, "version": __version__, "pid": os.getpid()}

    async def _h_status(self, params: dict) -> dict:
        assert self.sessions is not None and self.registry is not None
        return {
            "running": True,
            "version": __version__,
            "pid": os.getpid(),
            "uptime_s": round(self.uptime_seconds, 1),
            "endpoint": self.paths.ipc_endpoint,
            "repo_root": str(self.paths.repo_root),
            "requests_served": self._requests_served,
            **self.sessions.status(),
            "recovery": self.recovery.status() if self.recovery is not None else {},
            "chaos": self.chaos.status() if self.chaos is not None else {},
            "relay": self.relay.status() if self.relay is not None else {"enabled": False},
        }

    def _begin_shutdown(self, reason: str) -> None:
        """Start shutdown in the background, keeping a strong reference.

        asyncio holds only a weak reference to a running task, so a
        fire-and-forget shutdown can be garbage-collected before it ever runs —
        and then the caller has been told the daemon is stopping while it stays
        up. A command that reports success without doing the thing is the failure
        mode this project works hardest to avoid, and a shutdown that silently
        does not happen is its worst instance: the CLI says the daemon stopped,
        so nobody looks again.

        Shared by the IPC handler and the signal handler, because both need the
        same guarantee and the signal path is the one nobody exercises by hand —
        which is exactly what made it the easier of the two to leave broken.

        The done-callback exists because a task nobody awaits also swallows its
        own exception. Without it, a shutdown that raised would leave the daemon
        running with no log line explaining why.
        """
        task = asyncio.create_task(self.stop(reason=reason))
        self._shutdown_task = task
        task.add_done_callback(self._on_shutdown_finished)

    def _on_signal(self, sig: Any) -> None:
        """Bridge a POSIX signal to shutdown."""
        self._begin_shutdown(f"signal {sig.name}")

    async def _h_stop(self, params: dict) -> dict:
        self._begin_shutdown(str(params.get("reason", "cli request")))
        return {"stopping": True}

    def _on_shutdown_finished(self, task: asyncio.Task[Any]) -> None:
        """Surface an exception from the detached shutdown task.

        Nothing awaits ``stop()``, so without this the exception would surface
        only as asyncio's "task exception was never retrieved", logged at
        garbage-collection time — by which point the process has gone quiet and
        the message reads as noise rather than as the cause.
        """
        self._shutdown_task = None
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error("daemon.shutdown_task_failed", error=str(exc), exc_info=exc)

    async def _h_health(self, params: dict) -> dict:
        assert self.database is not None and self.registry is not None and self.bus is not None
        return {
            "database": await self.database.healthcheck(),
            "adapters": self.registry.available(),
            "bus": {"subscribers": self.bus.subscriber_count},
            "governance": {
                "enabled": self.config.governance.enabled,
                "max_delegation_depth": self.config.governance.max_delegation_depth,
                "authority_inheritance": self.config.governance.authority_inheritance,
            },
            "recovery": self.recovery.status() if self.recovery is not None else {},
            "notifications": {
                "enabled": self.config.settings.notify_enabled,
                "desktop": self.config.settings.notify_desktop,
                "webhooks": [
                    channel
                    for channel, url in (
                        ("slack", self.config.settings.slack_webhook_url),
                        ("discord", self.config.settings.discord_webhook_url),
                        ("teams", self.config.settings.teams_webhook_url),
                    )
                    if url
                ],
                "hooks_loaded": len(self.notifier.hooks) if self.notifier is not None else 0,
            },
            "chaos": self.chaos.status() if self.chaos is not None else {"enabled": False},
        }

    # --- session handlers --------------------------------------------------
    async def _h_session_create(self, params: dict) -> dict:
        assert self.sessions is not None
        session = await self.sessions.create_session(
            name=str(params.get("name") or "session"),
            description=str(params.get("description") or ""),
            branch=str(params.get("branch") or ""),
            base_branch=str(params.get("base_branch") or ""),
            owner=str(params.get("owner") or ""),
            tags=list(params.get("tags") or []),
            lanes=params.get("lanes"),
        )
        return session.model_dump(mode="json")

    async def _h_session_list(self, params: dict) -> list[dict]:
        assert self.database is not None
        async with self.database.session() as db_session:
            repo = Repository(db_session)
            open_only = bool(params.get("open_only", True))
            sessions = (
                await repo.open_sessions()
                if open_only
                else await repo.list_by(
                    __import__("openburrow.core.models", fromlist=["Session"]).Session
                )
            )
        return [s.summary() for s in sessions]

    async def _h_session_show(self, params: dict) -> dict:
        assert self.sessions is not None
        session = await self.sessions.get_session(str(params.get("session", "")))
        lanes = [r.lane.model_dump(mode="json") for r in self.sessions.running_lanes(session.id)]
        return {"session": session.model_dump(mode="json"), "running_lanes": lanes}

    async def _h_session_resume(self, params: dict) -> dict:
        """Stage 12 resume: consume checkpoints and bring the lanes back up.

        Deliberately an IPC method rather than a `Sessions` internal: a resume
        is a first-class user action (`burrow session resume`), and the parity
        test holds every CLI-called method to the same registered-handler
        standard the original 23 were held to.
        """
        assert self.recovery is not None
        return await self.recovery.resume_session(
            str(params.get("session", "")),
            reason=str(params.get("reason") or "manual"),
        )

    async def _h_session_close(self, params: dict) -> dict:
        assert self.sessions is not None
        status = str(params.get("status") or "completed")
        session = await self.sessions.close_session(
            str(params.get("session", "")),
            status=SessionStatus(status)
            if status in {s.value for s in SessionStatus}
            else SessionStatus.COMPLETED,
        )
        await self._post_completion(session, status)
        return session.model_dump(mode="json")

    async def _post_completion(self, session: Any, status: str) -> None:
        """Fire the after-close integrations (items 226/227/228 email).

        Deliberately fire-and-forget: the session is already closed, so none of
        these can fail it. Every path reports into the log and the bus, never
        raises.
        """
        settings = self.config.settings
        tasks: list[asyncio.Task[None]] = []
        if settings.github_pr_on_complete or settings.github_status_checks:
            tasks.append(asyncio.create_task(self._github_on_complete(session, status)))
        if settings.email_digest_enabled:
            tasks.append(asyncio.create_task(self._email_on_complete(session)))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _github_on_complete(self, session: Any, status: str) -> None:
        """Open the PR and/or post the commit status for a finished session."""
        from openburrow.daemon.github import open_pull_request, post_commit_status

        settings = self.config.settings
        if not settings.github_repo:
            log.warning("github.unconfigured", pr=settings.github_pr_on_complete)
            return
        success = status == "completed"
        summary = f"OpenBurrow session {session.name or session.id}: {status}"

        if settings.github_pr_on_complete:
            result = await open_pull_request(
                token=settings.github_token,
                repo=settings.github_repo,
                head_branch=session.branch,
                base_branch=session.base_branch,
                title=summary,
                body=(
                    f"Automated PR for OpenBurrow session `{session.name or session.id}`.\n\n"
                    f"Status: **{status}**\n\n"
                    "Generated by OpenBurrow — see the session reel for the full trace."
                ),
            )
            await self._github_report("github.pr", result, session.id)

        if settings.github_status_checks and session.head_commit:
            result = await post_commit_status(
                token=settings.github_token,
                repo=settings.github_repo,
                commit_sha=session.head_commit,
                state="success" if success else "failure",
                description=summary[:140],
            )
            await self._github_report("github.status", result, session.id)

    async def _github_report(self, event_type: str, result: Any, session_id: str) -> None:
        """Record one GitHub delivery outcome on the bus and in the log."""
        assert self.bus is not None
        level = log.info if result.ok else log.warning
        level(event_type, ok=result.ok, detail=result.detail)
        await self.bus.emit(
            event_type=event_type,
            session_id=session_id,
            summary=("opened" if result.ok else "failed") + f": {result.detail[:200]}",
            payload={"ok": result.ok, "detail": result.detail, "url": result.url},
        )

    async def _email_on_complete(self, session: Any) -> None:
        """Email the standup for this session to the digest recipients."""
        from openburrow.daemon.notify import build_standup, send_email_digest

        assert self.database is not None
        settings = self.config.settings
        async with self.database.session() as db_session:
            events = await BusEventLog(db_session).stream(session_id=session.id, limit=100_000)
        report = build_standup(events, session_name=session.name)
        sent = await asyncio.to_thread(
            send_email_digest,
            report,
            host=settings.smtp_host,
            port=settings.smtp_port,
            sender=settings.email_from,
            recipients=list(settings.email_digest_recipients),
            user=settings.smtp_user,
            password=settings.smtp_password,
            use_tls=settings.smtp_use_tls,
        )
        if sent:
            log.info("notify.email_sent", recipients=sent, session=session.id)

    # --- lane handlers -----------------------------------------------------
    async def _h_lane_start(self, params: dict) -> dict:
        assert self.sessions is not None
        session = await self.sessions.get_session(str(params.get("session", "")))
        lane = await self.sessions.start_lane(
            session,
            name=str(params.get("name") or "lane"),
            harness=str(params.get("harness") or self.config.adapters.default),
            role=str(params.get("role") or "implementer"),
            owner=str(params.get("owner") or ""),
            claims=list(params.get("claims") or []),
        )
        return lane.model_dump(mode="json")

    async def _h_lane_stop(self, params: dict) -> dict:
        assert self.sessions is not None
        session = await self.sessions.get_session(str(params.get("session", "")))
        lane = await self.sessions.get_lane(session.id, str(params.get("lane", "")))
        stopped = await self.sessions.stop_lane(lane.id, reason=str(params.get("reason", "")))
        return {"stopped": stopped, "lane_id": lane.id}

    async def _h_lane_status(self, params: dict) -> list[dict]:
        assert self.sessions is not None
        session = await self.sessions.get_session(str(params.get("session", "")))
        return [r.lane.model_dump(mode="json") for r in self.sessions.running_lanes(session.id)]

    async def _h_lane_prompt(self, params: dict) -> dict:
        assert self.sessions is not None
        session = await self.sessions.get_session(str(params.get("session", "")))
        lane = await self.sessions.get_lane(session.id, str(params.get("lane", "")))
        running = next((r for r in self.sessions.running_lanes() if r.lane.id == lane.id), None)
        if running is None:
            raise SessionNotFoundError(
                f"lane {lane.name!r} is not currently running",
                hint="Start it with `burrow lane start`, or check `burrow session show`.",
                context={"lane_id": lane.id},
            )
        prompt = str(params.get("prompt") or "")
        await running.adapter.send_prompt(lane, prompt)
        await self.bus.emit(  # type: ignore[union-attr]
            event_type="lane.prompted",
            session_id=session.id,
            lane_id=lane.id,
            summary=prompt[:200],
            payload={"prompt": prompt[:4096], "by": str(params.get("by") or "")},
        )
        return {"delivered": True, "lane_id": lane.id}

    # --- notify / hooks / standup handlers ---------------------------------
    async def _h_notify_test(self, params: dict) -> dict:
        """Send one synthetic notification through every configured channel.

        The only honest way to test a notify setup is to fire it: a dry-run
        that rendered the payload would verify the formatter and nothing else.
        """
        if self.notifier is None:
            return {"dispatched": False, "reason": "notify_enabled is false"}
        summary = str(params.get("summary") or "OpenBurrow notification test")
        await self.notifier.handle_event(
            {
                "event_type": "notify.test",
                "session_id": str(params.get("session") or ""),
                "summary": summary,
                "payload": {"test": True},
            }
        )
        return {
            "dispatched": True,
            "desktop": self.config.settings.notify_desktop,
            "webhooks": [name for name, url, _ in self.notifier._webhooks()],
            "hooks": [hook.name for hook in self.notifier.hooks],
        }

    async def _h_hook_list(self, params: dict) -> list[dict]:
        """The hooks the daemon loaded, so `burrow hook list` reads the truth."""
        if self.notifier is None:
            return []
        return [
            {"name": hook.name, "command": hook.command, "on": hook.on}
            for hook in self.notifier.hooks
        ]

    async def _h_standup(self, params: dict) -> dict:
        """The overnight summary, built from the log (item 228)."""
        from openburrow.daemon.notify import build_standup

        assert self.sessions is not None and self.database is not None
        session = await self.sessions.get_session(str(params.get("session", "")))
        async with self.database.session() as db_session:
            events = await BusEventLog(db_session).stream(session_id=session.id, limit=100_000)
        return build_standup(events, session_name=session.name)

    # --- task handlers -----------------------------------------------------
    async def _h_task_list(self, params: dict) -> list[dict]:
        assert self.database is not None and self.sessions is not None
        session = await self.sessions.get_session(str(params.get("session", "")))
        async with self.database.session() as db_session:
            repo = Repository(db_session)
            tasks = await (
                repo.blocking_tasks(session.id)
                if params.get("blocking_only")
                else repo.open_tasks(session.id)
            )
        return [t.summary() for t in tasks]

    async def _h_task_get(self, params: dict) -> dict | None:
        assert self.database is not None
        async with self.database.session() as db_session:
            task = await Repository(db_session).get(A2ATask, str(params.get("task", "")))
        return task.model_dump(mode="json") if task else None

    # --- bus handlers ------------------------------------------------------
    async def _h_bus_stream(self, params: dict) -> AsyncGenerator[dict[str, Any], None]:
        """Stream bus events. Returns an async generator so IPC frames each event."""
        assert self.bus is not None
        subscription = self.bus.subscribe(
            session_id=str(params.get("session") or ""),
            event_types=set(params.get("event_types") or []) or None,
        )
        idle = float(params.get("idle_timeout") or 30.0)
        async for event in self.bus.listen(subscription, idle_timeout=idle):
            yield event

    async def _h_bus_tail(self, params: dict) -> list[dict]:
        assert self.database is not None
        async with self.database.session() as db_session:
            log_reader = BusEventLog(db_session)
            return await log_reader.stream(
                session_id=str(params.get("session") or ""),
                since_seq=int(params.get("since_seq") or 0),
                limit=int(params.get("limit") or 100),
            )

    # --- adapter handlers --------------------------------------------------
    async def _h_adapters_list(self, params: dict) -> list[dict]:
        assert self.registry is not None
        return self.registry.describe_all()

    async def _h_policy_check(self, params: dict) -> dict:
        """Dry-run the policy gate.

        Exposed over IPC so the relay and the web dashboard consult the same
        object the daemon uses to decide whether to start a lane. A second
        implementation behind the HTTP surface is exactly how the CLI's copy
        drifted from its own documentation.
        """
        assert self.sessions is not None
        gate = self.sessions.policy_gate
        verdict = gate.check(
            str(params.get("command", "")),
            path=str(params.get("path", "") or ""),
            role=str(params.get("role", "") or "") or None,
        )
        return {**verdict.as_dict(), "enforce": gate.enforce}

    async def _h_policy_show(self, params: dict) -> dict:
        """The effective policy plus the settings that would do nothing."""
        assert self.sessions is not None
        gate = self.sessions.policy_gate
        return {
            **gate.policy.model_dump(mode="json"),
            "diagnostics": list(gate.diagnose()),
        }


def _pid_is_alive(pid: int) -> bool:
    """Cross-platform liveness check for a PID."""
    try:
        import psutil  # type: ignore[import-untyped]

        return psutil.pid_exists(pid)
    except ImportError:
        pass
    if is_windows():
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    else:
        return True


async def run_daemon(repo_root: Path | str | None = None) -> int:
    """Entry point used by ``burrow daemon start`` and the console script."""
    config = load_config(repo_root)
    daemon = Daemon(config)
    try:
        await daemon.start()
    except DaemonAlreadyRunningError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"daemon failed to start: {exc}", file=sys.stderr)
        return 1

    try:
        await daemon.serve_forever()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await daemon.stop(reason="shutdown")
    return 0


__all__ = ["Daemon", "run_daemon"]
