"""Daemon control commands.

``burrow daemon start`` deliberately supports two modes: **detached** (fork a
background process, the normal case) and **foreground** (``--foreground``, which
is what you use under a process supervisor like systemd or launchd). Getting
this wrong is why so many CLIs have a daemon that either dies when the terminal
closes or cannot be supervised.

Liveness is always checked by *pinging* the control socket, never by reading the
PID file. A PID file outlives a crashed daemon, so trusting it is how you get a
daemon that refuses to start after a crash.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
import time
from typing import Annotated

import typer

from openburrow.cli.context import CliContext
from openburrow.cli.output import (
    banner,
    console,
    emit,
    failure,
    info,
    print_kv,
    success,
    table,
    warn,
)
from openburrow.core.errors import OpenBurrowError

app = typer.Typer(help="Control the burrow daemon.", no_args_is_help=True)

START_TIMEOUT_S = 15.0


@app.command("start")
def start(
    ctx: typer.Context,
    foreground: Annotated[
        bool,
        typer.Option("--foreground", "-f", help="Run in this process (for process supervisors)."),
    ] = False,
    wait: Annotated[
        bool, typer.Option("--wait/--no-wait", help="Wait until the daemon answers.")
    ] = True,
) -> None:
    """Start the daemon.

    Detached mode re-executes this CLI as a child process with
    ``--foreground``, which keeps the daemon code path identical in both modes —
    a daemon that behaves differently when supervised is a daemon with two sets
    of bugs.
    """
    context: CliContext = ctx.obj

    if foreground:
        asyncio.run(_run_foreground(context))
        return

    already = asyncio.run(context.daemon_is_up())
    if already:
        warn("a burrow daemon is already running")
        info("Use `burrow daemon restart` to replace it.")
        raise typer.Exit(code=0)

    if context.is_json:
        info("starting daemon in the background")

    args = [
        sys.executable,
        "-m",
        "openburrow.cli.main",
        "--quiet",
        "daemon",
        "start",
        "--foreground",
    ]
    if context.repo_root:
        args = [*args[:3], "--repo", str(context.repo_root), *args[3:]]

    log_file = context.paths.logs_dir / "daemon.out"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handle = log_file.open("a", encoding="utf-8")

    kwargs: dict = {
        "stdout": handle,
        "stderr": handle,
        "stdin": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008
        )  # DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True

    process = subprocess.Popen(args, **kwargs)  # noqa: S603 - args are constructed, not user input

    if not wait:
        success(f"daemon starting (pid {process.pid})")
        info(f"logs: {log_file}")
        return

    client = context.daemon_client()
    deadline = time.time() + START_TIMEOUT_S
    while time.time() < deadline:
        if asyncio.run(client.ping()):
            payload = {"running": True, "pid": process.pid, "endpoint": client.paths.ipc_endpoint}

            def render(data: dict) -> None:
                success(f"daemon started (pid {data['pid']})")
                info(f"endpoint: {data['endpoint']}")
                info(f"logs: {log_file}")

            emit(context, payload, human_renderer=render)
            return
        if process.poll() is not None:
            failure("daemon exited during startup")
            info(f"Check the log: {log_file}")
            raise typer.Exit(code=1)
        time.sleep(0.25)

    failure(f"daemon did not become ready within {START_TIMEOUT_S:.0f}s")
    info(f"Check the log: {log_file}")
    raise typer.Exit(code=1)


async def _run_foreground(context: CliContext) -> None:
    from openburrow.daemon.server import run_daemon

    code = await run_daemon(context.repo_root)
    raise typer.Exit(code=code)


@app.command("stop")
def stop(
    ctx: typer.Context,
    force: Annotated[
        bool, typer.Option("--force", help="Send SIGKILL if a graceful stop hangs.")
    ] = False,
    timeout: Annotated[
        float, typer.Option("--timeout", help="Seconds to wait for a graceful stop.")
    ] = 30.0,
) -> None:
    """Stop the daemon, draining in-flight work first."""
    context: CliContext = ctx.obj
    client = context.daemon_client()

    if not asyncio.run(client.ping()):
        info("no daemon is running")
        return

    try:
        asyncio.run(client.call("daemon.stop", reason="cli request"))
    except OpenBurrowError as exc:
        warn(f"graceful stop request failed: {exc}")

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not asyncio.run(client.ping()):
            success("daemon stopped")
            return
        time.sleep(0.3)

    if force:
        pid = _read_pid(context)
        if pid:
            _kill(pid)
            success(f"daemon killed (pid {pid})")
            return
    failure(f"daemon did not stop within {timeout:.0f}s")
    info("Use --force to kill it, or inspect `burrow daemon status`.")
    raise typer.Exit(code=1)


@app.command("restart")
def restart(ctx: typer.Context) -> None:
    """Stop then start. Used after upgrading or changing configuration."""
    stop(ctx, force=False, timeout=30.0)
    start(ctx, foreground=False, wait=True)


@app.command("status")
def status(ctx: typer.Context) -> None:
    """Report whether the daemon is up, and what it is holding."""
    context: CliContext = ctx.obj
    client = context.daemon_client()

    if not asyncio.run(client.ping()):
        payload = {"running": False, "endpoint": client.paths.ipc_endpoint}
        emit(
            context,
            payload,
            human_renderer=lambda data: failure(
                f"daemon is not running (endpoint {data['endpoint']})"
            ),
        )
        raise typer.Exit(code=1)

    data = asyncio.run(client.call("daemon.status"))

    def render(result: dict) -> None:
        banner(
            "burrow daemon",
            subtitle=f"pid {result.get('pid')} · up {result.get('uptime_s', 0):.0f}s",
            style="green",
        )
        print_kv(
            {
                "version": result.get("version"),
                "endpoint": result.get("endpoint"),
                "repo": result.get("repo_root"),
                "sessions": result.get("sessions"),
                "lanes": result.get("lanes"),
                "watchers": result.get("watchers"),
                "subscribers": result.get("subscribers"),
                "requests": result.get("requests_served"),
            }
        )
        lanes = result.get("lane_detail") or []
        if lanes:
            console.print()
            table(
                ["lane", "harness", "status", "a2a", "pid"],
                [
                    [
                        lane.get("name"),
                        lane.get("harness"),
                        lane.get("status"),
                        lane.get("a2a") or "-",
                        lane.get("pid") or "-",
                    ]
                    for lane in lanes
                ],
            )

    emit(context, data, human_renderer=render)


@app.command("logs")
def logs(
    ctx: typer.Context,
    lines: Annotated[int, typer.Option("--lines", "-n", help="How many lines to show.")] = 80,
    follow: Annotated[bool, typer.Option("--follow", "-f", help="Keep streaming.")] = False,
) -> None:
    """Show the daemon's own log file."""
    context: CliContext = ctx.obj
    log_file = context.paths.logs_dir / "daemon.log"

    if not log_file.exists():
        info(f"no daemon log at {log_file}")
        return

    if not follow:
        content = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in content[-lines:]:
            console.print(line, markup=False, highlight=False)
        return

    with log_file.open("r", encoding="utf-8", errors="replace") as handle:
        handle.seek(0, os.SEEK_END)
        try:
            while True:
                line = handle.readline()
                if not line:
                    time.sleep(0.3)
                    continue
                console.print(line.rstrip(), markup=False, highlight=False)
        except KeyboardInterrupt:
            return


def _read_pid(context: CliContext) -> int | None:
    pid_file = context.paths.pid_path
    if not pid_file.exists():
        return None
    try:
        return int(pid_file.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None


def _kill(pid: int) -> None:
    import signal

    # The process may already be gone. Killing a corpse is not an error when
    # the caller's intent is "make sure this is not running".
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGKILL if os.name != "nt" else signal.SIGTERM)


__all__ = ["app"]
