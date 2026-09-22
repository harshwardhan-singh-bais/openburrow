"""Integration commands: hooks and notification plumbing.

``burrow hook list`` shows what the daemon actually loaded — not what the YAML
says, which is the version that drifts — and ``burrow hook test`` fires one
synthetic event through every configured channel. Testing a notify setup by
reading it is how a misconfigured webhook URL survives until the first real
incident.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

import typer

from openburrow.cli.context import CliContext
from openburrow.cli.output import emit, info, success, warn

app = typer.Typer(help="Shell hooks and notification plumbing.", no_args_is_help=True)


@app.command("list")
def list_hooks(ctx: typer.Context) -> None:
    """Hooks the daemon loaded from hooks.yaml."""
    context: CliContext = ctx.obj
    client = asyncio.run(context.require_daemon())
    hooks = asyncio.run(client.call("hook.list"))

    def render(data: list) -> None:
        if not data:
            info("no hooks loaded (add entries to .openburrow/hooks.yaml)")
            return
        for hook in data:
            on = ", ".join(hook.get("on") or []) or "all events"
            success(f"{hook.get('name')}: {hook.get('command')}  [dim]on {on}[/dim]")

    emit(context, hooks, human_renderer=render)


@app.command("test")
def test_notify(
    ctx: typer.Context,
    summary: Annotated[
        str, typer.Option("--summary", "-m", help="The test message to send.")
    ] = "OpenBurrow notification test",
) -> None:
    """Fire one synthetic event through every configured notify channel.

    Desktop toasts, Slack/Discord/Teams webhooks, and shell hooks all receive
    it — the point is to verify the wiring while it is cheap to fix, not after
    an incident sat unseen behind a bad webhook URL.
    """
    context: CliContext = ctx.obj
    client = asyncio.run(context.require_daemon())
    result = asyncio.run(client.call("notify.test", summary=summary))

    def render(data: dict) -> None:
        if not data.get("dispatched"):
            warn(f"not dispatched: {data.get('reason', '')}")
            return
        channels = ["desktop"] if data.get("desktop") else []
        channels += [f"webhook:{name}" for name in data.get("webhooks", [])]
        channels += [f"hook:{name}" for name in data.get("hooks", [])]
        if channels:
            for channel in channels:
                success(f"dispatched to {channel}")
        else:
            warn("no channels configured (set notify_desktop, a webhook URL, or hooks.yaml)")

    emit(context, result, human_renderer=render)


__all__ = ["app"]
