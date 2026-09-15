"""asciinema v2 cast files, written and read correctly.

A cast is a JSON header line followed by newline-delimited event arrays::

    {"version": 2, "width": 120, "height": 30, "timestamp": 1757...}
    [0.248, "o", "\\u001b[32m$\u001b[0m "]
    [0.512, "o", "running tests\\r\\n"]

We write the real format rather than a bespoke one so a reel plays in
``asciinema play``, in ``agg``, and in the web player people already have
installed. A replay format that needs its own viewer is a replay format nobody
watches.

The format gives us one thing that matters more than convenience: ``"o"`` events
are timestamped from the start of the recording, so a cast is a *timeline*. That
lets the reel viewer scrub to the moment a lane stalled, and it lets the causal
event log — which shares the same clock — be overlaid on the same axis. Without a
shared timebase the two would be two separate stories about the same session.

Custom event types are namespaced ``x-``. The v2 spec reserves single letters for
itself and does not forbid longer names, so a namespace keeps us from colliding
with a future addition to the spec. Our own events carry bus activity that has no
terminal representation — a claim, a negotiation move, a governance flag — which
is exactly the context a cast alone cannot show.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openburrow.core.logging import get_logger

log = get_logger(__name__)

#: Standard v2 event codes.
OUTPUT = "o"
INPUT = "i"
MARKER = "m"
RESIZE = "r"

#: Prefix for OpenBurrow's own event types.
CUSTOM_PREFIX = "x-"

#: Event types we emit. Kept as a closed set so the viewer can switch on them.
EVENT_CLAIM = f"{CUSTOM_PREFIX}claim"
EVENT_NEGOTIATION = f"{CUSTOM_PREFIX}negotiation"
EVENT_GOVERNANCE = f"{CUSTOM_PREFIX}governance"
EVENT_TASK = f"{CUSTOM_PREFIX}task"
EVENT_LANE = f"{CUSTOM_PREFIX}lane"

CUSTOM_EVENT_TYPES = frozenset(
    {EVENT_CLAIM, EVENT_NEGOTIATION, EVENT_GOVERNANCE, EVENT_TASK, EVENT_LANE}
)

#: A terminal is not a log file. Lines longer than this are truncated with a
#: visible marker so a replay shows that something was cut rather than silently
#: dropping it — an unmarked truncation makes a reel lie by omission.
MAX_EVENT_CHARS = 64_000
TRUNCATION_MARKER = "…[truncated by openburrow]\r\n"


@dataclass(slots=True)
class CastEvent:
    """One entry in a cast."""

    time: float
    kind: str
    data: str

    def to_json(self) -> str:
        return json.dumps([round(self.time, 6), self.kind, self.data], ensure_ascii=False)


@dataclass(slots=True)
class CastHeader:
    """The cast's first line."""

    version: int = 2
    width: int = 120
    height: int = 30
    timestamp: int = field(default_factory=lambda: int(time.time()))
    idle_time: float = 2.0
    title: str = ""
    env: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> str:
        payload: dict[str, Any] = {
            "version": self.version,
            "width": self.width,
            "height": self.height,
            "timestamp": self.timestamp,
            "idle_time_limit": self.idle_time,
        }
        if self.title:
            payload["title"] = self.title
        if self.env:
            payload["env"] = self.env
        return json.dumps(payload, ensure_ascii=False)


