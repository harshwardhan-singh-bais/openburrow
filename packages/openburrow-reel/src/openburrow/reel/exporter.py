"""Turning a recording into something a person can actually open.

The export produces two things that answer different questions, and both are in
the bundle on purpose:

**A static HTML transcript scrubber.** One file, no network requests, no CDN, no
build step. It shows every lane's output and the causal event log on a shared
clock, with a scrubber and click-to-trace causality. It works from a file:// URL,
inside a corporate network, and attached to an incident ticket — which is where
reels are actually read.

**The ``.cast`` files.** The faithful terminal recordings, playable with
``asciinema play`` or ``agg``. The HTML viewer shows *stripped* text, because
writing an ANSI renderer for the browser would mean either shipping a large
dependency or writing a subtly wrong one, and a subtly wrong replay is worse than
an honest transcript. The casts are the replay; the HTML is the index.

Everything is redacted before it is written or signed — see
:mod:`openburrow.reel.share` for why that order is not negotiable.

The bundle is also readable by the Next.js viewer, which consumes
``manifest.json`` and ``reel.json`` and adds the things a static file cannot do:
live sessions, team-level comparison, and comment threads.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from openburrow.core.logging import get_logger
from openburrow.core.models import ReelManifest
from openburrow.reel.cast import iter_output, read_cast
from openburrow.reel.recorder import ReelRecording
from openburrow.reel.share import (
    RedactionReport,
    redact_payload,
    sign,
)
from openburrow.reel.timeline import read_timeline

log = get_logger(__name__)

VIEWER_VERSION = "1"

#: Events beyond this are truncated in the HTML payload. The full log stays in
#: ``timeline.jsonl``; the viewer gets what it can render at 60fps.
MAX_VIEWER_EVENTS = 5_000

#: Characters of terminal output kept per lane in the HTML payload. A ten-hour
#: session can produce more text than a browser will happily hold in a script tag.
MAX_VIEWER_CHARS = 400_000

TRUNCATION_NOTE = "\n…[reel viewer truncated; full output is in the .cast file]\n"


class ReelExportError(RuntimeError):
    """The reel could not be exported."""


def export(
    recording: ReelRecording,
    *,
    directory: Path,
    plan: dict[str, Any] | None = None,
    audit: list[dict[str, Any]] | None = None,
    metrics: dict[str, Any] | None = None,
    total_cost_usd: float = 0.0,
    share_secret: str = "",
    ttl_hours: int = 168,
    allowed_orgs: list[str] | None = None,
) -> tuple[ReelManifest, RedactionReport]:
    """Write a complete reel bundle and return its manifest.

    Redaction runs over the whole payload in one pass, before any file is written.
    Doing it per-file would mean a new output file could be added later and
    quietly bypass redaction — a bug that is invisible until the day it is not.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)

    timeline = read_timeline(recording.timeline_path)
    lanes_payload, cast_files, event_count = _lane_payloads(recording)
    governance_count = sum(1 for entry in timeline if entry.type.startswith("governance."))
    negotiation_count = sum(1 for entry in timeline if entry.type.startswith("negotiation."))

    payload: dict[str, Any] = {
        "session": {
            "id": recording.session_id,
            "name": recording.session_name,
            "duration_s": recording.duration_s,
            "started_at": recording.started_at,
        },
        "lanes": lanes_payload,
        "timeline": [asdict(entry) for entry in timeline[:MAX_VIEWER_EVENTS]],
        "coverage": [
            {
                "lane_id": lane.lane_id,
                "has_cast": lane.has_cast,
                "reason_missing": lane.reason_missing,
            }
            for lane in recording.lane_coverage
        ],
    }

    redacted, report = redact_payload(payload)

    _write_json(directory / "reel.json", redacted)
    _write_json(directory / "plan.json", plan or {})
    _write_json(directory / "audit.json", audit or [])
    _write_json(directory / "metrics.json", metrics or {})

    manifest = ReelManifest(
        session_id=recording.session_id,
        session_name=recording.session_name,
        exporter=f"openburrow-reel/{VIEWER_VERSION}",
        cast_files=cast_files,
        timeline_file=recording.timeline_path.name,
        plan_file="plan.json",
        audit_file="audit.json",
        metrics_file="metrics.json",
        lane_count=len(recording.lane_coverage),
        event_count=event_count + len(timeline),
        negotiation_count=negotiation_count,
        governance_event_count=governance_count,
        duration_seconds=recording.duration_s,
        total_cost_usd=total_cost_usd,
        allowed_orgs=sorted(allowed_orgs or []),
        public=False,
        viewer_version=VIEWER_VERSION,
    )

    if share_secret:
        token = sign(
            manifest.id,
            secret=share_secret,
            ttl_hours=ttl_hours,
            allowed_orgs=allowed_orgs,
        )
        manifest.signed = True
        manifest.signature = token.encode()
        manifest.expires_at = token.expires_at

    _write_json(directory / "manifest.json", manifest.model_dump(mode="json"))
    (directory / "index.html").write_text(render_html(redacted, manifest, report), encoding="utf-8")

    log.info(
        "reel.exported",
        session_id=recording.session_id,
        directory=str(directory),
        lanes=manifest.lane_count,
        events=manifest.event_count,
        redactions=report.total,
        signed=manifest.signed,
    )
    return manifest, report


