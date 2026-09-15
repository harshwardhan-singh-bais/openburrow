"""Terminal rendering.

All presentation lives here so that commands can focus on behaviour. Every
renderer takes plain data and respects the context's output mode, which is what
makes ``--json`` work uniformly across the whole CLI instead of being
implemented per command.

Colour is used semantically, not decoratively:

* **cyan** — identifiers and lane names
* **green** — success, agreement, completed states
* **yellow** — things needing attention (approvals, negotiations, stale data)
* **red** — violations, failures, blocked states
* **dim** — metadata that is useful but not the point

That mapping is consistent enough that a user learns to read the output at a
glance, which is the entire justification for colour in a CLI.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from openburrow.cli.context import OutputMode, human_size, short_id, truncate

console = Console(highlight=False, soft_wrap=False)
error_console = Console(stderr=True, highlight=False)


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------
def emit(context: Any, payload: Any, *, human_renderer: Any = None) -> None:
    """Render ``payload`` according to the context's output mode.

    The single funnel every command uses. When ``--json`` is set the payload is
    printed as JSON and the human renderer is never called, which guarantees the
    two modes cannot drift apart.
    """
    mode = getattr(context, "output_mode", OutputMode.HUMAN)

    if mode == OutputMode.QUIET:
        if isinstance(payload, dict):
            for key in ("id", "value", "result"):
                if key in payload:
                    console.print(str(payload[key]))
                    return
        return

    if mode == OutputMode.JSON:
        console.print_json(json.dumps(_jsonable(payload), default=str))
        return

    if human_renderer is not None:
        human_renderer(payload)
    elif isinstance(payload, str):
        console.print(payload)
    elif isinstance(payload, dict):
        print_kv(payload)
    elif isinstance(payload, Iterable):
        for item in payload:
            console.print(item)
    else:
        console.print(str(payload))


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def print_kv(data: dict[str, Any], *, indent: int = 0) -> None:
    """Print a mapping as aligned key/value pairs."""
    if not data:
        console.print("  [dim](nothing)[/dim]")
        return
    width = max(len(str(k)) for k in data)
    pad = " " * indent
    for key, value in data.items():
        console.print(f"{pad}[dim]{key:<{width}}[/dim]  {_format_value(value)}")


def _format_value(value: Any) -> str:
    if value is None:
        return "[dim]—[/dim]"
    if isinstance(value, bool):
        return "[green]yes[/green]" if value else "[dim]no[/dim]"
    if isinstance(value, (list, tuple)):
        if not value:
            return "[dim]none[/dim]"
        return ", ".join(str(v) for v in value[:8]) + (" …" if len(value) > 8 else "")
    if isinstance(value, dict):
        if not value:
            return "[dim]none[/dim]"
        return ", ".join(f"{k}={v}" for k, v in list(value.items())[:4])
    return str(value)


def table(
    columns: Sequence[str],
    rows: Iterable[Sequence[Any]],
    *,
    title: str = "",
    caption: str = "",
) -> None:
    """Print a table. Empty row sets still render headers, so the shape is visible."""
    tbl = Table(
        title=title or None, caption=caption or None, header_style="bold", box=None, pad_edge=False
    )
    for column in columns:
        tbl.add_column(column, overflow="fold")
    count = 0
    for row in rows:
        tbl.add_row(*[str(cell) for cell in row])
        count += 1
    if count == 0:
        tbl.add_row(*["[dim]—[/dim]"] * len(columns))
    console.print(tbl)


# ---------------------------------------------------------------------------
# Semantic state rendering
# ---------------------------------------------------------------------------
#: Lane and task states mapped to a colour. One source of truth so a state
#: renders the same in `watch`, `session show`, and `report`.
STATE_STYLES: dict[str, str] = {
    "submitted": "dim",
    "working": "cyan",
    "input_required": "yellow",
    "auth_required": "yellow",
    "negotiating": "magenta",
    "blocked": "yellow",
    "idle": "dim",
    "watching": "dim",
    "starting": "dim",
    "completed": "green",
    "done": "green",
    "active": "green",
    "created": "dim",
    "paused": "yellow",
    "failed": "red",
    "crashed": "red",
    "rejected": "red",
    "canceled": "dim",
    "stopped": "dim",
    "expired": "dim",
    "pending": "yellow",
    "claimed": "cyan",
    "in_progress": "cyan",
    "review": "magenta",
    "escalated": "red",
    "agreed": "green",
    "stale": "yellow",
    "retired": "dim",
}


def state(value: Any) -> str:
    """Render a state value with its semantic colour."""
    text = str(value)
    style = STATE_STYLES.get(text, "white")
    return f"[{style}]{text}[/{style}]"


def severity_style(severity: str) -> str:
    return {
        "info": "dim",
        "notice": "cyan",
        "warning": "yellow",
        "violation": "red",
        "critical": "bold red",
    }.get(str(severity), "white")


def lane_line(lane: dict[str, Any]) -> str:
    """One-line lane summary used by the live board and session views."""
    name = lane.get("name") or short_id(str(lane.get("id", "")))
    harness = lane.get("harness", "?")
    status = state(lane.get("status", "?"))
    a2a = lane.get("a2a_endpoint") or ""
    endpoint = f" [dim]{a2a}[/dim]" if a2a else ""
    msgs = lane.get("messages_sent", 0)
    recv = lane.get("messages_received", 0)
    return f"[cyan]{name:<14}[/cyan] {harness:<12} {status:<18} msgs {msgs}/{recv}{endpoint}"


# ---------------------------------------------------------------------------
# Composite views
# ---------------------------------------------------------------------------
def banner(text: str, *, subtitle: str = "", style: str = "cyan") -> None:
    body = Text(text, style=f"bold {style}")
    if subtitle:
        body.append(f"\n{subtitle}", style="dim")
    console.print(Panel(body, border_style=style, padding=(0, 2)))


def success(message: str) -> None:
    console.print(f"[green]✓[/green] {message}")


def warn(message: str) -> None:
    console.print(f"[yellow]![/yellow] {message}")


def failure(message: str) -> None:
    error_console.print(f"[red]✗[/red] {message}")


def info(message: str) -> None:
    console.print(f"[dim]·[/dim] {message}")


def section(title: str) -> None:
    console.print(f"\n[bold]{title}[/bold]")


def render_error(error: Exception) -> None:
    """Render an OpenBurrow error with its hint and context.

    The hint is printed on its own line and the context as a dim block, because
    "what do I do about it" and "what were the values" are two different
    questions and mixing them into one sentence helps neither.
    """
    message = getattr(error, "message", str(error))
    hint = getattr(error, "hint", None)
    context = getattr(error, "context", None)
    code = getattr(error, "code", None)

    failure(message)
    if code:
        error_console.print(f"  [dim]code: {code}[/dim]")
    if hint:
        error_console.print(f"  [cyan]hint:[/cyan] {hint}")
    if context:
        error_console.print("  [dim]context:[/dim]")
        for key, value in context.items():
            error_console.print(f"    [dim]{key}:[/dim] {truncate(str(value), 120)}")


def session_header(session: dict[str, Any]) -> None:
    """Compact header block used at the top of most session-scoped output."""
    console.print(
        f"[bold]{session.get('name', 'session')}[/bold] "
        f"[dim]({short_id(str(session.get('id', '')))})[/dim]  "
        f"{state(session.get('status', '?'))}"
    )
    meta = [
        f"branch [cyan]{session.get('branch', '-')}[/cyan]",
        f"{session.get('lane_count', len(session.get('lanes', [])))} lane(s)",
        f"{session.get('duration_seconds', 0):.0f}s",
    ]
    if session.get("total_cost_usd"):
        meta.append(f"${session['total_cost_usd']:.4f}")
    console.print("  [dim]" + "  ·  ".join(meta) + "[/dim]")


def negotiation_view(exchange: dict[str, Any]) -> None:
    """Render a negotiation transcript with performative colouring."""
    performative_styles = {
        "propose": "cyan",
        "counter": "yellow",
        "accept": "green",
        "reject": "red",
        "inform": "dim",
        "withdraw": "dim",
    }
    topic = exchange.get("topic") or exchange.get("description") or "negotiation"
    outcome = str(exchange.get("outcome", "pending"))
    console.print(
        f"\n[bold]{topic}[/bold]  {state(outcome)}  "
        f"[dim]{exchange.get('lane_a', '?')} ↔ {exchange.get('lane_b', '?')}[/dim]"
    )
    for move in exchange.get("moves", []):
        performative = str(move.get("performative", "inform"))
        style = performative_styles.get(performative, "white")
        console.print(
            f"  [{style}]{performative:<9}[/{style}] "
            f"[cyan]{move.get('lane_id', '?')}[/cyan]  {move.get('summary', '')}"
        )
        if move.get("requested_change"):
            console.print(f"            [dim]→ wants: {move['requested_change']}[/dim]")
        if move.get("refs"):
            console.print(f"            [dim]refs: {', '.join(move['refs'][:4])}[/dim]")


def budget_bar(used: float, limit: float, *, width: int = 20) -> str:
    """ASCII progress bar for budget consumption."""
    if limit <= 0:
        return "[dim]" + "·" * width + "[/dim]"
    ratio = max(0.0, min(1.0, used / limit))
    filled = int(ratio * width)
    style = "green" if ratio < 0.7 else "yellow" if ratio < 0.9 else "red"
    return (
        f"[{style}]{'█' * filled}[/{style}][dim]{'░' * (width - filled)}[/dim] {ratio * 100:.0f}%"
    )


def size_fmt(num_bytes: int) -> str:
    return human_size(num_bytes)


__all__ = [
    "STATE_STYLES",
    "banner",
    "budget_bar",
    "console",
    "emit",
    "error_console",
    "failure",
    "info",
    "lane_line",
    "negotiation_view",
    "print_kv",
    "render_error",
    "section",
    "session_header",
    "severity_style",
    "size_fmt",
    "state",
    "success",
    "table",
    "warn",
]
