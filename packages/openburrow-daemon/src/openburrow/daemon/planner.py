"""The LLM fallback planner (Stage 8, item 120).

When a harness produces no plan of its own, the session still needs a plan —
claims, the Radar, and handoffs all read from it. This module asks the
configured model to decompose the session's task description into steps.

The failure contract follows the Radar judge's (``radar/judge.py``): a planner
that cannot run is **data**, not an error. The daemon calls this on lane start
and must never fail a lane because the model is down. Every failure returns
``PlannerResult(known=False, reason=...)`` and the session simply continues
without an LLM plan — the same "report coverage honestly" discipline the judge
uses, so the metrics cannot later claim plans the planner never produced.

The model's steps are constrained to a closed shape (title, files, deps) and
dependency references are validated against the generated set before anything
is persisted — a model hallucinating ``depends_on: [step-9]`` into a four-step
plan would otherwise poison ``validate_references`` and block every claim.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from openburrow.core.logging import get_logger

if TYPE_CHECKING:
    from openburrow.core.models.plan import Plan, PlanStep

log = get_logger(__name__)

#: Upper bound on generated steps. A model asked to plan a small task that
#: returns forty steps is malfunctioning, and forty steps each announced as an
#: A2A task would flood the bus.
MAX_STEPS = 12


_PROMPT = """You decompose a coding task into a plan for a team of coding agents.

Given the task description and the files involved, produce between 2 and %d
steps. Each step must be independently claimable by one agent: a reader of the
step title and target files should be able to start work without asking
questions.

Rules:
- Steps must be ordered so dependencies reference only EARLIER steps.
- target_paths: the files this step will create or modify, repo-relative.
- Do not invent file paths that were not mentioned or strongly implied.
- If the task is too vague to decompose, return steps: [] and say why in "note".

Reply with JSON only, no prose:
{"steps": [{"title": "...", "description": "...", "target_paths": ["..."],
            "depends_on": [0-based indices of earlier steps]}],
 "note": ""}

TASK
%s
"""


@dataclass(frozen=True, slots=True)
class PlannerResult:
    """What the planner produced, or honestly, why it produced nothing."""

    steps: list[dict] = field(default_factory=list)
    note: str = ""
    #: "llm" | "unavailable" | "malformed" | "rejected"
    method: str = "llm"
    known: bool = True

    @property
    def is_unknown(self) -> bool:
        return not self.known


class FallbackPlanner:
    """Asks the configured model for a plan when the harness gave none."""

    def __init__(self, *, model: str, timeout: float = 60.0, max_tokens: int = 2048) -> None:
        self.model = model
        self.timeout = timeout
        self.max_tokens = max_tokens

    @property
    def available(self) -> bool:
        return bool(self.model)

    async def plan(
        self, *, description: str, existing_steps: list[str] | None = None
    ) -> PlannerResult:
        """Decompose ``description``. Never raises."""
        if not self.model:
            return PlannerResult(
                method="unavailable", known=False, note="no planner model configured"
            )
        if not description.strip():
            return PlannerResult(
                method="rejected", known=False, note="no task description to plan from"
            )

        try:
            import litellm
        except ImportError:
            return PlannerResult(method="unavailable", known=False, note="litellm is not installed")

        prompt = _PROMPT % (MAX_STEPS, description[:4000])
        try:
            response = await litellm.acompletion(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                timeout=self.timeout,
                max_tokens=self.max_tokens,
            )
            raw = response["choices"][0]["message"]["content"]
        except Exception as exc:
            log.warning("planner.llm_failed", error=str(exc), model=self.model)
            return PlannerResult(
                method="unavailable", known=False, note=f"{type(exc).__name__}: {exc}"
            )

        parsed = _parse_json_object(raw)
        if parsed is None:
            log.warning("planner.malformed", model=self.model, raw=raw[:200])
            return PlannerResult(
                method="malformed", known=False, note="planner returned unparseable output"
            )

        return _result_from(parsed)


def _result_from(parsed: dict) -> PlannerResult:
    """Validate model output into a result, dropping malformed entries."""
    raw_steps = parsed.get("steps")
    if not isinstance(raw_steps, list):
        return PlannerResult(method="malformed", known=False, note="steps must be a list")
    if len(raw_steps) > MAX_STEPS:
        raw_steps = raw_steps[:MAX_STEPS]

    steps: list[dict] = []
    for index, raw in enumerate(raw_steps):
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or "").strip()
        if not title:
            continue
        paths = [str(p) for p in (raw.get("target_paths") or []) if str(p).strip()][:10]
        # Deps are validated after the loop, once every index is a real step.
        deps = raw.get("depends_on") or []
        steps.append(
            {
                "title": title[:200],
                "description": str(raw.get("description") or "")[:500],
                "target_paths": paths,
                "depends_on": [
                    int(d) for d in deps if isinstance(d, (int, str)) and str(d).isdigit()
                ],
                "_index": index,
            }
        )

    valid_indices = {step["_index"] for step in steps}
    for step in steps:
        step["depends_on"] = [
            d for d in step["depends_on"] if d in valid_indices and d < step["_index"]
        ]
        step.pop("_index")

    return PlannerResult(steps=steps, note=str(parsed.get("note") or "")[:300])


def apply_to_plan(plan: Plan, result: PlannerResult, *, step_factory: type) -> list[PlanStep]:
    """Append the planner's steps to an existing plan in model order.

    Returns the created steps. The plan snapshots once — a fallback plan is one
    structural change, not N of them.
    """
    created: list[PlanStep] = []
    for spec in result.steps:
        step = step_factory(
            title=spec["title"],
            description=spec["description"],
            target_paths=list(spec["target_paths"]),
        )
        plan.add_step(step, announce=False)
        created.append(step)
    # Second pass: indices are positions in the result, so deps resolve after
    # everything exists.
    for step, spec in zip(created, result.steps, strict=False):
        for dep_index in spec["depends_on"]:
            if 0 <= dep_index < len(created):
                step.depends_on.append(created[dep_index].id)
    if created:
        plan.snapshot()
    return created


def _parse_json_object(raw: str) -> dict | None:
    """Extract the first JSON object, tolerating fences and prose."""
    text = (raw or "").strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else text
        text = text.removeprefix("json").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


__all__ = ["MAX_STEPS", "FallbackPlanner", "PlannerResult", "apply_to_plan"]
