"""The causal event log that sits beside the cast.

A cast tells you what a terminal showed. It cannot tell you *why*, because most
of what drives a multi-agent session never appears in any terminal: a claim
created, a negotiation counter-offer, a governance flag, a task handed off. A
reel built only from casts is a set of videos with no plot.

So a reel has two halves that share one clock:

* **casts** — one per lane, written by :mod:`openburrow.reel.cast`
* **the timeline** — one JSONL file, written here

Both measure time as *seconds since the session started*, which is what lets the
viewer put them on the same axis. That is the whole reason for sharing a clock
rather than storing absolute timestamps: an absolute timestamp needs a timezone
to be read, and a viewer that has to reason about timezones to align two streams
will get it wrong.

The field that makes this *causal* rather than merely chronological is
``caused_by``. Every entry may name the entry that produced it. Without it, a
reel is a list of things that happened in an order; with it, you can ask "why did
lane C stop working" and walk backwards: lane C stopped because it received a
handoff, which happened because lane B hit an approval gate, which happened
because a claim was refused. That chain is the thing a person actually wants when
they watch a session that went wrong.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from openburrow.core.logging import get_logger

log = get_logger(__name__)

#: How far back :func:`causal_chain` will walk. A cycle in ``caused_by`` would
#: otherwise hang the viewer, and a genuine chain deeper than this is more likely
#: a bug than a story worth reading.
MAX_CHAIN_DEPTH = 64


@dataclass(slots=True)
class TimelineEntry:
    """One thing that happened, with optional attribution to its cause."""

    #: Seconds since the session started. Shares a clock with the casts.
    t: float
    #: Monotonic bus sequence, when the entry came from the bus.
    seq: int = 0
    #: Dotted type, e.g. ``claim.created`` or ``negotiation.move``.
    type: str = ""
    lane_id: str = ""
    summary: str = ""
    #: Entry id this one was caused by, for building chains.
    caused_by: str = ""
    #: Stable id for this entry, so other entries can point at it.
    id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, default=str)


class TimelineWriter:
    """Append-only JSONL writer for a session's causal log.

    Append-only for the same reason the bus is: a timeline that can be rewritten
    is a timeline that can be made to disagree with what happened, and the whole
    point of keeping one is that it is the record of what happened.
    """

    def __init__(self, path: Path, *, started_at: float | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._origin = started_at if started_at is not None else time.monotonic()
        self._handle = self.path.open("a", encoding="utf-8", newline="\n")
        self._counter = 0
        self._closed = False
        #: Recent entries, kept so ``caused_by`` can be resolved to a summary
        #: without re-reading the file. Bounded because a long session should not
        #: accumulate its whole history in memory.
        self._recent: dict[str, TimelineEntry] = {}
        self._recent_order: list[str] = []

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._origin

    def record(
        self,
        *,
        type: str,  # noqa: A002 - matches the wire field name
        lane_id: str = "",
        summary: str = "",
        payload: dict[str, Any] | None = None,
        seq: int = 0,
        caused_by: str = "",
        at: float | None = None,
    ) -> TimelineEntry:
        """Append an entry and return it."""
        if self._closed:
            return TimelineEntry(t=self.elapsed, type=type, lane_id=lane_id, summary=summary)

        self._counter += 1
        entry = TimelineEntry(
            t=round(at if at is not None else self.elapsed, 6),
            seq=seq,
            type=type,
            lane_id=lane_id,
            summary=summary[:500],
            caused_by=caused_by,
            id=f"tl_{self._counter:06d}",
            payload=payload or {},
        )
        self._handle.write(entry.to_json() + "\n")
        self._handle.flush()

        self._recent[entry.id] = entry
        self._recent_order.append(entry.id)
        while len(self._recent_order) > 512:
            self._recent.pop(self._recent_order.pop(0), None)
        return entry

    def describe(self, entry_id: str) -> str:
        """Human-readable summary of an entry, or ``""`` when it has aged out."""
        entry = self._recent.get(entry_id)
        if entry is None:
            return ""
        return f"{entry.type} @ {entry.t:.2f}s" + (f" ({entry.summary})" if entry.summary else "")

    def close(self) -> None:
        if self._closed:
            return
        self._handle.close()
        self._closed = True

    def __enter__(self) -> TimelineWriter:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def read_timeline(path: Path) -> list[TimelineEntry]:
    """Read a timeline, skipping any malformed line.

    A truncated last line is expected after a crash, and losing the whole file
    because of one bad line would discard the evidence of the crash itself.
    """
    entries: list[TimelineEntry] = []
    text = Path(path).read_text(encoding="utf-8")
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        try:
            entries.append(
                TimelineEntry(
                    t=float(payload.get("t", 0.0)),
                    seq=int(payload.get("seq", 0)),
                    type=str(payload.get("type", "")),
                    lane_id=str(payload.get("lane_id", "")),
                    summary=str(payload.get("summary", "")),
                    caused_by=str(payload.get("caused_by", "")),
                    id=str(payload.get("id", "")),
                    payload=dict(payload.get("payload") or {}),
                )
            )
        except (TypeError, ValueError):
            continue
    return entries


def causal_chain(entries: list[TimelineEntry], entry_id: str) -> list[TimelineEntry]:
    """Walk ``caused_by`` backwards from ``entry_id``.

    Returns the chain oldest-first, so it reads as a narrative: this happened,
    therefore that happened, therefore the thing you asked about happened.
    """
    by_id = {entry.id: entry for entry in entries if entry.id}
    chain: list[TimelineEntry] = []
    seen: set[str] = set()
    current = entry_id

    while current and len(chain) < MAX_CHAIN_DEPTH:
        if current in seen:
            log.warning("reel.timeline.cycle", entry_id=current)
            break
        seen.add(current)
        entry = by_id.get(current)
        if entry is None:
            break
        chain.append(entry)
        current = entry.caused_by

    chain.reverse()
    return chain


def effects_of(entries: list[TimelineEntry], entry_id: str) -> list[TimelineEntry]:
    """Direct effects of an entry, i.e. entries naming it as their cause."""
    return [entry for entry in entries if entry.caused_by == entry_id]


def between(entries: list[TimelineEntry], start: float, end: float) -> list[TimelineEntry]:
    """Entries in a time window, for scrubbing the viewer."""
    return [entry for entry in entries if start <= entry.t <= end]


def lanes_seen(entries: list[TimelineEntry]) -> list[str]:
    return sorted({entry.lane_id for entry in entries if entry.lane_id})


def iter_types(entries: list[TimelineEntry]) -> Iterator[tuple[str, int]]:
    """Counts by type, for the reel's summary panel."""
    counts: dict[str, int] = {}
    for entry in entries:
        counts[entry.type] = counts.get(entry.type, 0) + 1
    yield from sorted(counts.items(), key=lambda pair: pair[1], reverse=True)


__all__ = [
    "MAX_CHAIN_DEPTH",
    "TimelineEntry",
    "TimelineWriter",
    "between",
    "causal_chain",
    "effects_of",
    "iter_types",
    "lanes_seen",
    "read_timeline",
]
