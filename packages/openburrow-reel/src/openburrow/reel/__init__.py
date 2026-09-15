"""Session reels: recording a session so it can be watched, searched, and shared.

A reel is what makes a multi-agent session reviewable. Without one, the only
record of what happened is a scrollback buffer that dies with the terminal, and
the questions people actually ask afterwards — "why did lane C stop", "which lane
touched auth.py first", "was the approval actually granted" — have no answer.

Four parts, each doing one thing:

:mod:`openburrow.reel.cast`
    asciinema v2 cast files. A real, widely-supported format so a replay plays in
    tools people already have.

:mod:`openburrow.reel.timeline`
    The causal event log. Shares a clock with the casts so the two can be overlaid
    on one axis, and records ``caused_by`` so a viewer can trace why something
    happened rather than only when.

:mod:`openburrow.reel.recorder`
    Collects both, passively. It never summarises or filters, because a record
    that has been curated is a document, and a document can be wrong in ways a
    record cannot.

:mod:`openburrow.reel.exporter`
    Produces a self-contained HTML transcript scrubber plus the cast files, and a
    JSON bundle the Next.js viewer reads. Redaction happens here, before anything
    is written or signed.

:mod:`openburrow.reel.share`
    Signed, expiring share links. Redact first, sign second — the ordering is the
    design, and the module docstring explains why reversing it is worse than not
    signing at all.
"""

from openburrow.reel.cast import (
    CastEvent,
    CastHeader,
    CastWriter,
    custom_events,
    iter_output,
    read_cast,
    strip_ansi,
)
from openburrow.reel.exporter import ReelExportError, export, render_html
from openburrow.reel.recorder import LaneCoverage, ReelRecorder, ReelRecording
from openburrow.reel.share import (
    RedactionReport,
    ShareToken,
    build_share_url,
    new_secret,
    preview_redactions,
    redact,
    redact_payload,
    sign,
    verify,
)
from openburrow.reel.timeline import (
    TimelineEntry,
    TimelineWriter,
    between,
    causal_chain,
    effects_of,
    read_timeline,
)

__all__ = [
    "CastEvent",
    "CastHeader",
    "CastWriter",
    "LaneCoverage",
    "RedactionReport",
    "ReelExportError",
    "ReelRecorder",
    "ReelRecording",
    "ShareToken",
    "TimelineEntry",
    "TimelineWriter",
    "between",
    "build_share_url",
    "causal_chain",
    "custom_events",
    "effects_of",
    "export",
    "iter_output",
    "new_secret",
    "preview_redactions",
    "read_cast",
    "read_timeline",
    "redact",
    "redact_payload",
    "render_html",
    "sign",
    "strip_ansi",
    "verify",
]
