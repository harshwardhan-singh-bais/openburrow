"""Knowledge commands: the Shared Brain and lesson propagation.

Two different kinds of knowledge, deliberately kept apart:

* **Brain** — durable facts about the codebase, anchored to a file and a commit.
  Its value comes from staying true, so a superseded anchor marks the entry
  stale rather than leaving it to mislead someone.
* **Lessons** — operational knowledge about doing the work, scoped shorter and
  expiring by default. Its value comes from spreading fast.

Both carry a provenance chain back to the A2A message that produced them, which
is not decoration: a poisoned lesson is a named attack surface, and you cannot
defend against an attack you cannot trace.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
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
)
from openburrow.daemon.ipc import IpcClient

app = typer.Typer(help="Shared Brain and lesson propagation.", no_args_is_help=True)
brain_app = typer.Typer(
    help="Durable, anchored knowledge about this codebase.", no_args_is_help=True
)
lessons_app = typer.Typer(
    help="Operational lessons propagated between lanes.", no_args_is_help=True
)
app.add_typer(brain_app, name="brain")
app.add_typer(lessons_app, name="lessons")


# ---------------------------------------------------------------------------
# burrow brain
# ---------------------------------------------------------------------------
@brain_app.command("list")
def brain_list(
    ctx: typer.Context,
    path: Annotated[
        str, typer.Option("--path", help="Filter to entries anchored to this file.")
    ] = "",
    stale: Annotated[
        bool, typer.Option("--stale", help="Include entries whose anchor is superseded.")
    ] = False,
) -> None:
    """List Brain entries."""
    context: CliContext = ctx.obj
    client = asyncio.run(context.require_daemon())
    entries = asyncio.run(client.call("brain.list", path=path, active_only=not stale))

    def render(data: list[dict]) -> None:
        if not data:
            info("the Brain is empty — entries are promoted from tagged A2A messages")
            return
        table(
            ["id", "type", "title", "anchor", "status", "injected", "source lane"],
            [
                [
                    short_id(str(e.get("id", "")), 10),
                    e.get("entry_type"),
                    truncate(str(e.get("title", "")), 40),
                    truncate(str(e.get("anchor_path") or "repo-wide"), 24),
                    state(e.get("status")),
                    e.get("injection_count", 0),
                    short_id(str(e.get("source_lane") or ""), 10),
                ]
                for e in data
            ],
        )

    emit(context, entries, human_renderer=render)


@brain_app.command("add")
def brain_add(
    ctx: typer.Context,
    title: Annotated[str, typer.Argument(help="One-line summary.")],
    body: Annotated[str, typer.Option("--body", "-b", help="The detail.")] = "",
    entry_type: Annotated[
        str, typer.Option("--type", "-t", help="decision|gotcha|convention")
    ] = "convention",
    anchor: Annotated[
        str, typer.Option("--anchor", "-a", help="Repo-relative file this is about.")
    ] = "",
    commit: Annotated[str, typer.Option("--commit", help="Commit this is true at.")] = "",
) -> None:
    """Add a Brain entry by hand."""
    context: CliContext = ctx.obj
    client = asyncio.run(context.require_daemon())
    data = asyncio.run(
        client.call(
            "brain.add",
            title=title,
            body=body,
            entry_type=entry_type,
            anchor_path=anchor,
            anchor_commit=commit,
            promoted_by="human",
            confirmed_by=context.config().settings.governance_human_id,
        )
    )
    emit(
        context,
        data,
        human_renderer=lambda d: success(f"added Brain entry {short_id(str(d.get('id', '')))}"),
    )


@brain_app.command("retire")
def brain_retire(
    ctx: typer.Context,
    entry: Annotated[str, typer.Argument(help="Entry id.")],
    reason: Annotated[str, typer.Option("--reason", help="Why it is being retired.")] = "",
) -> None:
    """Retire an entry so it stops being injected into new lanes."""
    context: CliContext = ctx.obj
    client = asyncio.run(context.require_daemon())
    data = asyncio.run(client.call("brain.retire", entry=entry, reason=reason))
    emit(context, data, human_renderer=lambda _d: success("entry retired"))


@brain_app.command("export")
def brain_export(
    ctx: typer.Context,
    output: Annotated[
        Path | None, typer.Argument(help="Where to write. Defaults to AGENTS.md.")
    ] = None,
) -> None:
    """Write Brain entries back into ``AGENTS.md``-compatible format.

    This is the interop bridge: a repository that never installs OpenBurrow
    still benefits from what the participating lanes learned, because the
    knowledge lands in a file every other tool already reads.
    """
    context: CliContext = ctx.obj
    config = context.config()
    client = asyncio.run(context.require_daemon())
    entries = asyncio.run(client.call("brain.list", active_only=True))

    target = output or config.paths.agents_md
    lines = ["<!-- Generated by OpenBurrow. Edit freely; entries are merged, not replaced. -->", ""]
    for entry in entries:
        scope = f" (`{entry['anchor_path']}`)" if entry.get("anchor_path") else ""
        marker = str(entry.get("entry_type", "convention")).upper()
        lines.append(
            f"- **{marker}**{scope}: {entry.get('title')} — {entry.get('body', '')}".rstrip()
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")

    emit(
        context,
        {"path": str(target), "entries": len(entries)},
        human_renderer=lambda d: success(f"exported {d['entries']} entries to {d['path']}"),
    )


@brain_app.command("refresh-stale")
def brain_refresh_stale(
    ctx: typer.Context,
    session: Annotated[str, typer.Option("--session", "-s", help="Session id or name.")] = "",
) -> None:
    """Sweep the Brain for drift and ask a nearby lane to re-check stale entries.

    Staleness is git-derived: an entry anchored to a file the commits have moved
    is a claim whose evidence expired. The nearest idle or working lane is asked
    — as a question, not an instruction — to confirm or rewrite it.
    """
    context: CliContext = ctx.obj
    client = asyncio.run(context.require_daemon())
    session_id = session or _latest_session(client)
    data = asyncio.run(client.call("brain.refresh_stale", session=session_id))

    def render(result: dict) -> None:
        if not result.get("stale"):
            success("no stale entries — every anchor still holds")
            return
        section(f"{result['stale']} stale entr{'y' if result['stale'] == 1 else 'ies'}")
        for prompt in result.get("prompts") or []:
            delivered = "asked" if prompt.get("delivered") else "not delivered"
            console.print(
                f"  {truncate(str(prompt.get('title', '')), 46)} "
                f"[dim]({prompt.get('anchor_path', '')}) — {delivered}"
                + (f" to {prompt.get('target_lane', '')}" if prompt.get("target_lane") else "")
                + "[/dim]"
            )

    emit(context, data, human_renderer=render)


@brain_app.command("diff")
def brain_diff(
    ctx: typer.Context,
    session: Annotated[str, typer.Option("--session", "-s", help="Session id or name.")] = "",
) -> None:
    """Show what changed in shared knowledge over a session."""
    context: CliContext = ctx.obj
    client = asyncio.run(context.require_daemon())
    session_id = session or _latest_session(client)
    data = asyncio.run(client.call("brain.diff", session=session_id))

    def render(result: dict) -> None:
        section("Brain changes this session")
        print_kv(
            {
                "added": len(result.get("added") or []),
                "stale": len(result.get("stale") or []),
                "retired": len(result.get("retired") or []),
            }
        )
        for key in ("added", "stale", "retired"):
            for item in (result.get(key) or [])[:10]:
                console.print(f"  [dim]{key}:[/dim] {truncate(str(item), 70)}")

    emit(context, data, human_renderer=render)


# ---------------------------------------------------------------------------
# burrow lessons
# ---------------------------------------------------------------------------
@lessons_app.command("list")
def lessons_list(
    ctx: typer.Context,
    session: Annotated[str, typer.Option("--session", "-s", help="Session id or name.")] = "",
    scope: Annotated[str, typer.Option("--scope", help="session|repo|org.")] = "",
) -> None:
    """List live lessons, attributed to the lane that discovered them."""
    context: CliContext = ctx.obj
    client = asyncio.run(context.require_daemon())
    session_id = session or _latest_session(client)
    lessons = asyncio.run(client.call("lessons.list", session=session_id, scope=scope))

    def render(data: list[dict]) -> None:
        if not data:
            info("no lessons promoted yet")
            return
        table(
            ["id", "scope", "lesson", "source", "injected", "hit rate", "expires"],
            [
                [
                    short_id(str(lesson.get("id", "")), 10),
                    lesson.get("scope"),
                    truncate(str(lesson.get("title", "")), 42),
                    short_id(str(lesson.get("source_lane") or ""), 10),
                    lesson.get("injection_count", 0),
                    f"{(lesson.get('hit_rate') or 0) * 100:.0f}%",
                    (lesson.get("expires_at") or "never")[:10],
                ]
                for lesson in data
            ],
            caption="Hit rate is the share of injections that appear to have helped.",
        )

    emit(context, lessons, human_renderer=render)


@lessons_app.command("retire")
def lessons_retire(
    ctx: typer.Context,
    lesson: Annotated[str, typer.Argument(help="Lesson id.")],
    reason: Annotated[str, typer.Option("--reason", help="Why it is being retired.")] = "",
) -> None:
    """Retire a lesson so it stops being injected."""
    context: CliContext = ctx.obj
    client = asyncio.run(context.require_daemon())
    data = asyncio.run(client.call("lessons.retire", lesson=lesson, reason=reason))
    emit(context, data, human_renderer=lambda _d: success("lesson retired"))


@lessons_app.command("promote")
def lessons_promote(
    ctx: typer.Context,
    title: Annotated[str, typer.Argument(help="One-line summary of the lesson.")],
    body: Annotated[str, typer.Option("--body", "-b", help="The detail: what happened.")] = "",
    trigger: Annotated[
        str, typer.Option("--trigger", help="When to apply it: the situation to recognise.")
    ] = "",
    remedy: Annotated[
        str, typer.Option("--remedy", help="What to do when the trigger fires.")
    ] = "",
    scope: Annotated[str, typer.Option("--scope", help="session|repo|org.")] = "session",
    ttl_days: Annotated[int | None, typer.Option("--ttl-days", help="Days until expiry.")] = None,
    session: Annotated[str, typer.Option("--session", "-s", help="Session id or name.")] = "",
) -> None:
    """Promote text to a lesson so other lanes inherit it.

    Goes through the poisoned-lesson detector before it is stored: a lesson is a
    prompt fragment injected into every lane's context, so this command is the
    one place a human can (accidentally) inject into every harness at once.
    A flagged candidate is refused and reported, not stored.
    """
    context: CliContext = ctx.obj
    client = asyncio.run(context.require_daemon())
    session_id = session or _latest_session(client)
    data = asyncio.run(
        client.call(
            "lessons.promote",
            session=session_id,
            title=title,
            body=body,
            trigger=trigger,
            remedy=remedy,
            scope=scope,
            ttl_days=ttl_days,
            promoted_by="human",
        )
    )

    def render(result: dict) -> None:
        success(f"lesson promoted ({result.get('scope')}) — {result.get('title')}")
        print_kv({"id": result.get("id")})

    emit(context, data, human_renderer=render)


def _latest_session(client: IpcClient) -> str:
    sessions = asyncio.run(client.call("session.list", open_only=True))
    if not sessions:
        failure("no open sessions")
        raise typer.Exit(code=1)
    return str(sessions[0]["id"])


__all__ = ["app", "brain_app", "lessons_app"]
