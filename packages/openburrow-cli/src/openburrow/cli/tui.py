"""The full-screen dashboard.

This is a convenience view over exactly the same IPC calls ``burrow watch`` makes,
and it is deliberately the *smaller* of the two implementations. Anything you can
see here you can also get as text, because the interesting failure mode of a
multi-agent system is not "I wish the UI were prettier" — it is "a lane stopped
producing output four minutes ago and I need to know whether it is thinking or
wedged". ``burrow watch`` answers that on a serial console and in a CI log; this
answers it with more colour and less scrolling.

Design notes worth keeping:

* **The feed reads the bus log, not the sockets.** ``bus.tail`` pages through the
  append-only log by sequence number. If the TUI disconnects for ten seconds it
  resumes from the last sequence it saw and misses nothing, because the log *is*
  the record — there is no second source of truth that could disagree with it.
* **Polling, not streaming, on purpose.** ``bus.stream`` exists and would push
  events, but a dashboard that redraws on every event is unreadable when six
  lanes are talking. A fixed cadence makes the screen legible and costs one
  round-trip a second on a local socket.
* **A failure in the poll loop is content, not a crash.** If the daemon dies
  mid-session the dashboard must say so in the feed rather than vanishing and
  leaving the user staring at their shell wondering what happened.
"""

from __future__ import annotations

import asyncio
from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Footer, Header, RichLog, Static

from openburrow.cli.context import short_id, truncate

#: How long to wait between refreshes. One second is fast enough that a lane
#: going quiet is visible immediately, and slow enough to read.
POLL_INTERVAL_S = 1.0

#: Bus events per page. The feed keeps the last few pages; older events are in
#: the log and reachable with ``burrow logs``.
TAIL_PAGE = 200

#: Lane status → display style. Mirrors ``output.STATE_STYLES`` so the TUI and
#: the CLI agree about what "blocked" looks like.
LANE_STYLES: dict[str, str] = {
    "running": "bold green",
    "thinking": "bold cyan",
    "waiting": "yellow",
    "blocked": "bold yellow",
    "idle": "dim",
    "stale": "bold red",
    "stopped": "dim",
    "crashed": "bold red",
    "failed": "bold red",
    "completed": "green",
    "starting": "cyan",
}


def _lane_style(status: str) -> str:
    return LANE_STYLES.get(status, "")


#: The lane table's columns: ``(key, heading, width)``.
#:
#: Declared once because two places have to agree about them — ``add_column`` at
#: mount, and the per-cell update on every refresh. Nothing else checks that the
#: two agree, and a mismatch does not raise: it renders the wrong value under the
#: wrong heading, which reads as a data problem rather than a code one.
_LANE_COLUMNS: tuple[tuple[str, str, int | None], ...] = (
    ("lane", "lane", 14),
    ("harness", "harness", 12),
    ("role", "role", 12),
    ("state", "state", 10),
    ("msgs", "msgs", 6),
    ("task", "task", None),
)


