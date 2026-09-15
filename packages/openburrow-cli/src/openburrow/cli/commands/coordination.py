"""Coordination commands: claims, handoffs, and the plan.

These are the commands that make two agents working on one repo not collide.
The vocabulary is deliberately small:

``take``    claim an unowned plan step — first claim wins, no negotiation
``claim``   claim a file or path glob, advisory, visible to every lane
``release`` give a claim back
``offer``   request a step someone else already owns — opens a negotiation
``handoff`` deliberate reassignment mid-flight, with a payload

The split between ``take`` and ``offer`` is the important one. An unowned step
is uncontested, so claiming it needs no ceremony. An owned step is contested, so
transferring it goes through ACP performatives rather than being grabbed.
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

app = typer.Typer(help="Claims, handoffs, and plan coordination.", no_args_is_help=True)
plan_app = typer.Typer(help="Inspect and edit the session plan.", no_args_is_help=True)
app.add_typer(plan_app, name="plan")


# ---------------------------------------------------------------------------
# Claims
# ---------------------------------------------------------------------------
@app.command("take")
def take(
    ctx: typer.Context,
    step: Annotated[str, typer.Argument(help="Plan step id or title fragment.")],
    session: Annotated[str, typer.Option("--session", "-s", help="Session id or name.")] = "",
    lane: Annotated[str, typer.Option("--lane", "-l", help="Lane claiming the step.")] = "",
) -> None:
    """Claim an unowned plan step.

    First claim wins and there is no voting. Determinism beats fairness: an
    ambiguous owner is worse than a slightly suboptimal one.
    """
    context: CliContext = ctx.obj
    data = _claim_step(context, session, lane, step, kind="step")
    emit(context, data, human_renderer=lambda d: success(d["message"]))


@app.command("claim")
def claim(
    ctx: typer.Context,
    resource: Annotated[str, typer.Argument(help="File path or glob to claim.")],
    session: Annotated[str, typer.Option("--session", "-s", help="Session id or name.")] = "",
    lane: Annotated[str, typer.Option("--lane", "-l", help="Lane claiming the resource.")] = "",
    intent: Annotated[str, typer.Option("--intent", "-i", help="Why you want it.")] = "",
    ttl: Annotated[int, typer.Option("--ttl", help="Seconds until the claim expires.")] = 1800,
) -> None:
    """Claim a file or path glob.

    Claims are **advisory**: they do not block a write, they make the intent
    visible so another lane can react. Hard locks across heterogeneous harnesses
    would deadlock the moment one crashed, which is the failure this design
    deliberately avoids.
    """
    context: CliContext = ctx.obj
    client = asyncio.run(context.require_daemon())
    session_id = session or _latest_session(client)

    data = asyncio.run(
        client.call(
            "claim.create",
            session=session_id,
            lane=lane,
            resource=resource,
            intent=intent,
            ttl_seconds=ttl,
        )
    )

    def render(result: dict) -> None:
        if result.get("conflict"):
            warn(f"'{resource}' is already claimed by {result.get('conflict_lane')}")
            if result.get("conflict_intent"):
                info(f"their stated intent: {result['conflict_intent']}")
            info("Use `burrow offer` to negotiate a transfer rather than overriding.")
        else:
            success(f"claimed {resource}")
            if result.get("claim_id"):
                info(f"claim {short_id(str(result['claim_id']))}")

    emit(context, data, human_renderer=render)


@app.command("release")
def release(
    ctx: typer.Context,
    resource: Annotated[str, typer.Argument(help="File path or glob to release.")],
    session: Annotated[str, typer.Option("--session", "-s", help="Session id or name.")] = "",
    reason: Annotated[str, typer.Option("--reason", help="Why it is being released.")] = "released",
) -> None:
    """Release a claim so another lane can take it."""
    context: CliContext = ctx.obj
    client = asyncio.run(context.require_daemon())
    session_id = session or _latest_session(client)
    data = asyncio.run(
        client.call("claim.release", session=session_id, resource=resource, reason=reason)
    )
    emit(
        context,
        data,
        human_renderer=lambda d: (
            success(f"released {resource}")
            if d.get("released")
            else failure(f"no active claim on {resource}")
        ),
    )


@app.command("claims")
def list_claims(
    ctx: typer.Context,
    session: Annotated[str, typer.Option("--session", "-s", help="Session id or name.")] = "",
) -> None:
    """List active claims in a session."""
    context: CliContext = ctx.obj
    client = asyncio.run(context.require_daemon())
    session_id = session or _latest_session(client)
    claims = asyncio.run(client.call("claim.list", session=session_id))

    def render(data: list[dict]) -> None:
        table(
            ["resource", "kind", "lane", "owner", "ttl", "intent"],
            [
                [
                    truncate(str(c.get("resource", "")), 40),
                    c.get("kind"),
                    short_id(str(c.get("lane_id", "")), 10),
                    c.get("owner"),
                    f"{int(c.get('ttl_seconds') or 0)}s" if c.get("ttl_seconds") else "—",
                    truncate(str(c.get("intent", "")), 30),
                ]
                for c in data
            ],
        )

    emit(context, claims, human_renderer=render)


@app.command("offer")
def offer(
    ctx: typer.Context,
    step: Annotated[str, typer.Argument(help="Plan step id or title fragment.")],
    teammate: Annotated[str, typer.Argument(help="Lane to offer the step to.")],
    session: Annotated[str, typer.Option("--session", "-s", help="Session id or name.")] = "",
    from_lane: Annotated[str, typer.Option("--from", help="Lane currently owning it.")] = "",
    note: Annotated[str, typer.Option("--note", help="Why the transfer makes sense.")] = "",
) -> None:
    """Request a transfer of an already-owned step.

    This opens a real ACP exchange — ``propose`` to the owning lane, which
    answers ``accept``, ``reject``, or ``counter``. It does not force the
    transfer, because forcing one would make the ownership model meaningless.
    """
    context: CliContext = ctx.obj
    client = asyncio.run(context.require_daemon())
    session_id = session or _latest_session(client)

    data = asyncio.run(
        client.call(
            "handoff.offer",
            session=session_id,
            step=step,
            to_lane=teammate,
            from_lane=from_lane,
            note=note,
        )
    )

    def render(result: dict) -> None:
        if result.get("agreed"):
            success(f"{teammate} accepted the transfer of '{step}'")
        elif result.get("escalated"):
            warn("the exchange did not converge — escalated to a human")
            info("Check `burrow approvals list` or the session feed.")
        else:
            warn(f"{teammate} declined: {result.get('reason', 'no reason given')}")
        if result.get("exchanges"):
            info(f"{result['exchanges']} performative(s) exchanged")

    emit(context, data, human_renderer=render)


@app.command("handoff")
def handoff(
    ctx: typer.Context,
    step: Annotated[str, typer.Argument(help="Plan step id or title fragment.")],
    to_lane: Annotated[str, typer.Argument(help="Lane taking over.")],
    session: Annotated[str, typer.Option("--session", "-s", help="Session id or name.")] = "",
    reason: Annotated[str, typer.Option("--reason", help="Why the handoff is happening.")] = "",
    force: Annotated[
        bool, typer.Option("--force", help="Admin override, skips negotiation.")
    ] = False,
) -> None:
    """Reassign a step deliberately, mid-flight.

    The handoff payload carries branch state, plan status, fresh lesson context,
    and the relevant A2A history — so the receiving lane starts informed rather
    than re-deriving what the previous one already knew.
    """
    context: CliContext = ctx.obj
    client = asyncio.run(context.require_daemon())
    session_id = session or _latest_session(client)

    if force and not context.assume_yes and not context.is_json:
        warn("--force bypasses the negotiation and is recorded in the audit log")
        typer.confirm("Proceed with a forced handoff?", abort=True)

    data = asyncio.run(
        client.call(
            "handoff.execute",
            session=session_id,
            step=step,
            to_lane=to_lane,
            reason=reason,
            force=force,
        )
    )

    def render(result: dict) -> None:
        success(f"step '{step}' handed to {to_lane}")
        print_kv(
            {
                "payload": ", ".join(result.get("payload_keys") or []) or "—",
                "lessons included": result.get("lessons", 0),
                "history messages": result.get("history", 0),
                "forced": result.get("forced", False),
            }
        )

    emit(context, data, human_renderer=render)


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------
@plan_app.command("show")
def plan_show(
    ctx: typer.Context,
    session: Annotated[str, typer.Option("--session", "-s", help="Session id or name.")] = "",
) -> None:
    """Show the session plan as a dependency graph."""
    context: CliContext = ctx.obj
    client = asyncio.run(context.require_daemon())
    session_id = session or _latest_session(client)
    plan = asyncio.run(client.call("plan.get", session=session_id))

    if not plan:
        info("no plan for this session yet")
        return

    def render(data: dict) -> None:
        section(f"{data.get('title') or 'plan'}  (v{data.get('version', 1)})")
        console.print(
            f"  [dim]{len(data.get('steps', []))} steps · "
            f"{data.get('progress', 0) * 100:.0f}% complete[/dim]\n"
        )
        table(
            ["", "step", "status", "owner", "deps", "paths"],
            [
                [
                    "→"
                    if step.get("status") in {"pending"} and not step.get("owner_lane")
                    else " ",
                    truncate(str(step.get("title", "")), 40),
                    state(step.get("status")),
                    short_id(str(step.get("owner_lane") or ""), 10),
                    len(step.get("depends_on") or []),
                    truncate(", ".join(step.get("target_paths") or []) or "—", 30),
                ]
                for step in data.get("steps", [])
            ],
            caption="→ marks a step that is ready to take.",
        )

    emit(context, plan, human_renderer=render)


@plan_app.command("edit")
def plan_edit(
    ctx: typer.Context,
    session: Annotated[str, typer.Option("--session", "-s", help="Session id or name.")] = "",
    add: Annotated[str, typer.Option("--add", help="Add a step with this title.")] = "",
    depends_on: Annotated[str, typer.Option("--depends-on", help="Comma-separated step ids.")] = "",
    remove: Annotated[str, typer.Option("--remove", help="Remove a step by id.")] = "",
) -> None:
    """Edit the plan directly, as a human.

    Human edits bypass the LLM-generated plan entirely and are recorded as such
    — ``source: human`` on the plan — so it is always clear where a step came
    from.
    """
    context: CliContext = ctx.obj
    client = asyncio.run(context.require_daemon())
    session_id = session or _latest_session(client)

    if add:
        data = asyncio.run(
            client.call(
                "plan.add_step",
                session=session_id,
                title=add,
                depends_on=[d.strip() for d in depends_on.split(",") if d.strip()],
            )
        )
        emit(
            context,
            data,
            human_renderer=lambda d: success(f"added step {short_id(str(d.get('id', '')))}"),
        )
        return

    if remove:
        data = asyncio.run(client.call("plan.remove_step", session=session_id, step=remove))
        emit(
            context,
            data,
            human_renderer=lambda d: (
                success("step removed") if d.get("removed") else failure("step not found")
            ),
        )
        return

    warn("nothing to do — pass --add or --remove")
    raise typer.Exit(code=1)


@plan_app.command("diff")
def plan_diff(
    ctx: typer.Context,
    session: Annotated[str, typer.Option("--session", "-s", help="Session id or name.")] = "",
    version: Annotated[
        int, typer.Option("--version", help="Compare against this plan version.")
    ] = 1,
) -> None:
    """Show what changed in the plan since a prior version."""
    context: CliContext = ctx.obj
    client = asyncio.run(context.require_daemon())
    session_id = session or _latest_session(client)
    data = asyncio.run(client.call("plan.diff", session=session_id, version=version))

    def render(result: dict) -> None:
        section(f"plan diff vs v{version}")
        for key, style in (("added", "green"), ("removed", "red"), ("changed", "yellow")):
            items = result.get(key) or []
            console.print(f"  [{style}]{key}:[/{style}] {len(items)}")
            for item in items[:10]:
                console.print(f"    [dim]{item}[/dim]")

    emit(context, data, human_renderer=render)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _claim_step(context: CliContext, session: str, lane: str, step: str, *, kind: str) -> dict:
    client = asyncio.run(context.require_daemon())
    session_id = session or _latest_session(client)
    data = asyncio.run(
        client.call("plan.take", session=session_id, lane=lane, step=step, kind=kind)
    )
    if isinstance(data, dict) and "message" not in data:
        data = {**data, "message": f"claimed '{step}'"}
    return data


def _latest_session(client) -> str:
    sessions = asyncio.run(client.call("session.list", open_only=True))
    if not sessions:
        failure("no open sessions")
        raise typer.Exit(code=1)
    return str(sessions[0]["id"])


__all__ = ["app", "plan_app"]
