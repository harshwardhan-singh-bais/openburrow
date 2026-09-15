"""Session, lane, and messaging commands.

The daily-use surface. ``burrow ask`` is the one worth explaining: it sends a
direct A2A message to one lane and, with ``--wait``, blocks until the lane
replies — but the blocking is not a bespoke mechanism. The receiving task moves
to ``input_required``, which *is* A2A's representation of "waiting for an
answer". That is why a human's question and an agent's question are the same
event to every other part of the system.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

import typer

from openburrow.cli.context import CliContext, short_id, truncate
from openburrow.cli.output import (
    banner,
    console,
    emit,
    failure,
    info,
    lane_line,
    print_kv,
    section,
    session_header,
    state,
    success,
    table,
    warn,
)

app = typer.Typer(help="Start, inspect, and close sessions.", no_args_is_help=True)
lane_app = typer.Typer(help="Manage lanes within a session.", no_args_is_help=True)
app.add_typer(lane_app, name="lane")


# ---------------------------------------------------------------------------
# burrow session
# ---------------------------------------------------------------------------
@app.command("start")
def start(
    ctx: typer.Context,
    name: Annotated[str, typer.Option("--name", "-n", help="Session name.")] = "",
    description: Annotated[
        str, typer.Option("--description", "-d", help="What this session is for.")
    ] = "",
    harness: Annotated[
        str,
        typer.Option("--harness", help="Harness for an ad-hoc single lane, e.g. claude-code."),
    ] = "",
    lanes: Annotated[
        str,
        typer.Option("--lanes", help="Comma-separated lane templates: name:harness[:role]"),
    ] = "",
    branch: Annotated[str, typer.Option("--branch", help="Branch for the session.")] = "",
    base: Annotated[str, typer.Option("--base", help="Base branch.")] = "",
    tags: Annotated[str, typer.Option("--tags", help="Comma-separated labels.")] = "",
) -> None:
    """Start a session and bring up its lanes.

    Lane sources, in priority order: ``--lanes`` (explicit), ``--harness``
    (one ad-hoc lane), or the ``lanes:`` block in ``openburrow.yaml``.
    """
    context: CliContext = ctx.obj
    name = name or f"session-{int(asyncio.get_event_loop_policy().get_event_loop().time())}"

    specs: list[dict] | None = None
    if lanes:
        specs = []
        for index, spec in enumerate(part.strip() for part in lanes.split(",") if part.strip()):
            bits = spec.split(":")
            specs.append(
                {
                    "name": bits[0],
                    "harness": bits[1] if len(bits) > 1 else "mock",
                    "role": bits[2]
                    if len(bits) > 2
                    else ("implementer" if index == 0 else "reviewer"),
                }
            )
    elif harness:
        specs = [{"name": f"{harness}-1", "harness": harness, "role": "implementer"}]

    payload = {
        "name": name,
        "description": description,
        "branch": branch,
        "base_branch": base,
        "tags": [t.strip() for t in tags.split(",") if t.strip()],
        "lanes": specs,
    }

    client = asyncio.run(context.require_daemon())
    data = asyncio.run(client.call("session.create", **payload))

    def render(session: dict) -> None:
        banner("session started", style="green")
        session_header(session)
        lanes_data = session.get("lanes") or []
        if lanes_data:
            console.print()
            info(f"{len(lanes_data)} lane(s) coming up")

    emit(context, data, human_renderer=render)


@app.command("list")
def list_sessions(
    ctx: typer.Context,
    all_sessions: Annotated[
        bool, typer.Option("--all", "-a", help="Include closed sessions.")
    ] = False,
) -> None:
    """List sessions."""
    context: CliContext = ctx.obj
    client = asyncio.run(context.require_daemon())
    sessions = asyncio.run(client.call("session.list", open_only=not all_sessions))

    def render(data: list[dict]) -> None:
        table(
            ["id", "name", "status", "branch", "lanes", "duration", "cost", "nego", "flags"],
            [
                [
                    short_id(str(s.get("id", "")), 10),
                    s.get("name"),
                    state(s.get("status")),
                    s.get("branch"),
                    s.get("lanes"),
                    f"{s.get('duration_s', 0):.0f}s",
                    f"${s.get('cost_usd', 0):.4f}",
                    s.get("negotiations", 0),
                    s.get("governance_flags", 0),
                ]
                for s in data
            ],
            caption="Use `burrow session show <id>` for detail.",
        )

    emit(context, sessions, human_renderer=render)


@app.command("show")
def show(
    ctx: typer.Context,
    session: Annotated[str, typer.Argument(help="Session id, id prefix, or name.")],
) -> None:
    """Show a session and its lanes."""
    context: CliContext = ctx.obj
    client = asyncio.run(context.require_daemon())
    data = asyncio.run(client.call("session.show", session=session))

    def render(result: dict) -> None:
        session_data = result.get("session") or {}
        session_header(session_data)
        if session_data.get("description"):
            console.print(f"  [dim]{session_data['description']}[/dim]")

        lanes_data = result.get("running_lanes") or []
        if lanes_data:
            section("Lanes")
            for lane in lanes_data:
                console.print("  " + lane_line(lane))
                a2a = lane.get("a2a_endpoint")
                card = lane.get("agent_card_url")
                if a2a:
                    console.print(f"     [dim]a2a:  {a2a}[/dim]")
                if card:
                    console.print(f"     [dim]card: {card}[/dim]")
                if lane.get("worktree_path"):
                    console.print(f"     [dim]tree: {lane['worktree_path']}[/dim]")
        else:
            info("no lanes are currently running")

    emit(context, data, human_renderer=render)


@app.command("watch")
def watch(
    ctx: typer.Context,
    session: Annotated[str, typer.Argument(help="Session id or name.")] = "",
    follow: Annotated[bool, typer.Option("--follow", "-f", help="Stream the A2A feed.")] = False,
) -> None:
    """Watch a session's live bus feed.

    ``--follow`` streams until interrupted. Without it, the last events are
    printed and the command exits, which is what you want in a script.
    """
    context: CliContext = ctx.obj
    client = asyncio.run(context.require_daemon())
    session = session or _latest_session(client)
    data = asyncio.run(client.call("session.show", session=session))
    session_id = (data.get("session") or {}).get("id", session)

    if not follow:
        events = asyncio.run(client.call("bus.tail", session=session_id, limit=40))
        _render_events(events)
        return

    async def stream() -> None:
        info("streaming the A2A feed — Ctrl-C to stop")
        try:
            async for event in client.stream("bus.stream", session=session_id, idle_timeout=3600):
                _render_event(event)
        except KeyboardInterrupt:
            return

    asyncio.run(stream())


@app.command("close")
def close(
    ctx: typer.Context,
    session: Annotated[str, typer.Argument(help="Session id or name.")],
    status: Annotated[
        str, typer.Option("--status", help="completed|abandoned|failed.")
    ] = "completed",
) -> None:
    """Close a session, stopping its lanes and cleaning up worktrees."""
    context: CliContext = ctx.obj
    client = asyncio.run(context.require_daemon())

    if not context.assume_yes and not context.is_json:
        typer.confirm(f"Close session {session}?", abort=True)

    data = asyncio.run(client.call("session.close", session=session, status=status))
    emit(context, data, human_renderer=lambda _d: success(f"session closed ({status})"))


# ---------------------------------------------------------------------------
# burrow lane
# ---------------------------------------------------------------------------
@lane_app.command("start")
def lane_start(
    ctx: typer.Context,
    session: Annotated[str, typer.Argument(help="Session id or name.")],
    name: Annotated[str, typer.Option("--name", "-n", help="Lane name.")] = "",
    harness: Annotated[str, typer.Option("--harness", "-H", help="Harness to run.")] = "",
    role: Annotated[
        str, typer.Option("--role", "-r", help="implementer|reviewer|coordinator|observer.")
    ] = "implementer",
    owner: Annotated[str, typer.Option("--owner", help="Human who owns this lane.")] = "",
    claims: Annotated[
        str,
        typer.Option("--claims", help="Comma-separated glob patterns this lane expects to touch."),
    ] = "",
) -> None:
    """Add a lane to a running session."""
    context: CliContext = ctx.obj
    config = context.config()
    harness = harness or config.adapters.default
    name = (
        name or f"{harness}-{int(asyncio.get_event_loop_policy().get_event_loop().time()) % 1000}"
    )

    client = asyncio.run(context.require_daemon())
    data = asyncio.run(
        client.call(
            "lane.start",
            session=session,
            name=name,
            harness=harness,
            role=role,
            owner=owner,
            claims=[c.strip() for c in claims.split(",") if c.strip()],
        )
    )

    def render(lane: dict) -> None:
        success(f"lane '{lane.get('name')}' started ({lane.get('harness')})")
        print_kv(
            {
                "id": lane.get("id"),
                "a2a": lane.get("a2a_endpoint") or "-",
                "card": lane.get("agent_card_url") or "-",
                "worktree": lane.get("worktree_path") or "-",
            }
        )

    emit(context, data, human_renderer=render)


@lane_app.command("list")
def lane_list(
    ctx: typer.Context,
    session: Annotated[str, typer.Argument(help="Session id or name.")],
) -> None:
    """List a session's lanes."""
    context: CliContext = ctx.obj
    client = asyncio.run(context.require_daemon())
    lanes = asyncio.run(client.call("lane.status", session=session))

    def render(data: list[dict]) -> None:
        if not data:
            info("no lanes running")
            return
        for lane in data:
            console.print(lane_line(lane))

    emit(context, lanes, human_renderer=render)


