"""Observability wiring (items 212-213) and the daemon metric surface.

The contract under test is that the exporters **fail closed to no-ops**: with
the features off, everything reports "disabled" and the counters still count.
An observability layer that raises when its backend is missing has its failure
priority exactly backwards.

The metric tests drive the *public recorders* rather than poking ``_value`` on
the counter objects. That is not stylistic. ``_value`` only exists on an
unlabelled ``prometheus_client.Counter``; the moment a counter gains a label,
``counter._value`` raises ``AttributeError`` and ``counter.inc()`` raises
``ValueError``. The previous version of this file did both, which is how two
real defects stayed invisible:

* ``sessions._handle_crash`` called an **undefined** ``retry_total`` — a
  ``NameError`` on the crash path, in the one branch that only runs when a
  harness dies;
* the output pump called ``bus_messages.labels(output.kind)`` on a counter
  constructed with no label names, which raises on the first chunk of harness
  output.

Both survived because no test ever ran those branches and because the tests that
did touch metrics reached past the API into storage that a label would remove.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from openburrow.core.config.settings import Settings
from openburrow.daemon.observability import (
    bus_messages,
    bus_replies,
    cost_usd,
    event_family,
    lane_outputs,
    lesson_hits,
    lesson_injections,
    record_bus_message,
    record_bus_reply,
    record_lane_output,
    record_lesson_hit,
    record_lesson_injection,
    record_replan,
    record_retry,
    record_usage,
    replan_total,
    retry_total,
    setup_observability,
    shutdown_observability,
    span,
    token_cost,
    tracer,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    shutdown_observability()
    yield
    shutdown_observability()


def value_of(counter: Any, **labels: str) -> float:
    """Read a counter's value through whatever path its label set requires.

    A labelled counter has no ``_value`` until ``labels()`` resolves one, so the
    helper has to know the difference — which is exactly the difference the old
    tests did not.
    """
    if labels:
        return float(counter.labels(**labels)._value.get())
    return float(counter._value.get())


def test_disabled_by_default_reports_disabled() -> None:
    status = setup_observability(Settings())
    assert status["otel"] == "disabled"
    assert status["prometheus"] == "disabled"


def test_enabled_features_report_enabled() -> None:
    settings = Settings(otel_enabled=True, metrics_prometheus=True)
    status = setup_observability(settings)
    assert status["otel"] == "enabled"
    assert status["prometheus"] == "enabled"


def test_bad_otlp_endpoint_does_not_raise() -> None:
    # The exporter is lazy over the network, but constructing against an
    # unusable endpoint must still not take the daemon down.
    settings = Settings(otel_enabled=True, otel_exporter_otlp_endpoint="not-a-url")
    status = setup_observability(settings)
    assert status["otel"].startswith(("enabled", "error:"))


def test_tracer_works_without_setup() -> None:
    with tracer().start_as_current_span("no-op-span"):
        pass  # no-op proxy: the call shape is the same either way


def test_counters_count() -> None:
    before = value_of(token_cost, session="sess_1")
    record_usage("sess_1", 250)
    assert value_of(token_cost, session="sess_1") == before + 250


def test_usage_is_labelled_by_session() -> None:
    """Item 214 is "token/cost **per session**". One running total cannot answer
    which session spent it, and the unused ``session_id`` parameter on
    ``record_usage`` was the only evidence the metric was never per-session."""
    before_a = value_of(token_cost, session="sess_a")
    before_b = value_of(token_cost, session="sess_b")

    record_usage("sess_a", 100)

    assert value_of(token_cost, session="sess_a") == before_a + 100
    assert value_of(token_cost, session="sess_b") == before_b


def test_cost_is_recorded_separately_from_tokens() -> None:
    """Cost is optional and independently reported; folding it into the token
    counter would make one number mean two units."""
    before_cost = value_of(cost_usd, session="sess_cost")
    before_tokens = value_of(token_cost, session="sess_cost")

    record_usage("sess_cost", 500, 0.25)

    assert value_of(cost_usd, session="sess_cost") == pytest.approx(before_cost + 0.25)
    assert value_of(token_cost, session="sess_cost") == before_tokens + 500


def test_record_usage_ignores_zero() -> None:
    before = value_of(token_cost, session="sess_1")
    record_usage("sess_1", 0)
    assert value_of(token_cost, session="sess_1") == before


def test_a_missing_session_is_named_not_blank() -> None:
    """An empty label is a series nobody queries, so the spend disappears from
    every dashboard while looking counted."""
    before = value_of(token_cost, session="unknown")
    record_usage("", 5)
    assert value_of(token_cost, session="unknown") == before + 5
    assert value_of(token_cost, session="") == 0


def test_every_recorder_increments_its_counter() -> None:
    """Each public recorder must reach its counter without raising.

    This is the regression for the two never-executed branches: a recorder that
    raises ``NameError`` (undefined counter) or ``ValueError`` (labels on an
    unlabelled counter) fails here instead of inside the crash handler, where it
    would replace a recoverable harness death with an unhandled exception.
    """
    before_tokens = value_of(token_cost, session="sess_1")
    record_usage("sess_1", 7)
    assert value_of(token_cost, session="sess_1") == before_tokens + 7

    before_replan = value_of(replan_total, trigger="silent_failure")
    record_replan("silent_failure")
    assert value_of(replan_total, trigger="silent_failure") == before_replan + 1

    for outcome in ("attempted", "succeeded", "exhausted"):
        before = value_of(retry_total, outcome=outcome)
        record_retry(outcome)
        assert value_of(retry_total, outcome=outcome) == before + 1

    before_injections = value_of(lesson_injections)
    record_lesson_injection(2)
    assert value_of(lesson_injections) == before_injections + 2

    before_hits = value_of(lesson_hits)
    record_lesson_hit()
    assert value_of(lesson_hits) == before_hits + 1

    before_outputs = value_of(lane_outputs, kind="tool-call")
    record_lane_output("tool-call")
    assert value_of(lane_outputs, kind="tool-call") == before_outputs + 1


def test_a_blank_label_does_not_create_an_empty_series() -> None:
    """A missing value must land under an explicit bucket, never under ``""``.

    An empty label is the metric equivalent of a silent failure: it is a series
    nobody queries, so the volume it holds disappears from every dashboard while
    looking like it was counted.
    """
    record_retry("")
    assert value_of(retry_total, outcome="succeeded") >= 1

    record_lane_output("")
    assert value_of(lane_outputs, kind="text") >= 1


def test_bus_message_recorders_are_labelled_by_family() -> None:
    before = value_of(bus_messages, family="a2a")
    record_bus_message("a2a")
    assert value_of(bus_messages, family="a2a") == before + 1

    before_replies = value_of(bus_replies)
    record_bus_reply()
    assert value_of(bus_replies) == before_replies + 1


class TestEventFamily:
    """The volume metric's label, derived rather than declared."""

    @pytest.mark.parametrize(
        ("event_type", "expected"),
        [
            ("a2a.task.updated", "a2a"),
            ("governance.policy_denied", "governance"),
            ("lane.output.text", "lane"),
            ("single", "single"),
        ],
    )
    def test_the_leading_segment_is_the_family(self, event_type: str, expected: str) -> None:
        assert event_family(event_type) == expected

    @pytest.mark.parametrize("event_type", ["", ".leading_dot"])
    def test_a_missing_family_is_named_not_blank(self, event_type: str) -> None:
        assert event_family(event_type) == "unknown"


