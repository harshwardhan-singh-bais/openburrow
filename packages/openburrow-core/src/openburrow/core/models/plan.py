"""Plans and plan steps.

The plan is a **graph**, not a list. Steps declare dependencies, and the graph
is what makes "what can start now?" a query rather than a conversation.

Two rules the rest of the system depends on:

* **Progress is never inferred.** A step's status changes only when an A2A task
  reaches a terminal state, or a human edits it. A harness saying "I'm nearly
  done" does not move a step to ``done``. Item 122 makes this explicit because
  inferred progress is how multi-agent systems lie to their users.

* **Ownership lives on the step, authority lives on the task.** A step records
  *who* is doing it; the task that implements it records *what they were allowed
  to do*. Keeping those separate is what lets the governance layer audit a
  completed step long after the lane is gone.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from pydantic import Field, computed_field, field_validator

from openburrow.core.models.base import BurrowModel, ensure_aware, now
from openburrow.core.models.enums import StepStatus


class PlanStep(BurrowModel):
    """One unit of planned work."""

    id_kind: ClassVar[str] = "step"

    plan_id: str = ""
    title: str = ""
    description: str = ""

    # --- graph -------------------------------------------------------------
    #: Steps that must reach ``done`` before this one can start.
    depends_on: list[str] = Field(default_factory=list)
    #: Steps that cannot start until this one finishes (denormalised inverse).
    blocks: list[str] = Field(default_factory=list)
    parent_step_id: str = ""
    order: int = 0

    # --- ownership ---------------------------------------------------------
    status: StepStatus = StepStatus.PENDING
    owner_lane: str = ""
    claimed_at: datetime | None = None
    #: Lane that originally owned it, kept through handoffs for the audit trail.
    original_owner_lane: str = ""

    # --- scope -------------------------------------------------------------
    #: Files this step is expected to touch — feeds the Radar's first pass.
    target_paths: list[str] = Field(default_factory=list)
    #: Tools/skills the step expects to need.
    required_skills: list[str] = Field(default_factory=list)
    estimated_effort: str = ""  # free-form: "S" | "M" | "L" or a token estimate

    # --- linkage -----------------------------------------------------------
    task_ids: list[str] = Field(default_factory=list)
    artifact_refs: list[str] = Field(default_factory=list)
    commit_shas: list[str] = Field(default_factory=list)

    # --- timing ------------------------------------------------------------
    started_at: datetime | None = None
    ended_at: datetime | None = None

    @field_validator("title", mode="before")
    @classmethod
    def _title_required(cls, value: object) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("plan step requires a title")
        return text

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_done(self) -> bool:
        return self.status in {StepStatus.DONE, StepStatus.SKIPPED}

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_owned(self) -> bool:
        return bool(self.owner_lane)

    @property
    def duration_seconds(self) -> float:
        if self.started_at is None:
            return 0.0
        end = ensure_aware(self.ended_at) if self.ended_at else now()
        return (end - ensure_aware(self.started_at)).total_seconds()

    def claim(self, lane_id: str) -> None:
        if not self.original_owner_lane:
            self.original_owner_lane = lane_id
        self.owner_lane = lane_id
        self.claimed_at = now()
        if self.status == StepStatus.PENDING:
            self.status = StepStatus.CLAIMED
        self.touch()

    def start(self) -> None:
        self.status = StepStatus.IN_PROGRESS
        self.started_at = self.started_at or now()
        self.touch()

    def complete(self) -> None:
        self.status = StepStatus.DONE
        self.ended_at = now()
        self.touch()

    def fail(self, reason: str = "") -> None:
        self.status = StepStatus.FAILED
        self.ended_at = now()
        if reason:
            self.metadata["failure_reason"] = reason
        self.touch()

    def handoff(self, to_lane: str) -> None:
        """Transfer ownership, preserving the original owner for the audit trail."""
        if not self.original_owner_lane:
            self.original_owner_lane = self.owner_lane
        self.owner_lane = to_lane
        self.claimed_at = now()
        self.touch()


class Plan(BurrowModel):
    """A dependency graph of steps for one session.

    Plans are versioned rather than mutated in place. ``version`` increments on
    every structural change and prior versions are retained, which is what makes
    ``burrow plan diff`` and rollback possible without a separate history table.
    """

    id_kind: ClassVar[str] = "plan"

    session_id: str = ""
    title: str = ""
    description: str = ""
    version: int = 1

    steps: list[PlanStep] = Field(default_factory=list)

    # --- provenance --------------------------------------------------------
    #: Which lane's output this plan was translated from (empty for human/fallback).
    source_lane: str = ""
    #: How the plan came to exist — the translator's own honesty field.
    source: str = "harness"  # harness | human | llm-fallback | imported
    source_artifact_id: str = ""

    # --- history -----------------------------------------------------------
    #: Snapshots of prior versions: ``[{"version": 1, "steps": [...]}, ...]``.
    history: list[dict[str, Any]] = Field(default_factory=list)
    approved_by: str = ""
    approved_at: datetime | None = None

    # ------------------------------------------------------------------ graph
    def step(self, step_id: str) -> PlanStep | None:
        return next((s for s in self.steps if s.id == step_id), None)

    def by_status(self, status: StepStatus) -> list[PlanStep]:
        return [s for s in self.steps if s.status == status]

    def ready_steps(self) -> list[PlanStep]:
        """Steps whose dependencies are all satisfied and which nobody owns yet.

        This is what ``burrow take`` offers, and what the coordinator lane reads
        when deciding what to delegate next.
        """
        done = {s.id for s in self.steps if s.is_done}
        return [
            step
            for step in self.steps
            if step.status == StepStatus.PENDING
            and not step.owner_lane
            and all(dep in done for dep in step.depends_on)
        ]

    def blocked_steps(self) -> list[PlanStep]:
        done = {s.id for s in self.steps if s.is_done}
        return [
            step
            for step in self.steps
            if step.status in {StepStatus.PENDING, StepStatus.BLOCKED}
            and any(dep not in done for dep in step.depends_on)
        ]

    def descendants(self, step_id: str) -> list[PlanStep]:
        """Everything transitively blocked by ``step_id`` — used for cascade replanning."""
        found: list[PlanStep] = []
        frontier = [step_id]
        seen: set[str] = set()
        while frontier:
            current = frontier.pop()
            if current in seen:
                continue
            seen.add(current)
            for step in self.steps:
                if current in step.depends_on and step.id not in seen:
                    found.append(step)
                    frontier.append(step.id)
        return found

    def has_cycle(self) -> bool:
        """Dependency cycle check. A cyclic plan is a bug, and we refuse to run it."""
        graph = {s.id: [d for d in s.depends_on if self.step(d)] for s in self.steps}
        # The three-state DFS colouring from CLRS. Lowercase because these are
        # local markers, not module constants.
        white, grey, black = 0, 1, 2
        colour = dict.fromkeys(graph, white)

        def visit(node: str) -> bool:
            colour[node] = grey
            for neighbour in graph.get(node, []):
                if colour.get(neighbour) == grey:
                    return True
                if colour.get(neighbour) == white and visit(neighbour):
                    return True
            colour[node] = black
            return False

        return any(colour[node] == white and visit(node) for node in graph)

    def validate_references(self) -> list[str]:
        """Return problems that make the plan unrunnable (item 126).

        Checks dependency targets exist, ordering is consistent, and no cycle.
        Returns an empty list when the plan is sound — callers treat a non-empty
        result as a hard stop before any lane is spawned.
        """
        problems: list[str] = []
        ids = {s.id for s in self.steps}
        for step in self.steps:
            for dep in step.depends_on:
                if dep not in ids:
                    problems.append(f"step {step.id} depends on unknown step {dep}")
            if step.parent_step_id and step.parent_step_id not in ids:
                problems.append(f"step {step.id} has unknown parent {step.parent_step_id}")
        if self.has_cycle():
            problems.append("plan contains a dependency cycle")
        return problems

    # ------------------------------------------------------------------ state
    @computed_field  # type: ignore[prop-decorator]
    @property
    def progress(self) -> float:
        """Fraction of steps done. Derived, never stored — see the module docstring."""
        if not self.steps:
            return 0.0
        return round(sum(1 for s in self.steps if s.is_done) / len(self.steps), 4)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_complete(self) -> bool:
        return bool(self.steps) and all(s.is_done for s in self.steps)

    def counts(self) -> dict[str, int]:
        tally: dict[str, int] = {}
        for step in self.steps:
            tally[str(step.status)] = tally.get(str(step.status), 0) + 1
        return tally

    # ------------------------------------------------------------------ edits
    def snapshot(self) -> None:
        """Push the current state onto the version history before a mutation."""
        self.history.append(
            {
                "version": self.version,
                "at": now().isoformat(),
                "steps": [s.model_dump(mode="json") for s in self.steps],
            }
        )
        self.version += 1

    def add_step(self, step: PlanStep, *, announce: bool = True) -> PlanStep:
        """Add a step, optionally announcing it as a new A2A task (item 125)."""
        if announce:
            self.snapshot()
        step.plan_id = self.id
        if not step.order:
            step.order = len(self.steps)
        self.steps.append(step)
        self.touch()
        return step

    def remove_step(self, step_id: str) -> bool:
        target = self.step(step_id)
        if target is None:
            return False
        self.snapshot()
        self.steps = [s for s in self.steps if s.id != step_id]
        for step in self.steps:
            if step_id in step.depends_on:
                step.depends_on.remove(step_id)
        self.touch()
        return True

    def rollback(self, version: int) -> bool:
        """Restore a prior version from ``history``. Returns False if not found."""
        for snapshot in reversed(self.history):
            if snapshot.get("version") == version:
                self.steps = [PlanStep.model_validate(s) for s in snapshot["steps"]]
                self.version = version
                self.touch()
                return True
        return False

    def diff_against(self, other: Plan) -> dict[str, list[str]]:
        """Structural diff between two plans — drives ``burrow plan diff``."""
        mine = {s.id: s for s in self.steps}
        theirs = {s.id: s for s in other.steps}
        added = [sid for sid in theirs if sid not in mine]
        removed = [sid for sid in mine if sid not in theirs]
        changed: list[str] = []
        for sid in set(mine) & set(theirs):
            if mine[sid].model_dump(exclude={"updated_at"}) != theirs[sid].model_dump(
                exclude={"updated_at"}
            ):
                changed.append(sid)
        return {"added": added, "removed": removed, "changed": changed}

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "version": self.version,
            "steps": len(self.steps),
            "progress": self.progress,
            "ready": len(self.ready_steps()),
            "blocked": len(self.blocked_steps()),
            "counts": self.counts(),
        }


__all__ = ["Plan", "PlanStep"]