@lane_app.command("stop")
def lane_stop(
    ctx: typer.Context,
    session: Annotated[str, typer.Argument(help="Session id or name.")],
    lane: Annotated[str, typer.Argument(help="Lane id or name.")],
    reason: Annotated[str, typer.Option("--reason", help="Why it is being stopped.")] = "",
) -> None:
    """Stop one lane without closing the session."""
    context: CliContext = ctx.obj
    client = asyncio.run(context.require_daemon())
    data = asyncio.run(client.call("lane.stop", session=session, lane=lane, reason=reason))
    emit(
        context,
        data,
        human_renderer=lambda d: (
            success(f"lane {lane} stopped")
            if d.get("stopped")
            else failure(f"lane {lane} was not running")
        ),
    )


# ---------------------------------------------------------------------------
# burrow ask
# ---------------------------------------------------------------------------
@app.command("ask")
def ask(
    ctx: typer.Context,
    lane: Annotated[str, typer.Argument(help="Lane id or name to ask.")],
    message: Annotated[str, typer.Argument(help="The question or instruction.")],
    session: Annotated[str, typer.Option("--session", "-s", help="Session id or name.")] = "",
    wait: Annotated[
        bool, typer.Option("--wait/--no-wait", help="Block until the lane replies.")
    ] = False,
    timeout: Annotated[
        float, typer.Option("--timeout", help="Seconds to wait with --wait.")
    ] = 300.0,
) -> None:
    """Send a direct message to one lane.

    With ``--wait`` the receiving task moves to ``input_required`` and this
    command polls until it leaves that state — the reply-wait mechanic is the
    A2A lifecycle, not a custom blocking call.
    """
    context: CliContext = ctx.obj
    client = asyncio.run(context.require_daemon())
    session = session or _latest_session(client)

    data = asyncio.run(
        client.call(
            "lane.prompt",
            session=session,
            lane=lane,
            prompt=message,
            by=context.config().settings.governance_human_id,
        )
    )

    if not wait:
        emit(context, data, human_renderer=lambda _d: success(f"delivered to {lane}"))
        return

    info(f"waiting up to {timeout:.0f}s for a reply from {lane}…")
    deadline = asyncio.get_event_loop_policy().get_event_loop().time() + timeout

    async def poll() -> dict | None:
        while True:
            tasks = await client.call("task.list", session=session)
            blocked = [t for t in tasks if t.get("state") in {"input_required", "auth_required"}]
            if not blocked:
                return {"replied": True, "tasks": tasks}
            if asyncio.get_running_loop().time() > deadline:
                return {"replied": False, "waiting_on": blocked}
            await asyncio.sleep(1.0)

    result = asyncio.run(poll())
    emit(
        context,
        result,
        human_renderer=lambda d: (
            success("lane replied") if d.get("replied") else warn("timed out waiting for a reply")
        ),
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _latest_session(client) -> str:
    sessions = asyncio.run(client.call("session.list", open_only=True))
    if not sessions:
        failure("no open sessions")
        info("Start one with `burrow session start`.")
        raise typer.Exit(code=1)
    return str(sessions[0]["id"])


def _render_events(events: list[dict]) -> None:
    if not events:
        info("no bus events yet")
        return
    for event in events:
        _render_event(event)


def _render_event(event: dict) -> None:
    seq = event.get("seq", "?")
    kind = event.get("event_type", "?")
    lane = event.get("lane_id") or ""
    summary = truncate(str(event.get("summary") or ""), 70)
    lane_part = f" [cyan]{short_id(lane, 10)}[/cyan]" if lane else ""
    console.print(f"[dim]{seq:>6}[/dim]  {kind:<26}{lane_part}  {summary}")


__all__ = ["app", "lane_app"]
