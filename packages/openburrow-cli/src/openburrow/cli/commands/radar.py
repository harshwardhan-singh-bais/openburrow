"""Merge Radar commands: what each lane intends, and where they will collide.

The Radar is the only part of OpenBurrow that tries to prevent a collision
rather than report one, so its rendering has to keep two things apart that are
easy to blur:

**Certainty is not confidence.** ``both lanes claim src/api/routes.py`` is a fact
anyone can check in the database. ``the judge thinks these two descriptions
collide`` is a guess. They are drawn with different words, because a guess shown
with a fact's weight stops being believed within a week — which is when people
mute the Radar, and a muted Radar is worth nothing.

**Silence is not agreement.** When the judge is unavailable, no semantic signal
existed. ``burrow radar status`` says that out loud beside the coverage number,
rather than letting "0 semantic conflicts" read as "the codebase is calm".
"""

from __future__ import annotations

import asyncio
from typing import Annotated

import typer

from openburrow.cli.context import CliContext, short_id, truncate
from openburrow.cli.output import (
    console,
    emit,
    failure,
    info,
    print_kv,
    section,
    state,
    success,
    table,
    warn,
)
from openburrow.daemon.ipc import IpcClient

app = typer.Typer(
    help="Predict cross-lane collisions before they cost anyone a day.",
    no_args_is_help=True,
)


# ---------------------------------------------------------------------------
# burrow radar scan
# ---------------------------------------------------------------------------
@app.command("scan")
def scan(
    ctx: typer.Context,
    session: Annotated[str, typer.Option("--session", "-s", help="Session id or name.")] = "",
) -> None:
    """Scan every pair of lanes and report conflicts not yet announced.

    Each announced conflict is written to the bus log as a ``radar.conflict``
    event, so the warning is replayable. The whole claim the Radar makes is that
    it spoke *before* the collision, and that claim is unverifiable unless the
    speaking was recorded.
    """
    context: CliContext = ctx.obj
    client = asyncio.run(context.require_daemon())
    session_id = session or _latest_session(client)
    data = asyncio.run(client.call("radar.scan", session=session_id))

    def render(result: dict) -> None:
        if not result.get("enabled", True):
            warn(str(result.get("reason") or "the Radar is disabled"))
            return

        announced = list(result.get("announced") or [])
        if announced:
            warn(f"{len(announced)} conflict(s) announced — each is a prompt to negotiate")
        else:
            success("no new conflicts this scan")
        _render_conflicts(announced, dict(result.get("lanes") or {}))
        _render_stats(dict(result.get("stats") or {}), dict(result.get("judge") or {}))

    emit(context, data, human_renderer=render)


# ---------------------------------------------------------------------------
# burrow radar intents
# ---------------------------------------------------------------------------
@app.command("intents")
def intents(
    ctx: typer.Context,
    session: Annotated[str, typer.Option("--session", "-s", help="Session id or name.")] = "",
) -> None:
    """Show what the Radar believes each lane is working on.

    Re-derived from claims and plan steps on every call, so this is the same
    input the scan uses. When a lane shows nothing, that is the honest answer:
    nothing was claimed and no step named its files, so the Radar has no
    structural signal for it at all.
    """
    context: CliContext = ctx.obj
    client = asyncio.run(context.require_daemon())
    session_id = session or _latest_session(client)
    data = asyncio.run(client.call("radar.intents", session=session_id))

    def render(result: dict) -> None:
        rows = list(result.get("intents") or [])
        if not rows:
            info("no lanes in this session, so there is nothing to compare")
            return
        table(
            ["lane", "status", "files", "steps", "risk", "source"],
            [
                [
                    _lane_label(row),
                    state(row.get("status")),
                    _files_cell(list(row.get("files") or []), list(row.get("directories") or [])),
                    len(row.get("step_ids") or []),
                    row.get("risk_tier"),
                    str(row.get("source") or ""),
                ]
                for row in rows
            ],
            caption="Files come from claims and plan steps only — never from a model.",
        )
        judge = dict(result.get("judge") or {})
        if not judge.get("configured"):
            info(f"judge: not in use — {judge.get('reason') or 'not configured'}")

    emit(context, data, human_renderer=render)


