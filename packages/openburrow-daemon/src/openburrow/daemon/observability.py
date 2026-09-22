"""Observability wiring (Stage 16, items 212-213): OTel spans and Prometheus.

Both exporters are **opt-in and must fail closed to no-ops.** The daemon runs on
a developer's laptop more often than on a server; a tracing backend that cannot
be reached must degrade to "spans discarded", not to exceptions in the session
supervisor. Every function here is therefore safe to call with no collector
present and cheap to call with the feature off.

Instrumentation points are deliberately few: the daemon server loop, the lane
supervisor, and the relay client. Span-per-IPC-call would produce noise at a
rate no one would ever read; the value is in following one session lifecycle
across process boundaries.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Iterator
from typing import Any

from openburrow.core.config.settings import Settings
from openburrow.core.logging import get_logger

log = get_logger(__name__)

_tracer: Any = None
_meter: Any = None
_provider: Any = None
_prometheus_server: Any = None
_lock = threading.Lock()
_prometheus_started = False


def setup_observability(settings: Settings) -> dict[str, Any]:
    """Initialise OTel tracing and the Prometheus exporter if configured.

    Returns a status dict for ``burrow status`` — what was actually switched on,
    not what the config says (rule 6: if you cannot tell, say so). Never raises:
    observability is infrastructure, and a misconfigured endpoint must not take
    the daemon down with it.
    """
    status: dict[str, Any] = {"otel": "disabled", "prometheus": "disabled"}
    try:
        status["otel"] = _setup_otel(settings)
    except Exception as exc:
        log.warning("otel.setup_failed", error=str(exc))
        status["otel"] = f"error: {exc}"
    try:
        status["prometheus"] = _setup_prometheus(settings)
    except Exception as exc:
        log.warning("prometheus.setup_failed", error=str(exc))
        status["prometheus"] = f"error: {exc}"
    # Reported separately from the two config echoes above, because "the setting
    # is on" and "spans are actually being recorded" are different facts. An
    # exporter pointed at a collector that is not there reports "enabled" and
    # records nothing, and that gap is invisible unless it is stated.
    status["tracing_active"] = _tracing_active()
    return status


def shutdown_observability() -> None:
    """Flush and release the exporters, then drop the references.

    The flush is the part that matters and the part that was missing. Nulling
    the module globals released nothing: the ``TracerProvider`` owns a batch
    processor with its own background thread, so spans still queued at shutdown
    were never exported, and the thread kept writing to a stream that had since
    closed — which surfaces as ``ValueError: I/O operation on closed file`` from
    a thread nobody owns, long after the daemon reported a clean stop. A daemon
    that loses the last seconds of every session's traces is a daemon whose
    traces are not worth collecting.

    Still never raises. Shutdown runs on the way out of a session, and an
    exporter that cannot flush must not turn a clean exit into a crash.
    """
    # Module-level handles to the exporters, set once by setup_observability and
    # cleared here. There is no object to hang them on: the daemon holds one of
    # each for the life of the process, which is what a module global is for.
    global _tracer, _meter, _provider, _prometheus_server, _prometheus_started  # noqa: PLW0603
    with _lock:
        provider, _provider = _provider, None
        server, _prometheus_server = _prometheus_server, None
        _tracer = None
        _meter = None
        _prometheus_started = False

    if provider is not None:
        with contextlib.suppress(Exception):
            provider.shutdown()
    if server is not None:
        with contextlib.suppress(Exception):
            server.shutdown()


def _setup_otel(settings: Settings) -> str:
    global _tracer, _provider  # noqa: PLW0603 - see shutdown_observability
    if not settings.otel_enabled:
        return "disabled"
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(
        resource=Resource.create({"service.name": settings.otel_service_name})
    )
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint))
    )
    trace.set_tracer_provider(provider)
    # Held so shutdown can reach the batch processor. ``set_tracer_provider``
    # cannot be undone (OTel allows it once per process), so the module's own
    # reference is the only handle on the thing that needs flushing.
    _provider = provider
    _tracer = trace.get_tracer("openburrow.daemon")
    log.info("otel.ready", endpoint=settings.otel_exporter_otlp_endpoint)
    return "enabled"


def tracer() -> Any:
    """The daemon tracer, or a no-op proxy when OTel is off.

    The no-op proxy is a real object with the same API, so call sites do not
    branch — ``with span("x")`` is correct in both modes.
    """
    if _tracer is None:
        from opentelemetry import trace

        return trace.get_tracer("openburrow.daemon")  # no-op until a provider is set
    return _tracer


def _tracing_active() -> bool:
    """True when a real provider is installed, not just the API's placeholder.

    ``_tracer`` is only set by :func:`_setup_otel`, so testing it alone would
    miss the other supported way tracing gets switched on: ``opentelemetry-
    instrument`` auto-instrumentation, which installs a global provider before
    the daemon is imported and never calls us. Asking the API covers both.
    """
    if _tracer is not None:
        return True
    try:
        from opentelemetry import trace

        return not isinstance(trace.get_tracer_provider(), trace.ProxyTracerProvider)
    except Exception:
        return False


@contextlib.contextmanager
def span(name: str, **attributes: Any) -> Iterator[Any]:
    """Open a tracing span, or open a discarded one when tracing is off.

    This function is why item 212 was marked "deps declared, no spans emitted".
    :func:`tracer`'s docstring has always promised that ``with span("x")`` is the
    call-site pattern and that call sites never branch — but ``span`` did not
    exist, so nothing in the daemon was ever traced and the only evidence the
    feature worked was the function claiming it did. A documented helper that is
    absent is worse than no documentation: it reads as a working feature.

    It **always yields a span object**, never ``None``. The obvious alternative —
    yield ``None`` when tracing is off and let callers check — reintroduces
    exactly the branch the docstring says call sites do not need, and the first
    caller who writes ``with span("x") as s: s.set_attribute(...)`` crashes on
    the happy path. OTel's non-recording span accepts every call and does
    nothing, which is the behaviour we want: a tracing feature that takes
    sessions down is worse than no tracing at all.
    """
    with tracer().start_as_current_span(name) as current:
        for key, value in attributes.items():
            # Attribute setting is the one place a bad value (a non-scalar, an
            # unserialisable object) reaches the exporter. Losing the attribute
            # is acceptable; losing the session it was measuring is not.
            with contextlib.suppress(Exception):
                current.set_attribute(key, value)
        yield current


def _setup_prometheus(settings: Settings) -> str:
    """Expose ``/metrics`` on ``metrics_host:metrics_port`` from a background server."""
    global _prometheus_server, _prometheus_started  # noqa: PLW0603
    if not settings.metrics_prometheus:
        return "disabled"
    if _prometheus_started:
        return "enabled"

    from prometheus_client import start_http_server

    # The address is passed explicitly. Without it `start_http_server` binds
    # `0.0.0.0`, which would publish this machine's session ids, lane names and
    # token spend to the whole network — see the note on `metrics_host`. The
    # default is loopback; a remote scrape target is opt-in.
    #
    # The daemon's own counters are registry-free (the default registry), the
    # same convention the relay package uses, so a Prometheus scrape of the
    # daemon and the relay reads identically.
    server, _thread = start_http_server(settings.metrics_port, addr=settings.metrics_host)
    # Held so shutdown can close it. `_prometheus_started` alone would leave the
    # listening socket open after a stop/start cycle in one process, and the
    # second `setup_observability` would fail to bind with "address in use".
    _prometheus_server = server
    _prometheus_started = True
    log.info("prometheus.ready", host=settings.metrics_host, port=settings.metrics_port)
    return "enabled"


# ---------------------------------------------------------------------------
# Daemon metrics (items 214-219 — the metric set)
# ---------------------------------------------------------------------------
_counters: dict[str, Any] = {}


class _NopCounter:
    """A counter that counts nothing, for when ``prometheus_client`` is absent.

    Call sites stay identical in both modes, for the same reason :func:`span` is
    a no-op context manager: a branch per metric is a branch somebody eventually
    writes only on the happy path. ``labels()`` returns ``self`` so a labelled
    call chains exactly as it would against the real client.
    """

    def inc(self, amount: float = 1, **_labels: Any) -> None:
        return None

    def labels(self, **_labels: Any) -> _NopCounter:
        return self


def _counter(name: str, doc: str, *labelnames: str) -> Any:
    with _lock:
        if name not in _counters:
            try:
                from prometheus_client import Counter

                _counters[name] = Counter(name, doc, labelnames=labelnames)
            except Exception:
                _counters[name] = _NopCounter()
        return _counters[name]


token_cost = _counter(
    "openburrow_daemon_token_usage_total",
    "Reported harness usage, summed per session where the harness reports it.",
    "session",
)
cost_usd = _counter(
    "openburrow_daemon_cost_usd_total",
    "Reported harness cost in USD, by session. Zero when the harness reports none.",
    "session",
)
replan_total = _counter(
    "openburrow_daemon_replans_total",
    "Forced and voluntary replans, by trigger.",
    "trigger",
)
retry_total = _counter(
    "openburrow_daemon_retries_total",
    "Lane retries, by outcome (succeeded/exhausted).",
    "outcome",
)
lesson_injections = _counter(
    "openburrow_daemon_lesson_injections_total", "Lessons injected into lane context."
)
lesson_hits = _counter(
    "openburrow_daemon_lesson_hits_total", "Injections the receiving lane acted on."
)
bus_messages = _counter(
    "openburrow_daemon_bus_messages_total", "Bus messages sent, by family.", "family"
)
bus_replies = _counter(
    "openburrow_daemon_bus_replies_total", "Direct replies to bus messages (reply-rate numerator)."
)
lane_outputs = _counter(
    "openburrow_daemon_lane_outputs_total", "Harness output chunks published, by kind.", "kind"
)


def record_usage(session_id: str, tokens: int, cost: float | None = None) -> None:
    """Record reported harness usage (item 214), labelled by session.

    Labelled rather than global, because the metric is specified as *per
    session* and a single running total cannot answer "which session spent
    this". The docstring said "summed per session" while the counter summed
    everything into one series, and the unused ``session_id`` and ``cost``
    parameters were the only evidence — a signature that takes two arguments it
    discards is a feature that was never finished.

    No-op when the harness reports nothing. A missing number is honest; a
    fabricated one is not, and a zero here means "not reported", never "free".
    """
    key = session_id or "unknown"
    if tokens > 0:
        token_cost.labels(session=key).inc(tokens)
    if cost is not None and cost > 0:
        cost_usd.labels(session=key).inc(cost)


def record_replan(trigger: str = "unknown") -> None:
    """Item 215's replan counter. ``trigger`` separates the causes: a silent
    failure, a negotiation outcome, and a human edit are not the same event and
    averaging them hides whichever one is actually climbing."""
    replan_total.labels(trigger=trigger or "unknown").inc()


def record_retry(outcome: str = "succeeded") -> None:
    """Item 215's retry counter, split by outcome so the exhaustion rate is
    readable without subtracting one series from another."""
    retry_total.labels(outcome=outcome or "succeeded").inc()


def record_lesson_injection(count: int = 1) -> None:
    """Item 217's denominator: lessons actually put in front of a lane.

    Distinct from "lessons stored" — a store nobody injects from is the failure
    this pair of counters exists to make visible.
    """
    if count > 0:
        lesson_injections.inc(count)


def record_lesson_hit(count: int = 1) -> None:
    """Item 217's numerator: injections the receiving lane then acted on.

    Item 54's "did this message change the receiver's behaviour" is the same
    measurement one layer down, so the two share this counter rather than
    growing two numbers that would drift apart.
    """
    if count > 0:
        lesson_hits.inc(count)


def record_bus_message(family: str = "inform") -> None:
    """Item 218's volume counter, labelled by message family."""
    bus_messages.labels(family=family or "inform").inc()