def render_html(
    payload: dict[str, Any],
    manifest: ReelManifest,
    report: RedactionReport | None = None,
) -> str:
    """Render the standalone viewer."""
    from jinja2 import Environment, PackageLoader, select_autoescape

    env = Environment(
        loader=PackageLoader("openburrow.reel", "templates"),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template("reel.html")

    duration = float(payload.get("session", {}).get("duration_s", 0.0))
    redacted_types = ""
    if report is not None and report.counts:
        redacted_types = ", ".join(
            f"{label} ×{count}"  # noqa: RUF001 - display only
            for label, count in sorted(report.counts.items(), key=lambda p: p[1], reverse=True)
        )

    return template.render(
        session_name=manifest.session_name or manifest.session_id,
        lane_count=manifest.lane_count,
        duration=duration,
        duration_label=_duration_label(duration),
        event_count=manifest.event_count,
        negotiation_count=manifest.negotiation_count,
        governance_event_count=manifest.governance_event_count,
        exported_label=manifest.exported_at.strftime("%Y-%m-%d %H:%M UTC"),
        viewer_version=VIEWER_VERSION,
        redactions=report.total if report else 0,
        redacted_types=redacted_types,
        # ``</`` must not appear inside the script tag or the JSON can close it
        # early and the rest becomes markup. Escaping the slash is the standard
        # fix and leaves the JSON semantically identical.
        reel_json=json.dumps(payload, ensure_ascii=False, default=str).replace("</", "<\\/"),
    )


def _lane_payloads(
    recording: ReelRecording,
) -> tuple[list[dict[str, Any]], dict[str, str], int]:
    """Read each lane's cast into a viewer-friendly event list."""
    lanes: list[dict[str, Any]] = []
    cast_files: dict[str, str] = {}
    total = 0

    for lane in recording.lane_coverage:
        events: list[list[Any]] = []
        chars = 0
        if lane.has_cast and lane.cast_path:
            try:
                _, cast_events = read_cast(Path(lane.cast_path))
            except OSError as exc:
                log.warning("reel.export.cast_unreadable", lane_id=lane.lane_id, error=str(exc))
                cast_events = []
            for at, text in iter_output(cast_events):
                if chars >= MAX_VIEWER_CHARS:
                    events.append([at, TRUNCATION_NOTE])
                    break
                events.append([round(at, 3), text])
                chars += len(text)
            cast_files[lane.lane_id] = Path(lane.cast_path).name
            total += len(cast_events)

        lanes.append(
            {
                "id": lane.lane_id,
                "title": lane.title or lane.lane_id,
                "has_cast": lane.has_cast,
                "reason_missing": lane.reason_missing,
                "output_events": lane.output_events,
                "input_events": lane.input_events,
                "events": events,
            }
        )

    return lanes, cast_files, total


def _write_json(path: Path, data: Any) -> None:
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )


def _duration_label(seconds: float) -> str:
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


__all__ = [
    "MAX_VIEWER_CHARS",
    "MAX_VIEWER_EVENTS",
    "TRUNCATION_NOTE",
    "VIEWER_VERSION",
    "ReelExportError",
    "export",
    "render_html",
]
