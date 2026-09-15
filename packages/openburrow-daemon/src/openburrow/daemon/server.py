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
from openburrow.daemon.ipc import IpcServer, socket_is_live
from openburrow.daemon.sessions import SessionManager

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

        # --- control plane ------------------------------------------------
        self.ipc = IpcServer(self.paths)
        self._register_handlers()
        endpoint = await self.ipc.start()

        self._install_signal_handlers()

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
    def _register_handlers(self) -> None:
        assert self.ipc is not None
        register = self.ipc.register

        register("daemon.ping", self._h_ping)
        register("daemon.status", self._h_status)
        register("daemon.stop", self._h_stop)
        register("daemon.health", self._h_health)

        register("session.create", self._h_session_create)
        register("session.list", self._h_session_list)
        register("session.show", self._h_session_show)
        register("session.close", self._h_session_close)

        register("lane.start", self._h_lane_start)
        register("lane.stop", self._h_lane_stop)
        register("lane.status", self._h_lane_status)
        register("lane.prompt", self._h_lane_prompt)

        register("task.list", self._h_task_list)
        register("task.get", self._h_task_get)

        register("bus.stream", self._h_bus_stream)
        register("bus.tail", self._h_bus_tail)

        register("adapters.list", self._h_adapters_list)

        register("governance.policy_check", self._h_policy_check)
        register("governance.policy_show", self._h_policy_show)

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

    async def _h_session_close(self, params: dict) -> dict:
        assert self.sessions is not None
        status = str(params.get("status") or "completed")
        session = await self.sessions.close_session(
            str(params.get("session", "")),
            status=SessionStatus(status)
            if status in {s.value for s in SessionStatus}
            else SessionStatus.COMPLETED,
        )
        return session.model_dump(mode="json")

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
    async def _h_bus_stream(self, params: dict):
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
