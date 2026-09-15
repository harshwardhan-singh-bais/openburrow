"""Observability commands: watch, logs, report, export, replay.

``burrow report`` is the honest-numbers command. It surfaces the metrics that
answer whether the collaboration is real rather than decorative —
``negotiation_precision`` (did opening a negotiation actually prevent a
collision?) and ``message_effectiveness`` (did an inbound message change what a
lane did?). A bus with high volume and low effectiveness is expensive noise, and
these numbers are what make that visible instead of assumed away.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Annotated

import typer

from openburrow.cli.context import CliContext, human_size, short_id, truncate
from openburrow.cli.output import (
    banner,
    console,
    emit,
    failure,
    info,
    lane_line,
    negotiation_view,
    print_kv,
    section,
    success,
    table,
)

app = typer.Typer(help="Live view, logs, reports, and replay.", no_args_is_help=True)


# ---------------------------------------------------------------------------
# burrow watch
# ---------------------------------------------------------------------------
@app.command("watch")
def watch(
    ctx: typer.Context,
    session: Annotated[str, typer.Argument(help="Session id or name.")] = "",
    interval: Annotated[
        float, typer.Option("--interval", help="Refresh interval in seconds.")
    ] = 2.0,
    once: Annotated[bool, typer.Option("--once", help="Print one frame and exit.")] = False,
) -> None:
    """Live multi-lane board plus the A2A conversation feed.

    Refreshes in place using ANSI cursor control rather than a full TUI, so it
    works over any terminal and degrades gracefully when output is piped. For
    the full interactive experience, ``burrow tui`` uses Textual.
    """
    context: CliContext = ctx.obj
    client = asyncio.run(context.require_daemon())
    session_id = session or _latest_session(client)

    def frame() -> str:
        status = asyncio.run(client.call("daemon.status"))
        lanes = list(status.get("lane_detail", []))
        events = asyncio.run(client.call("bus.tail", session=session_id, limit=12))
        approvals = asyncio.run(client.call("approvals.list", session=session_id))

        lines: list[str] = []
        lines.append(f"[bold]OpenBurrow[/bold]  [dim]{time.strftime('%H:%M:%S')}[/dim]")
        lines.append("")

        if approvals:
            lines.append(f"[yellow]{len(approvals)} approval(s) waiting[/yellow]")
        else:
            lines.append("[dim]no approvals waiting[/dim]")
        lines.append("")

        lines.append("[bold]Lanes[/bold]")
        if lanes:
            for lane in lanes:
                lines.append("  " + lane_line(lane))
        else:
            lines.append("  [dim]none running[/dim]")

        lines.append("")
        lines.append("[bold]A2A feed[/bold]")
        if events:
            for event in reversed(events[-10:]):
                kind = event.get("event_type", "?")
                summary = truncate(str(event.get("summary") or ""), 56)
                lines.append(f"  [dim]{event.get('seq', '?'):>5}[/dim] {kind:<24} {summary}")
        else:
            lines.append("  [dim]quiet[/dim]")

        return "\n".join(lines)

    if once or context.is_json:
        rendered = frame()
        if context.is_json:
            status = asyncio.run(client.call("daemon.status"))
            events = asyncio.run(client.call("bus.tail", session=session_id, limit=20))
            emit(context, {"status": status, "events": events})
        else:
            console.print(rendered)
        return

    try:
        first = True
        while True:
            output = frame()
            if not first:
                console.print("\x1b[2J\x1b[H", end="")
            console.print(output)
            first = False
            time.sleep(interval)
    except KeyboardInterrupt:
        console.print("\n[dim]stopped[/dim]")


# ---------------------------------------------------------------------------
# burrow logs
# ---------------------------------------------------------------------------
@app.command("logs")
def logs(
    ctx: typer.Context,
    session: Annotated[str, typer.Argument(help="Session id or name.")] = "",
    lane: Annotated[str, typer.Option("--lane", "-l", help="Filter to one lane.")] = "",
    bus_only: Annotated[
        bool, typer.Option("--bus-only", help="Only inter-agent messages.")
    ] = False,
    follow: Annotated[bool, typer.Option("--follow", "-f", help="Stream new events.")] = False,
    limit: Annotated[int, typer.Option("--limit", "-n", help="How many events.")] = 100,
) -> None:
    """Structured log tail, per lane, filterable to bus-only."""
    context: CliContext = ctx.obj
    client = asyncio.run(context.require_daemon())
    session_id = session or _latest_session(client)

    def render_event(event: dict) -> None:
        seq = event.get("seq", "?")
        kind = str(event.get("event_type", "?"))
        lane_id = str(event.get("lane_id") or "")
        summary = truncate(str(event.get("summary") or ""), 70)
        lane_part = f" [cyan]{short_id(lane_id, 10)}[/cyan]" if lane_id else ""
        console.print(f"[dim]{seq:>6}[/dim]  {kind:<26}{lane_part}  {summary}")

    if follow:

        async def stream() -> None:
            try:
                async for event in client.stream(
                    "bus.stream",
                    session=session_id,
                    event_types=["bus.message.sent", "bus.message.received"] if bus_only else [],
                    idle_timeout=3600,
                ):
                    if lane and event.get("lane_id") != lane:
                        continue
                    render_event(event)
            except KeyboardInterrupt:
                return

        asyncio.run(stream())
        return

    events = asyncio.run(client.call("bus.tail", session=session_id, limit=limit))
    if lane:
        events = [event for event in events if event.get("lane_id") == lane]
    if bus_only:
        events = [
            event for event in events if str(event.get("event_type", "")).startswith("bus.message")
        ]

    if context.is_json:
        emit(context, events)
        return

    for event in events:
        render_event(event)
    if not events:
        info("no matching events")


# ---------------------------------------------------------------------------
# burrow report
# ---------------------------------------------------------------------------
@app.command("report")
def report(
    ctx: typer.Context,
    session: Annotated[str, typer.Argument(help="Session id or name.")] = "",
) -> None:
    """End-of-session summary: files, time, cost, negotiations, governance."""
    context: CliContext = ctx.obj
    client = asyncio.run(context.require_daemon())
    session_id = session or _latest_session(client)
    data = asyncio.run(client.call("report.generate", session=session_id))

    def render(result: dict) -> None:
        metrics = result.get("metrics") or {}
        banner("Session report", subtitle=result.get("session_name", ""), style="cyan")

        section("Totals")
        print_kv(
            {
                "duration": f"{metrics.get('duration_s', 0):.0f}s",
                "lanes": metrics.get("lanes"),
                "messages": metrics.get("messages"),
                "cost": f"${metrics.get('cost_usd', 0):.4f}",
                "tokens": metrics.get("tokens"),
            }
        )

        section("Collaboration quality")
        print_kv(
            {
                "negotiations issued": metrics.get("negotiations"),
                "collisions avoided": metrics.get("collisions_avoided"),
                "negotiation precision": f"{(metrics.get('negotiation_precision') or 0) * 100:.0f}%",
                "lessons promoted": metrics.get("lessons"),
                "brain entries": metrics.get("brain_entries"),
                "resumes": metrics.get("resumes"),
            }
        )

        section("Governance")
        print_kv(
            {
                "flags raised": metrics.get("governance_flags"),
                "approvals": metrics.get("approvals"),
            }
        )

        lanes = result.get("lanes") or []
        if lanes:
            section("Per lane")
            table(
                ["lane", "harness", "msgs", "effectiveness", "tasks", "cost", "crashes"],
                [
                    [
                        lane.get("lane_id"),
                        lane.get("harness"),
                        f"{lane.get('messages_sent', 0)}/{lane.get('messages_received', 0)}",
                        f"{(lane.get('message_effectiveness') or 0) * 100:.0f}%",
                        f"{lane.get('tasks_completed', 0)}/{lane.get('tasks_submitted', 0)}",
                        f"${lane.get('cost_usd', 0):.4f}",
                        lane.get("crash_count", 0),
                    ]
                    for lane in lanes
                ],
            )

        negotiations = result.get("negotiations") or []
        if negotiations:
            section("Negotiations")
            for exchange in negotiations:
                negotiation_view(exchange)

        if result.get("report_path"):
            info(f"written to {result['report_path']}")

    emit(context, data, human_renderer=render)


# ---------------------------------------------------------------------------
# burrow export / replay
# ---------------------------------------------------------------------------
@app.command("export")
def export(
    ctx: typer.Context,
    session: Annotated[str, typer.Argument(help="Session id or name.")],
    output: Annotated[Path | None, typer.Option("--output", "-o", help="Bundle directory.")] = None,
    sign: Annotated[
        bool, typer.Option("--sign", help="Sign the bundle for shareable links.")
    ] = False,
) -> None:
    """Export a session as a portable replay bundle.

    The bundle includes the terminal casts, the full A2A transcript, the causal
    timeline, and the audit log. The static HTML viewer needs no backend, which
    preserves the "a shared link just works" property.
    """
    context: CliContext = ctx.obj
    client = asyncio.run(context.require_daemon())
    data = asyncio.run(
        client.call("reel.export", session=session, output=str(output) if output else "", sign=sign)
    )

    def render(result: dict) -> None:
        success(f"exported to {result.get('path')}")
        print_kv(
            {
                "lanes": result.get("lane_count"),
                "events": result.get("event_count"),
                "negotiations": result.get("negotiation_count"),
                "governance events": result.get("governance_event_count"),
                "size": human_size(int(result.get("size_bytes") or 0)),
            }
        )
        if result.get("viewer_hint"):
            info(f"view: {result['viewer_hint']}")

    emit(context, data, human_renderer=render)


@app.command("replay")
def replay(
    ctx: typer.Context,
    session: Annotated[str, typer.Argument(help="Session id or name.")],
    speed: Annotated[float, typer.Option("--speed", help="Playback speed multiplier.")] = 1.0,
    from_seq: Annotated[int, typer.Option("--from", help="Start at this bus sequence number.")] = 0,
) -> None:
    """Replay a session in-terminal, including its negotiations.

    Reads the append-only log and re-emits events in order. Because the log is
    the same source the live bus wrote to, a replay cannot disagree with what
    actually happened — which is the whole reason the log is authoritative.
    """
    context: CliContext = ctx.obj
    client = asyncio.run(context.require_daemon())
    events = asyncio.run(client.call("bus.tail", session=session, since_seq=from_seq, limit=10000))

    if context.is_json:
        emit(context, events)
        return

    info(f"replaying {len(events)} event(s) at {speed:g}x — Ctrl-C to stop")
    try:
        for event in events:
            seq = event.get("seq", "?")
            kind = str(event.get("event_type", "?"))
            lane = str(event.get("lane_id") or "")
            summary = truncate(str(event.get("summary") or ""), 66)
            lane_part = f" [cyan]{short_id(lane, 10)}[/cyan]" if lane else ""
            console.print(f"[dim]{seq:>6}[/dim]  {kind:<26}{lane_part}  {summary}")
            time.sleep(max(0.0, 0.12 / max(speed, 0.01)))
    except KeyboardInterrupt:
        console.print("\n[dim]stopped[/dim]")


@app.command("tui")
def tui(
    ctx: typer.Context,
    session: Annotated[str, typer.Option("--session", "-s", help="Session id or name.")] = "",
) -> None:
    """Launch the full Textual TUI dashboard."""
    context: CliContext = ctx.obj
    try:
        from openburrow.cli.tui import BurrowTui
    except ImportError as exc:
        failure("the Textual TUI is unavailable")
        info("Install with: uv add textual")
        raise typer.Exit(code=1) from exc

    client = asyncio.run(context.require_daemon())
    session_id = session or _latest_session(client)
    BurrowTui(client, session_id).run()


def _latest_session(client) -> str:
    sessions = asyncio.run(client.call("session.list", open_only=True))
    if not sessions:
        failure("no open sessions")
        info("Start one with `burrow session start`.")
        raise typer.Exit(code=1)
    return str(sessions[0]["id"])


__all__ = ["app"]