def record_bus_reply() -> None:
    """Item 218's reply counter. The ratio of this to :func:`record_bus_message`
    is the bus-health signal: a bus that talks and never answers is one where
    lanes are broadcasting into a void."""
    bus_replies.inc()


def record_lane_output(kind: str = "text") -> None:
    """Harness output chunks published, by kind.

    Deliberately *not* folded into :func:`record_bus_message`. The two look
    similar and are not: one counts events written to the bus log, the other
    counts chunks read off a harness's PTY, and the pump emits the second kind
    while the log holds the first. Sharing a counter name would make
    ``bus_messages`` mean two things at once, and the original code did exactly
    that — it called ``bus_messages.labels(output.kind)`` on a counter
    constructed with no label names, which raises ``ValueError`` on the first
    harness that produced any output.
    """
    lane_outputs.labels(kind=kind or "text").inc()


def event_family(event_type: str) -> str:
    """The leading segment of a dotted event type — ``a2a.task.updated`` → ``a2a``.

    The label exists so the volume metric answers "which subsystem is talking"
    rather than "the bus emitted 40,000 events". Derived rather than declared
    because a declared family is a second place to update when an event type is
    renamed, and the two would diverge silently.
    """
    return (event_type or "").split(".", 1)[0] or "unknown"


__all__ = [
    "bus_messages",
    "bus_replies",
    "cost_usd",
    "event_family",
    "lane_outputs",
    "lesson_hits",
    "lesson_injections",
    "record_bus_message",
    "record_bus_reply",
    "record_lane_output",
    "record_lesson_hit",
    "record_lesson_injection",
    "record_replan",
    "record_retry",
    "record_usage",
    "replan_total",
    "retry_total",
    "setup_observability",
    "shutdown_observability",
    "span",
    "token_cost",
    "tracer",
]
