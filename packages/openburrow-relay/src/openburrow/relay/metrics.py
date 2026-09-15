"""Metrics.

A thin wrapper over `prometheus_client` so that exactly one module knows which
metrics library is in use. Route code calls ``METRICS.events_fanned_out.inc()``
and never imports the client library, which means swapping backends later is one
file rather than a grep.

Cardinality is the thing to be careful about. Room ids and member ids are
attacker-influenced strings, so neither appears as a label — a label per room is
how a metrics endpoint turns into an out-of-memory kill. What is labelled is
bounded: ``direction``, ``reason``, ``status``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

#: The default registry is process-global, which makes tests order-dependent:
#: a second `Counter` with the same name raises. A dedicated registry keeps the
#: relay's metrics self-contained and re-creatable.
REGISTRY = CollectorRegistry(auto_describe=True)

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


@dataclass(slots=True)
class RelayMetrics:
    """The relay's metric set."""

    registry: CollectorRegistry

    # --- connections ------------------------------------------------------
    connections_open: Gauge
    connections_total: Counter
    connections_rejected: Counter
    connection_duration: Histogram

    # --- events -----------------------------------------------------------
    events_received: Counter
    events_fanned_out: Counter
    events_duplicate: Counter
    events_dropped: Counter
    frames_rejected: Counter
    lagged_clients: Counter

    # --- storage ----------------------------------------------------------
    db_queries: Counter
    db_query_duration: Histogram
    db_errors: Counter

    # --- auth -------------------------------------------------------------
    tokens_issued: Counter
    auth_failures: Counter
    rate_limited: Counter

    # --- docs -------------------------------------------------------------
    doc_updates_in: Counter
    doc_updates_out: Counter


def build_metrics(registry: CollectorRegistry | None = None) -> RelayMetrics:
    """Build a metric set on its own registry.

    A fresh registry by default, *not* the module-level one. Registering the same
    collector name twice raises ``DuplicateTimeseries``, so a factory that reused
    the default registry would make ``build_metrics()`` a function you can only
    call once per process — which is fine until the second test in a file calls
    it, and then it looks like the code under test is broken.
    """
    reg = registry or CollectorRegistry(auto_describe=True)

    def counter(name: str, doc: str, labels: tuple[str, ...] = ()) -> Counter:
        return Counter(name, doc, labels, registry=reg)

    def gauge(name: str, doc: str, labels: tuple[str, ...] = ()) -> Gauge:
        return Gauge(name, doc, labels, registry=reg)

    def histogram(name: str, doc: str, labels: tuple[str, ...] = ()) -> Histogram:
        return Histogram(name, doc, labels, registry=reg)

    return RelayMetrics(
        registry=reg,
        connections_open=gauge(
            "openburrow_relay_connections_open",
            "WebSocket connections currently open.",
            ("kind",),
        ),
        connections_total=counter(
            "openburrow_relay_connections_total",
            "WebSocket connections accepted, by kind.",
            ("kind",),
        ),
        connections_rejected=counter(
            "openburrow_relay_connections_rejected_total",
            "WebSocket handshakes refused, by reason.",
            ("reason",),
        ),
        connection_duration=histogram(
            "openburrow_relay_connection_duration_seconds",
            "How long a WebSocket stayed open.",
            ("kind",),
        ),
        events_received=counter(
            "openburrow_relay_events_received_total",
            "Bus events accepted from an originating daemon.",
        ),
        events_fanned_out=counter(
            "openburrow_relay_events_fanned_out_total",
            "Event deliveries attempted, one per recipient.",
        ),
        events_duplicate=counter(
            "openburrow_relay_events_duplicate_total",
            "Events discarded because (repo, seq) was already stored.",
        ),
        events_dropped=counter(
            "openburrow_relay_events_dropped_total",
            "Frames dropped because a client's outbound queue was full.",
        ),
        frames_rejected=counter(
            "openburrow_relay_frames_rejected_total",
            "Inbound frames refused, by reason.",
            ("reason",),
        ),
        lagged_clients=counter(
            "openburrow_relay_lagged_clients_total",
            "Times a client was told it had missed events and must re-tail.",
        ),
        db_queries=counter(
            "openburrow_relay_db_queries_total",
            "Database round trips, by operation.",
            ("op",),
        ),
        db_query_duration=histogram(
            "openburrow_relay_db_query_duration_seconds",
            "Database round trip latency, by operation.",
            ("op",),
        ),
        db_errors=counter(
            "openburrow_relay_db_errors_total",
            "Database failures, by operation.",
            ("op",),
        ),
        tokens_issued=counter(
            "openburrow_relay_tokens_issued_total",
            "JWTs minted.",
        ),
        auth_failures=counter(
            "openburrow_relay_auth_failures_total",
            "Rejected credentials, by reason.",
            ("reason",),
        ),
        rate_limited=counter(
            "openburrow_relay_rate_limited_total",
            "Requests refused by the rate limiter, by surface.",
            ("surface",),
        ),
        doc_updates_in=counter(
            "openburrow_relay_doc_updates_in_total",
            "CRDT updates accepted from a client.",
        ),
        doc_updates_out=counter(
            "openburrow_relay_doc_updates_out_total",
            "CRDT updates relayed to other clients.",
        ),
    )


METRICS = build_metrics()


def render(metrics: RelayMetrics | None = None) -> bytes:
    """Serialise the metric set for the /metrics endpoint."""
    return generate_latest((metrics or METRICS).registry)


def _family(metric: Counter | Gauge) -> Any:
    """The one metric family behind a counter or gauge.

    ``collect()`` is typed as returning an ``Iterable``, so indexing it is not
    statically valid even though the runtime object is always a single-element
    list. ``next(iter(...))`` says "the first family" without pretending the
    result is a sequence — it is what the annotation actually promises, and it
    avoids building a list to throw away. Both call sites used to write
    ``list(...)[0]`` and each carried its own comment explaining the same
    assumption; this is that assumption, stated once.

    Accepts ``Gauge`` as well as ``Counter``: ``connections_open`` is a gauge,
    and both expose ``collect()`` through their common base. Typing this as
    ``Counter`` alone was my first version, and mypy caught it — which is the
    argument for annotating the helper rather than inlining the call twice.
    """
    return next(iter(metric.collect()))


def snapshot(metrics: RelayMetrics | None = None) -> dict[str, Any]:
    """A JSON-able view of the counters, for the health endpoints.

    Prometheus text is for Prometheus. The dashboard wants a dict, and parsing
    the text format to get one would be silly.
    """
    m = metrics or METRICS
    return {
        "connections_open": {
            sample.labels.get("kind", ""): sample.value
            for sample in _family(m.connections_open).samples
            if sample.name.endswith("_open")
        },
        "events_received": _total(m.events_received),
        "events_fanned_out": _total(m.events_fanned_out),
        "events_duplicate": _total(m.events_duplicate),
        "events_dropped": _total(m.events_dropped),
        "lagged_clients": _total(m.lagged_clients),
        "auth_failures": _total(m.auth_failures),
        "rate_limited": _total(m.rate_limited),
    }


def _total(counter: Counter) -> float:
    for sample in _family(counter).samples:
        if sample.name.endswith("_total"):
            return sample.value
    return 0.0


__all__ = [
    "CONTENT_TYPE",
    "METRICS",
    "REGISTRY",
    "RelayMetrics",
    "build_metrics",
    "render",
    "snapshot",
]