class TestSpan:
    """Item 212: ``span()`` is the helper ``tracer()`` always claimed existed."""

    def test_the_body_runs_and_nothing_raises_with_tracing_off(self) -> None:
        ran = False
        with span("lane.start", **{"lane.name": "alice"}) as current:
            ran = True
        assert ran
        assert current is not None, "call sites must never have to guard for None"

    def test_the_yielded_span_accepts_every_call_when_not_recording(self) -> None:
        """The contract that makes ``with span(...) as s: s.set_attribute(...)``
        safe without a branch — a non-recording span accepts and discards."""
        with span("bus.publish") as current:
            current.set_attribute("bus.seq", 1)
            current.set_attribute("lane.name", "alice")

    def test_span_does_not_raise_when_the_feature_is_disabled(self) -> None:
        """The daemon's rule: observability degrades, it never takes a session down."""
        setup_observability(Settings(otel_enabled=False))
        with span("bus.publish"):
            pass

    def test_span_emits_to_the_module_tracer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The whole point of the fix: spans must actually reach a provider.

        The module's ``_tracer`` is patched rather than the OTel global, and that
        is forced by the API: ``trace.set_tracer_provider`` may only be called
        once per process, so a test that installs a second provider is silently
        ignored and would report "no spans" for a reason unrelated to ``span()``.
        Patching ``_tracer`` exercises the same handle ``_setup_otel`` sets, which
        is what production uses.
        """
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        from openburrow.daemon import observability

        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        monkeypatch.setattr(observability, "_tracer", provider.get_tracer("test"))

        with span("lane.start", **{"lane.name": "alice"}):
            pass

        spans = exporter.get_finished_spans()
        assert [s.name for s in spans] == ["lane.start"]
        attributes = spans[0].attributes
        assert attributes is not None
        assert attributes["lane.name"] == "alice"

    def test_an_unserialisable_attribute_does_not_break_the_span(self) -> None:
        """A bad attribute loses the attribute, not the session it was measuring."""
        with span("lane.start", **{"lane.payload": object()}):
            pass


class TestShutdown:
    """``shutdown_observability``'s docstring says "flush and release". It did neither."""

    def test_shutdown_flushes_the_provider(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A provider held at shutdown must be flushed, not just forgotten.

        Nulling the module globals released nothing: the batch processor keeps
        its own thread, so queued spans were dropped and that thread went on
        writing to a closed stream after the daemon reported a clean stop.
        """
        from openburrow.daemon import observability

        shutdowns: list[str] = []

        class _Provider:
            def shutdown(self) -> None:
                shutdowns.append("provider")

        monkeypatch.setattr(observability, "_provider", _Provider())
        observability.shutdown_observability()

        assert shutdowns == ["provider"]

    def test_shutdown_closes_the_metrics_server(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Leaving the socket open makes a stop/start cycle fail to rebind."""
        from openburrow.daemon import observability

        closed: list[str] = []

        class _Server:
            def shutdown(self) -> None:
                closed.append("server")

        monkeypatch.setattr(observability, "_prometheus_server", _Server())
        observability.shutdown_observability()

        assert closed == ["server"]

    def test_shutdown_survives_a_provider_that_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Shutdown runs on the way out of a session; it must not raise."""
        from openburrow.daemon import observability

        class _Angry:
            def shutdown(self) -> None:
                raise RuntimeError("collector went away mid-flush")

        monkeypatch.setattr(observability, "_provider", _Angry())
        observability.shutdown_observability()

    def test_shutdown_is_idempotent(self) -> None:
        shutdown_observability()
        shutdown_observability()
