"""Recording a session into a reel.

The recorder is deliberately passive: it observes and writes, and never
interprets. That restraint is what makes a reel trustworthy as evidence. The
moment a recorder starts summarising, filtering, or "cleaning up" what it
observes, the reel stops being a record of the session and becomes a document
about the session — and a document can be wrong in ways a record cannot.

Three practical consequences:

**Nothing is dropped for being uninteresting.** Governance flags, failed
negotiations, and crashed lanes are the parts people actually need to see. A reel
that shows only the happy path is a demo, not a record.

**Partial coverage is reported, not hidden.** If a lane's cast file cannot be
opened, that lane's terminal output is still recorded in the timeline, and the
manifest records the gap. Silently omitting a lane would produce a reel that
looks complete and is not.

**The search index is a convenience, not the source.** It is built from
ANSI-stripped text so queries match what a human read, while the cast keeps the
raw bytes so the replay is faithful. Both are kept because they answer different
questions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openburrow.core.logging import get_logger
from openburrow.reel.cast import (
    EVENT_CLAIM,
    EVENT_GOVERNANCE,
    EVENT_LANE,
    EVENT_NEGOTIATION,
    EVENT_TASK,
    CastHeader,
    CastWriter,
    strip_ansi,
)
from openburrow.reel.timeline import TimelineEntry, TimelineWriter

log = get_logger(__name__)

#: Bus event type prefix → the cast custom event it becomes. Prefix matching
#: rather than an exhaustive map, because new bus event types get added as the
#: platform grows and a reel should not go blind when one appears.
_PREFIX_TO_CAST_EVENT: tuple[tuple[str, str], ...] = (
    ("claim.", EVENT_CLAIM),
    ("negotiation.", EVENT_NEGOTIATION),
    ("governance.", EVENT_GOVERNANCE),
    ("task.", EVENT_TASK),
    ("lane.", EVENT_LANE),
    ("delegation.", EVENT_GOVERNANCE),
    ("approval.", EVENT_GOVERNANCE),
)

#: Cap on the in-memory search index. A long session can produce hundreds of
#: megabytes of terminal output, and holding all of it to answer a search that
#: someone runs once is not a trade worth making. The tail is what people search.
MAX_INDEX_ENTRIES = 20_000


@dataclass
class LaneCoverage:
    """What the recorder managed to capture for one lane."""

    lane_id: str
    title: str = ""
    cast_path: str = ""
    has_cast: bool = False
    output_events: int = 0
    input_events: int = 0
    reason_missing: str = ""


@dataclass
class ReelRecording:
    """The result of a recording, as the exporter needs it."""

    session_id: str
    session_name: str
    timeline_path: Path
    lane_coverage: list[LaneCoverage] = field(default_factory=list)
    started_at: float = 0.0
    duration_s: float = 0.0

    @property
    def complete(self) -> bool:
        return all(lane.has_cast for lane in self.lane_coverage)

    @property
    def gaps(self) -> list[LaneCoverage]:
        return [lane for lane in self.lane_coverage if not lane.has_cast]

    def coverage_ratio(self) -> float:
        if not self.lane_coverage:
            return 1.0
        return sum(1 for lane in self.lane_coverage if lane.has_cast) / len(self.lane_coverage)


class ReelRecorder:
    """Collects lane output and bus events into a reel.

    One recorder per session. Thread-affinity is not enforced, but the intended
    use is a single async task feeding it from the bus and the output pumps, so
    no locking is done — adding locks here would suggest a concurrency model the
    daemon does not have.
    """

    def __init__(
        self,
        *,
        session_id: str,
        session_name: str,
        directory: Path,
        width: int = 120,
        height: int = 30,
        enabled: bool = True,
    ) -> None:
        self.session_id = session_id
        self.session_name = session_name
        self.directory = Path(directory)
        self.width = width
        self.height = height
        self.enabled = enabled

        self.directory.mkdir(parents=True, exist_ok=True)
        self._casts: dict[str, CastWriter] = {}
        self._coverage: dict[str, LaneCoverage] = {}
        self._index: list[tuple[str, float, str]] = []
        self._timeline = TimelineWriter(self.directory / "timeline.jsonl")
        self._closed = False

    # --- lanes -------------------------------------------------------------
    def open_lane(self, lane_id: str, *, title: str = "") -> LaneCoverage:
        """Begin recording a lane's terminal.

        Returns a coverage record rather than raising on failure: a session with
        one unrecordable lane should still produce a reel for the rest.
        """
        coverage = LaneCoverage(lane_id=lane_id, title=title or lane_id)
        self._coverage[lane_id] = coverage

        if not self.enabled:
            coverage.reason_missing = "recording disabled by configuration"
            return coverage

        path = self.directory / f"{lane_id}.cast"
        try:
            writer = CastWriter(
                path,
                header=CastHeader(
                    width=self.width,
                    height=self.height,
                    title=f"{self.session_name} · {title or lane_id}",
                ),
            )
            writer.start()
            self._casts[lane_id] = writer
            coverage.cast_path = str(path)
            coverage.has_cast = True
        except OSError as exc:
            coverage.reason_missing = f"could not open cast file: {exc}"
            log.warning("reel.cast.open_failed", lane_id=lane_id, error=str(exc))

        self._timeline.record(
            type="lane.recording_started",
            lane_id=lane_id,
            summary=f"recording {'started' if coverage.has_cast else 'degraded'}",
        )
        return coverage

    def lane_output(self, lane_id: str, chunk: str) -> None:
        """Record a chunk of lane terminal output."""
        coverage = self._coverage.setdefault(lane_id, LaneCoverage(lane_id=lane_id))
        coverage.output_events += 1

        writer = self._casts.get(lane_id)
        if writer is not None:
            writer.output(chunk)

        if len(self._index) < MAX_INDEX_ENTRIES:
            cleaned = strip_ansi(chunk).strip()
            if cleaned:
                self._index.append((lane_id, self._timeline.elapsed, cleaned))

    def lane_input(self, lane_id: str, text: str) -> None:
        """Record text sent *into* a lane.

        Input matters as much as output: a reel where you can see what a lane did
        but not what it was told is a reel that cannot explain anything.
        """
        coverage = self._coverage.setdefault(lane_id, LaneCoverage(lane_id=lane_id))
        coverage.input_events += 1
        writer = self._casts.get(lane_id)
        if writer is not None:
            writer.input(text)
        self._timeline.record(
            type="lane.prompt",
            lane_id=lane_id,
            summary=text[:200],
            payload={"chars": len(text)},
        )

    def lane_stopped(self, lane_id: str, *, reason: str = "") -> None:
        writer = self._casts.get(lane_id)
        if writer is not None:
            writer.marker(f"lane stopped: {reason or 'no reason given'}")
        self._timeline.record(type="lane.stopped", lane_id=lane_id, summary=reason[:300])

    # --- bus ---------------------------------------------------------------
    def record_bus_event(self, event: dict[str, Any]) -> TimelineEntry | None:
        """Record a bus event in the timeline, and mirror it into the casts.

        Mirroring is what makes the reel scrubbable by *cause* rather than only
        by time: a claim appears in the claiming lane's cast at the moment it
        happened, so a viewer can scrub the terminal to the exact point where a
        lane took ownership of a file.
        """
        event_type = str(event.get("event_type") or event.get("type") or "")
        lane_id = str(event.get("lane_id") or "")
        summary = str(event.get("summary") or "")
        payload = dict(event.get("payload") or {})

        entry = self._timeline.record(
            type=event_type,
            lane_id=lane_id,
            summary=summary,
            payload=payload,
            seq=int(event.get("seq") or 0),
            caused_by=str(event.get("caused_by") or ""),
        )

        cast_event = _cast_event_for(event_type)
        if cast_event is not None:
            for target_lane in _cast_targets(event, lane_id):
                writer = self._casts.get(target_lane)
                if writer is not None:
                    writer.custom(
                        cast_event,
                        {
                            "seq": entry.seq,
                            "type": event_type,
                            "lane": lane_id,
                            "summary": summary[:300],
                            "t": entry.t,
                        },
                    )
        return entry

    def record_governance(
        self,
        *,
        kind: str,
        lane_id: str = "",
        summary: str = "",
        payload: dict[str, Any] | None = None,
    ) -> TimelineEntry:
        """Record a governance event.

        Kept as its own method rather than a bus event because governance
        decisions sometimes happen outside the bus — a delegation refused during
        startup, for instance — and those must still land in the reel. An audit
        trail with a hole where the refusals were is worse than no trail.
        """
        entry = self._timeline.record(
            type=f"governance.{kind}",
            lane_id=lane_id,
            summary=summary,
            payload=payload or {},
        )
        writer = self._casts.get(lane_id)
        if writer is not None:
            writer.custom(
                EVENT_GOVERNANCE,
                {"kind": kind, "summary": summary[:300], "t": entry.t},
            )
        return entry

    def mark(self, label: str, *, lane_id: str = "") -> None:
        """Drop a marker into a lane's cast. Used for checkpoints and approvals."""
        writer = self._casts.get(lane_id)
        if writer is not None:
            writer.marker(label)
        self._timeline.record(type="marker", lane_id=lane_id, summary=label)

    # --- search ------------------------------------------------------------
    def search(self, query: str, *, limit: int = 50) -> list[dict[str, Any]]:
        """Substring search over ANSI-stripped output.

        Case-insensitive substring rather than a scored index, because the
        questions people ask a reel are "where did it say 429" and "when did it
        touch auth.py" — literal, not semantic.
        """
        needle = query.casefold().strip()
        if not needle:
            return []
        hits: list[dict[str, Any]] = []
        for lane_id, at, text in reversed(self._index):
            if needle in text.casefold():
                hits.append({"lane_id": lane_id, "t": round(at, 3), "text": text[:400]})
                if len(hits) >= limit:
                    break
        return hits

    # --- lifecycle ---------------------------------------------------------
    @property
    def elapsed(self) -> float:
        return self._timeline.elapsed

    def close(self) -> ReelRecording:
        """Flush everything and return what was captured."""
        if self._closed:
            return self._result()

        for writer in self._casts.values():
            writer.close()
        self._timeline.record(type="session.recording_ended", summary="recording closed")
        self._timeline.close()
        self._closed = True

        result = self._result()
        if result.gaps:
            log.warning(
                "reel.recording.partial",
                session_id=self.session_id,
                missing=[gap.lane_id for gap in result.gaps],
            )
        else:
            log.info(
                "reel.recording.complete",
                session_id=self.session_id,
                lanes=len(result.lane_coverage),
                duration_s=round(result.duration_s, 1),
            )
        return result

    def _result(self) -> ReelRecording:
        return ReelRecording(
            session_id=self.session_id,
            session_name=self.session_name,
            timeline_path=self.directory / "timeline.jsonl",
            lane_coverage=sorted(self._coverage.values(), key=lambda c: c.lane_id),
            duration_s=round(self._timeline.elapsed, 3),
        )

    def __enter__(self) -> ReelRecorder:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _cast_event_for(event_type: str) -> str | None:
    for prefix, cast_event in _PREFIX_TO_CAST_EVENT:
        if event_type.startswith(prefix):
            return cast_event
    return None


def _cast_targets(event: dict[str, Any], lane_id: str) -> list[str]:
    """Which lanes' casts should show this event.

    Both the actor and the recipients, because a message is part of both lanes'
    stories. Recording it only for the sender would make the receiving lane's
    replay inexplicable — it would react to something the viewer never saw.
    """
    targets = [lane_id] if lane_id else []
    recipients = event.get("recipients") or event.get("target_lanes") or []
    if isinstance(recipients, list):
        targets.extend(str(item) for item in recipients if item)
    return list(dict.fromkeys(targets))


__all__ = ["LaneCoverage", "ReelRecorder", "ReelRecording"]