# ---------------------------------------------------------------------------
# burrow radar status
# ---------------------------------------------------------------------------
@app.command("status")
def status(
    ctx: typer.Context,
    session: Annotated[
        str, typer.Option("--session", "-s", help="Restrict the lane list to one session.")
    ] = "",
) -> None:
    """Counters and judge coverage — whether to trust what the Radar has said.

    ``judge coverage`` is the fraction of judge consultations that produced a
    usable verdict. A Radar that found nothing because the judge was unreachable
    is a different world from one that found nothing because nothing is
    colliding, and this command is the difference between them.
    """
    context: CliContext = ctx.obj
    client = asyncio.run(context.require_daemon())
    data = asyncio.run(client.call("radar.status", session=session))

    def render(result: dict) -> None:
        if not result.get("enabled", True):
            warn("the Radar is disabled (OPENBURROW_RADAR_ENABLED=false)")
            return
        _render_stats(dict(result.get("stats") or {}), dict(result.get("judge") or {}))
        thresholds = dict(result.get("thresholds") or {})
        if thresholds:
            print_kv(
                {
                    "semantic threshold": thresholds.get("semantic"),
                    "scope threshold": thresholds.get("scope"),
                    "lanes tracked": result.get("lanes_tracked", 0),
                }
            )
        tracked = list(result.get("intents") or [])
        if tracked:
            section("Intents")
            for line in tracked[:20]:
                console.print(f"  [dim]{truncate(str(line), 90)}[/dim]")

    emit(context, data, human_renderer=render)


# ---------------------------------------------------------------------------
# rendering helpers
# ---------------------------------------------------------------------------
def _render_conflicts(rows: list[dict], lanes: dict[str, str]) -> None:
    """One block per conflict, with certainty stated in words rather than implied."""
    for row in rows:
        label = (
            "[bold red]conflict[/bold red]" if row.get("certain") else "[yellow]possible[/yellow]"
        )
        console.print(
            f"  {label} {_lane_name(str(row.get('lane_a', '')), lanes)}"
            f" ↔ {_lane_name(str(row.get('lane_b', '')), lanes)}"
        )
        console.print(f"    [dim]{row.get('reason')}[/dim]")
        files = list(row.get("files") or [])
        if files:
            console.print(f"    [cyan]{', '.join(files[:3])}[/cyan]")
        if row.get("recommended_action"):
            console.print(f"    [dim]→ {row['recommended_action']}[/dim]")
        judge = row.get("judge")
        if isinstance(judge, dict) and not judge.get("known", True):
            console.print("    [dim]the judge could not answer — a prediction only[/dim]")


def _render_stats(stats: dict, judge: dict) -> None:
    if not stats:
        info("no scan has run in this daemon yet")
        return
    print_kv(
        {
            "scans": stats.get("scans", 0),
            "pairs evaluated": stats.get("pairs_evaluated", 0),
            "conflicts": stats.get("conflicts", 0),
            "certain": stats.get("certain_conflicts", 0),
            "scope": stats.get("scope_conflicts", 0),
            "semantic": stats.get("semantic_conflicts", 0),
            "suppressed repeats": stats.get("suppressed", 0),
            "judge calls": stats.get("judge_calls", 0),
            "judge coverage": stats.get("judge_coverage", 0.0),
        }
    )
    if not judge.get("configured"):
        info(f"judge: not in use — {judge.get('reason') or 'not configured'}")
    elif not stats.get("semantic_available"):
        warn(
            "the judge was consulted but rarely answered, so no semantic signal "
            "existed for that period"
        )


def _lane_name(lane_id: str, lanes: dict[str, str]) -> str:
    name = lanes.get(lane_id)
    return f"[cyan]{name}[/cyan]" if name else f"[cyan]{short_id(lane_id, 10)}[/cyan]"


def _lane_label(row: dict) -> str:
    name = str(row.get("lane_name") or "")
    lane_id = str(row.get("lane_id") or "")
    return f"{name} ({short_id(lane_id, 8)})" if name else short_id(lane_id, 10)


def _files_cell(files: list[str], directories: list[str]) -> str:
    if not files:
        return "[dim]—[/dim]" if not directories else f"[dim]{directories[0]}/[/dim]"
    more = len(files) + len(directories) - 1
    return truncate(files[0], 32) + (f"[dim] +{more}[/dim]" if more else "")


def _latest_session(client: IpcClient) -> str:
    sessions = asyncio.run(client.call("session.list", open_only=True))
    if not sessions:
        failure("no open sessions")
        info("Start one with `burrow session start`.")
        raise typer.Exit(code=1)
    return str(sessions[0]["id"])


__all__ = ["app"]
