"""Stage 20 chaos engine: deterministic, seeded, advisory fault injection."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from openburrow.daemon.chaos import ChaosEngine

pytestmark = [pytest.mark.unit]


def make_settings(**overrides: Any) -> SimpleNamespace:
    """A stand-in Settings. Typed loosely on purpose; see the ignores below."""
    base = {
        "chaos_enabled": True,
        "chaos_kill_lane_after_s": 0,
        "chaos_drop_relay_messages": 0.0,
        "chaos_malform_plan_output": False,
        "chaos_hang_approval": False,
        "chaos_seed": 1337,
        **overrides,
    }
    return SimpleNamespace(**base)


def make_engine(**overrides: Any) -> ChaosEngine:
    """Build the engine once per call site so the type-ignore lives in one place."""
    return ChaosEngine(make_settings(**overrides))  # type: ignore[arg-type]


def test_disabled_engine_injects_nothing() -> None:
    engine = make_engine(chaos_enabled=False)
    for _ in range(20):
        assert not engine.should_kill("lane-1")
        assert not engine.should_drop_message("lane-1")
        assert engine.malform_output("lane-1", "clean output") == "clean output"
        assert not engine.approval_should_hang("lane-1")
    assert engine.injected_count == 0


def test_kill_fires_once_at_the_planned_time() -> None:
    engine = make_engine(chaos_kill_lane_after_s=30)
    plan = engine.plan_for("lane-1")
    assert plan.kill_at_s is not None
    # The window has elapsed (forced rather than slept — a chaos test that
    # sleeps is a slow test).
    plan.kill_at_s = 0.0
    assert engine.should_kill("lane-1")
    assert not engine.should_kill("lane-1")  # one-shot
    assert engine.injected_count == 1
    assert engine.status()["recent"][0]["kind"] == "kill_lane"


def test_same_seed_same_plan() -> None:
    """Reproducibility is the whole point (the mock adapter's docstring agrees)."""
    engine_a = make_engine()
    engine_b = make_engine()
    for lane in ("lane-1", "lane-2", "lane-3"):
        assert engine_a.plan_for(lane).kill_at_s == engine_b.plan_for(lane).kill_at_s
        assert engine_a.plan_for(lane).malform == engine_b.plan_for(lane).malform


def test_targeting_leaves_healthy_lanes_untouched() -> None:
    """The healthy contrast lane is what makes a chaos run a test, not an outage."""
    engine = make_engine(chaos_kill_lane_after_s=30, chaos_malform_plan_output=True)
    engine.lane_ids = {"chaotic"}

    chaotic = engine.plan_for("chaotic")
    assert chaotic.kill_at_s is not None
    assert chaotic.malform is True

    bystander = engine.plan_for("bystander")
    assert bystander.kill_at_s is None
    assert bystander.malform is False
    assert engine.malform_output("bystander", "honest output") == "honest output"
    # And the targeted lane really does corrupt.
    assert "not valid json" in engine.malform_output("chaotic", "structured plan")


def test_message_dropping_is_probabilistic_but_bounded() -> None:
    engine = make_engine(chaos_drop_relay_messages=1.0)
    # p=1.0 drops everything, deterministically.
    assert all(engine.should_drop_message("lane-1") for _ in range(20))
    engine_none = make_engine(chaos_drop_relay_messages=0.0)
    assert not any(engine_none.should_drop_message("lane-1") for _ in range(20))


def test_scenario_lists_all_four_families() -> None:
    engine = make_engine(
        chaos_kill_lane_after_s=30,
        chaos_malform_plan_output=True,
        chaos_hang_approval=True,
        chaos_drop_relay_messages=0.25,
    )
    scenario = engine.run_scripted_scenario()
    assert len(scenario) == 4
    assert "t+30s" in scenario[0]
    assert "malform" in scenario[1]
    assert "hang" in scenario[2]
    assert "25%" in scenario[3]