class BurrowTui(App[None]):
    """A live board of one session: its lanes, its bus, and its task load."""

    TITLE = "OpenBurrow"

    CSS = """
    Screen {
        layout: vertical;
    }

    #body {
        height: 1fr;
    }

    #lanes-pane {
        width: 3fr;
        border: round $primary;
        border-title-color: $primary;
    }

    #feed-pane {
        width: 5fr;
        border: round $secondary;
        border-title-color: $secondary;
    }

    #lanes {
        height: 1fr;
    }

    #feed {
        height: 1fr;
    }

    #status {
        height: 1;
        padding: 0 1;
        background: $surface;
        color: $text;
    }

    #status.-error {
        background: $error;
        color: $text;
    }
    """

    # Textual's metaclass reads this off the class and subclasses override it, so
    # it is a class attribute by the framework's contract rather than by accident.
    BINDINGS = [  # noqa: RUF012
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh_now", "Refresh"),
        Binding("p", "toggle_pause", "Pause"),
        Binding("c", "clear_feed", "Clear feed"),
        Binding("f", "toggle_follow", "Follow"),
    ]

    def __init__(self, client: Any, session_id: str) -> None:
        super().__init__()
        self._client = client
        self._session_id = session_id
        self._since_seq = 0
        self._paused = False
        self._force_refresh = False
        self._closing = False
        self._failures = 0
        self._last_summary: dict[str, Any] = {}

    # --- layout -----------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="body"):
            with Vertical(id="lanes-pane") as lanes_pane:
                lanes_pane.border_title = "lanes"
                yield DataTable(id="lanes", zebra_stripes=True, cursor_type="row")
            with Vertical(id="feed-pane") as feed_pane:
                feed_pane.border_title = "bus"
                yield RichLog(id="feed", highlight=True, markup=True, wrap=True)
        yield Static("connecting…", id="status")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#lanes", DataTable)
        for key, heading, width in _LANE_COLUMNS:
            table.add_column(heading, key=key, width=width)
        self.run_worker(self._poll_loop(), name="poll", exclusive=True)

    # --- actions ----------------------------------------------------------
    def action_refresh_now(self) -> None:
        self._force_refresh = True

    def action_toggle_pause(self) -> None:
        self._paused = not self._paused
        self._set_status("paused" if self._paused else "live")

    def action_clear_feed(self) -> None:
        self.query_one("#feed", RichLog).clear()

    def action_toggle_follow(self) -> None:
        """Jump the feed to the newest events.

        Useful after scrolling back: the feed does not yank itself to the bottom
        while you are reading, because a log that moves under your cursor is
        worse than a log that is briefly behind.
        """
        self._since_seq = 0
        self._force_refresh = True

    # --- polling ----------------------------------------------------------
    async def _poll_loop(self) -> None:
        while not self._closing:
            if self._force_refresh or not self._paused:
                self._force_refresh = False
                try:
                    await self._refresh()
                    self._failures = 0
                except Exception as exc:
                    self._failures += 1
                    self._report_failure(exc)
            await asyncio.sleep(POLL_INTERVAL_S)

    async def _refresh(self) -> None:
        session = await self._client.call("session.show", session=self._session_id)
        self._last_summary = session.get("session", {})
        lanes = session.get("running_lanes", []) or []
        await self._render_lanes(lanes)

        events = await self._client.call(
            "bus.tail",
            session=self._session_id,
            since_seq=self._since_seq,
            limit=TAIL_PAGE,
        )
        self._render_events(events or [])
        self._set_status(self._status_line(lanes))

    async def _render_lanes(self, lanes: list[dict[str, Any]]) -> None:
        table = self.query_one("#lanes", DataTable)
        # A lane's key is its id, so rows update in place rather than being
        # rebuilt — which keeps the cursor on the lane you were looking at.
        seen: set[str] = set()
        existing = {getattr(key, "value", key) for key in table.rows}
        for lane in lanes:
            lane_id = str(lane.get("id", ""))
            seen.add(lane_id)
            status = str(lane.get("status", "?"))
            cells = (
                short_id(str(lane.get("name") or lane_id), 12),
                str(lane.get("harness", "-")),
                str(lane.get("role", "-")),
                f"[{_lane_style(status)}]{status}[/]",
                str(lane.get("messages_sent", 0) + lane.get("messages_received", 0)),
                truncate(str(lane.get("current_task") or "-"), 40),
            )
            if lane_id in existing:
                # Per cell, because `DataTable.update_row` does not exist —
                # textual offers `update_cell` and nothing row-shaped. The
                # previous code called `update_row` inside a
                # `try/except RowDoesNotExist`, which could not catch the
                # `AttributeError` it actually raised, so the lane table failed
                # on its first refresh and every refresh after it.
                #
                # `strict=True` is the guard the old code lacked: adding a column
                # without adding a cell now raises here rather than silently
                # rendering a row one column short.
                for (key, _heading, _width), value in zip(_LANE_COLUMNS, cells, strict=True):
                    table.update_cell(lane_id, key, value)
            else:
                table.add_row(*cells, key=lane_id)
                existing.add(lane_id)

        # A lane that has stopped no longer appears in `running_lanes`, so its row
        # would otherwise linger forever showing a state that is no longer true.
        #
        # `row_key`, not `key`: an earlier loop in this method already binds `key`
        # to a column name, and reusing the name would rebind it from `str` to
        # `RowKey` in the same scope.
        for row_key in list(table.rows):
            if getattr(row_key, "value", row_key) not in seen:
                table.remove_row(row_key)

    def _render_events(self, events: list[dict[str, Any]]) -> None:
        if not events:
            return
        feed = self.query_one("#feed", RichLog)
        for event in events:
            seq = int(event.get("seq") or 0)
            self._since_seq = max(self._since_seq, seq)
            kind = str(event.get("event_type") or event.get("type") or "event")
            lane = event.get("lane_id")
            lane_part = f" [cyan]{short_id(str(lane), 10)}[/cyan]" if lane else ""
            summary = truncate(str(event.get("summary") or ""), 90)
            feed.write(f"[dim]{seq:>6}[/dim]  {kind:<24}{lane_part}  {summary}")

    # --- status -----------------------------------------------------------
    def _status_line(self, lanes: list[dict[str, Any]]) -> str:
        name = self._last_summary.get("name") or self._session_id
        status = self._last_summary.get("status", "?")
        blocked = sum(
            1 for lane in lanes if str(lane.get("status")) in {"blocked", "stale", "crashed"}
        )
        head = f"{name}  ·  {status}  ·  {len(lanes)} lane(s)"
        if blocked:
            head += f"  ·  [bold red]{blocked} need attention[/]"
        return head

    def _set_status(self, text: str) -> None:
        widget = self.query_one("#status", Static)
        widget.update(text)
        widget.set_class(text.startswith("daemon unreachable"), "-error")

    def _report_failure(self, exc: Exception) -> None:
        """Surface a failed poll without killing the dashboard."""
        message = str(exc)
        self._set_status(f"daemon unreachable ({self._failures}x): {truncate(message, 80)}")
        if self._failures in {1, 10}:
            self.query_one("#feed", RichLog).write(
                f"[bold red]poll failed[/bold red] [dim]({self._failures})[/dim] {truncate(message, 120)}"
            )

    # --- teardown ---------------------------------------------------------
    def on_unmount(self) -> None:
        self._closing = True


__all__ = ["BurrowTui"]