class CastWriter:
    """Writes a cast incrementally.

    Events are buffered and flushed in batches rather than written one at a time.
    A lane producing output at 200 lines/second would otherwise mean 200 tiny
    writes per second to disk, which is a real cost on Windows and shows up as
    dropped output under load.
    """

    def __init__(
        self, path: Path, *, header: CastHeader | None = None, flush_every: int = 32
    ) -> None:
        self.path = Path(path)
        self.header = header or CastHeader()
        self.flush_every = max(1, flush_every)
        self._buffer: list[CastEvent] = []
        self._started_at: float | None = None
        self._closed = False
        self._written = 0

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("w", encoding="utf-8", newline="\n")
        self._handle.write(self.header.to_json() + "\n")

    @property
    def events_written(self) -> int:
        return self._written

    def start(self) -> None:
        """Begin the clock. Called once the lane is actually producing output."""
        self._started_at = time.monotonic()

    def output(self, data: str) -> None:
        self._append(OUTPUT, data)

    def input(self, data: str) -> None:
        self._append(INPUT, data)

    def marker(self, label: str) -> None:
        self._append(MARKER, label)

    def resize(self, width: int, height: int) -> None:
        self._append(RESIZE, f"{width}x{height}")

    def custom(self, event_type: str, payload: dict[str, Any]) -> None:
        """Emit a namespaced OpenBurrow event."""
        if not event_type.startswith(CUSTOM_PREFIX):
            raise ValueError(f"custom cast events must start with {CUSTOM_PREFIX!r}")
        self._append(event_type, json.dumps(payload, ensure_ascii=False, default=str))

    def _append(self, kind: str, data: str) -> None:
        if self._closed:
            return
        if self._started_at is None:
            self.start()
        assert self._started_at is not None

        if len(data) > MAX_EVENT_CHARS:
            data = data[:MAX_EVENT_CHARS] + TRUNCATION_MARKER

        self._buffer.append(CastEvent(time.monotonic() - self._started_at, kind, data))
        self._written += 1
        if len(self._buffer) >= self.flush_every:
            self.flush()

    def flush(self) -> None:
        if not self._buffer or self._closed:
            return
        self._handle.write("\n".join(event.to_json() for event in self._buffer) + "\n")
        self._handle.flush()
        self._buffer.clear()

    def close(self) -> None:
        if self._closed:
            return
        self.flush()
        self._handle.close()
        self._closed = True

    def __enter__(self) -> CastWriter:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def read_cast(path: Path) -> tuple[CastHeader, list[CastEvent]]:
    """Read a cast file. Tolerates a truncated final line.

    A recording that was interrupted mid-write is the normal case for a session
    that crashed, and a reader that refuses to open it would destroy the evidence
    of exactly the event you most want to look at.
    """
    text = Path(path).read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return CastHeader(), []

    header_payload = _parse_object(lines[0]) or {}
    header = CastHeader(
        version=int(header_payload.get("version", 2)),
        width=int(header_payload.get("width", 120)),
        height=int(header_payload.get("height", 30)),
        timestamp=int(header_payload.get("timestamp", 0)),
        idle_time=float(header_payload.get("idle_time_limit", 2.0)),
        title=str(header_payload.get("title", "")),
        env=dict(header_payload.get("env") or {}),
    )

    events: list[CastEvent] = []
    for line in lines[1:]:
        parsed = _parse_array(line)
        if parsed is None or len(parsed) < 3:
            continue
        try:
            events.append(CastEvent(float(parsed[0]), str(parsed[1]), str(parsed[2])))
        except (TypeError, ValueError):
            continue
    return header, events


def iter_output(events: list[CastEvent]) -> Iterator[tuple[float, str]]:
    """Yield only terminal output, as ``(seconds, text)``."""
    for event in events:
        if event.kind == OUTPUT:
            yield event.time, event.data


def custom_events(events: list[CastEvent], event_type: str = "") -> Iterator[CastEvent]:
    """Yield OpenBurrow's own events, optionally filtered by type."""
    for event in events:
        if not event.kind.startswith(CUSTOM_PREFIX):
            continue
        if event_type and event.kind != event_type:
            continue
        yield event


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences.

    Used for search indexes and previews. The cast itself keeps the escapes,
    because a replay that has been stripped of colour is no longer a faithful
    recording of what the user saw.
    """
    import re

    return re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", text)


def _parse_object(line: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(line)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _parse_array(line: str) -> list[Any] | None:
    try:
        parsed = json.loads(line)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, list) else None


__all__ = [
    "CUSTOM_EVENT_TYPES",
    "CUSTOM_PREFIX",
    "EVENT_CLAIM",
    "EVENT_GOVERNANCE",
    "EVENT_LANE",
    "EVENT_NEGOTIATION",
    "EVENT_TASK",
    "INPUT",
    "MARKER",
    "OUTPUT",
    "RESIZE",
    "CastEvent",
    "CastHeader",
    "CastWriter",
    "custom_events",
    "iter_output",
    "read_cast",
    "strip_ansi",
]
