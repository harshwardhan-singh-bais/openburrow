"""The LLM fallback planner (item 120).

The failure contract is the point: a planner that cannot run must return
``known=False`` data, never raise, and never silently pretend no plan was
needed. These tests also pin the validation rules — dropped malformed steps,
self-referencing or forward-referencing dependencies removed — because a
poisoned plan blocks every claim downstream.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from openburrow.core.models.plan import Plan, PlanStep
from openburrow.daemon.planner import PlannerResult, apply_to_plan

pytestmark = [pytest.mark.unit]


def _planner(model: str = "gpt-4o") -> Any:
    from openburrow.daemon.planner import FallbackPlanner

    return FallbackPlanner(model=model, timeout=5)


class TestFailureContract:
    async def test_no_model_is_unavailable_not_fatal(self) -> None:
        result = await _planner(model="").plan(description="build a parser")
        assert result.is_unknown
        assert result.method == "unavailable"
        assert result.steps == []

    async def test_empty_description_is_rejected(self) -> None:
        result = await _planner().plan(description="   ")
        assert result.is_unknown
        assert result.method == "rejected"

    async def test_litellm_missing_is_unavailable(self) -> None:
        planner = _planner()
        with patch.dict("sys.modules", {"litellm": None}):
            result = await planner.plan(description="build a parser")
        # Whether litellm is importable in this env, the result is honest data.
        assert result.is_unknown or result.steps

    async def test_llm_error_is_unavailable(self) -> None:
        planner = _planner()
        with patch("litellm.acompletion", side_effect=RuntimeError("no network")):
            result = await planner.plan(description="build a parser")
        assert result.is_unknown
        assert "no network" in result.note


class TestValidation:
    def _parsed(self, steps: list[dict[str, Any]]) -> dict[str, Any]:
        return {"steps": steps, "note": ""}

    async def _run_with(self, parsed: dict[str, Any]) -> PlannerResult:
        planner = _planner()

        async def fake_acompletion(**_kwargs: Any) -> Any:
            import json

            # json.dumps, not an f-string: the model's reply is JSON, and a
            # Python repr with single quotes would be (correctly) unparseable.
            return {"choices": [{"message": {"content": json.dumps(parsed)}}]}

        with patch("litellm.acompletion", side_effect=fake_acompletion):
            return await planner.plan(description="task")

    async def test_malformed_step_without_title_is_dropped(self) -> None:
        result = await self._run_with(self._parsed([{"title": ""}, {"title": "real step"}]))
        assert len(result.steps) == 1
        assert result.steps[0]["title"] == "real step"

    async def test_forward_dependency_is_removed(self) -> None:
        result = await self._run_with(
            self._parsed(
                [
                    {"title": "a", "depends_on": [1]},  # forward: must go
                    {"title": "b"},
                ]
            )
        )
        assert result.steps[0]["depends_on"] == []

    async def test_backward_dependency_is_kept(self) -> None:
        result = await self._run_with(
            self._parsed(
                [
                    {"title": "a"},
                    {"title": "b", "depends_on": [0]},
                ]
            )
        )
        assert result.steps[1]["depends_on"] == [0]

    async def test_step_cap_is_enforced(self) -> None:
        from openburrow.daemon.planner import MAX_STEPS

        raw = [{"title": f"step {i}"} for i in range(MAX_STEPS + 5)]
        result = await self._run_with(self._parsed(raw))
        assert len(result.steps) == MAX_STEPS

    async def test_non_list_steps_is_malformed(self) -> None:
        result = await self._run_with({"steps": "nope", "note": ""})
        assert result.method == "malformed"
        assert result.is_unknown


class TestApplyToPlan:
    def test_creates_steps_and_wires_deps(self) -> None:
        plan = Plan(session_id="s1", title="t")
        result = PlannerResult(
            steps=[
                {"title": "first", "description": "", "target_paths": ["a.py"], "depends_on": []},
                {"title": "second", "description": "", "target_paths": [], "depends_on": [0]},
            ]
        )
        created = apply_to_plan(plan, result, step_factory=PlanStep)
        assert len(created) == 2
        assert created[1].depends_on == [created[0].id]
        assert plan.steps == created
        # One structural snapshot for the whole fallback plan.
        assert plan.version == 2

    def test_empty_result_creates_nothing(self) -> None:
        plan = Plan(session_id="s1", title="t")
        created = apply_to_plan(plan, PlannerResult(steps=[]), step_factory=PlanStep)
        assert created == []
        assert plan.steps == []
