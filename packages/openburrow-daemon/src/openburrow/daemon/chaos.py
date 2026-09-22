"""Chaos / fault injection (Stage 20).

The chaos settings existed before this module did, which is the failure mode
this file exists to close: configuration that promises fault injection and
nothing that reads it. Every switch in ``OPENBURROW_CHAOS_*`` now has exactly
one consumer here.

The point of chaos is reproducibility, which is why the module is **seeded and
deterministic**: with the same seed and the same script, the same faults happen
in the same order. A chaos test that fails once in five runs is not a chaos
test, it is a flake generator — the mock adapter's docstring says the same
thing, and :class:`ChaosEngine` follows it.

The faults map one-to-one onto the recovery paths they exercise:

* ``chaos_kill_lane_after_s``  → crash restart, checkpoints, resume (Stage 12)
* ``chaos_hang_approval``      → approval timeout policy (Stage 13)
* ``chaos_malform_plan_output``→ fallback parsing, dead-letter (Stage 12/15)
* ``chaos_drop_relay_messages``→ relay reconnect + state reconciliation (Stage 4/18)

Guardrails, and they are load-bearing: chaos can never be enabled in production
(``Settings._guard_production_invariants`` already raises), and the engine only
injects into lanes it was told to — a blanket fault would make the *recovery*
untestable because nothing healthy would be left to recover.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from openburrow.core.logging import get_logger
from openburrow.core.models.base import now

if TYPE_CHECKING:
    from openburrow.core.config.settings import Settings

log = get_logger(__name__)


@dataclass(slots=True)
class ChaosEvent:
    """One injected fault, on the record."""

    kind: str
    lane_id: str
    at: str
    detail: str = ""


@dataclass(slots=True)
class _LanePlan:
    """Per-lane fault plan, derived from settings and the seed."""

    kill_at_s: float | None = None
    malform: bool = False
    hang_approvals: bool = False
    drop_probability: float = 0.0


class ChaosEngine:
    """Injects deterministic faults into running lanes.

    Attached to the daemon when ``chaos_enabled`` is true. The supervisor and
    the relay-facing paths consult it at their natural injection points; the
    engine itself never touches a process — it *advises*, and the caller
    (which owns the process) acts. Keeping the engine advisory is what makes
    it testable without killing anything.
    """

    def __init__(self, settings: Settings, *, lane_ids: set[str] | None = None) -> None:
        self.settings = settings
        self.enabled = bool(settings.chaos_enabled)
        # Not cryptographic: the seed exists so a chaos run is reproducible.
        # S311 is silenced with the reason stated rather than with a bare noqa.
        self._rng = random.Random(settings.chaos_seed)  # noqa: S311
        self.events: list[ChaosEvent] = []
        #: Lanes chaos applies to. Empty means every lane — but only when chaos
        #: is explicitly enabled, which production startup already refuses.
        self.lane_ids: set[str] = lane_ids or set()
        self._plans: dict[str, _LanePlan] = {}
        self._started_at = now().timestamp()

    # --- planning ----------------------------------------------------------
    def plan_for(self, lane_id: str) -> _LanePlan:
        """This lane's fault plan, derived once and then stable.

        Derived per lane rather than globally so a scripted scenario can run
        two lanes — one chaotic, one healthy — and assert the healthy one was
        untouched. That contrast *is* the test.
        """
        if not self.enabled:
            return _LanePlan()
        if lane_id in self._plans:
            return self._plans[lane_id]
        if self.lane_ids and lane_id not in self.lane_ids:
            return _LanePlan()

        plan = _LanePlan()
        if kill_after := int(self.settings.chaos_kill_lane_after_s or 0):
            # Relative to when the lane was first seen, not when the daemon
            # armed the engine — a lane that starts later gets its full window.
            # Jitter staggers lanes so a chaos run never kills everything in the
            # same instant: that tests a total outage, not recovery.
            jitter = self._rng.uniform(0, min(10.0, kill_after * 0.25)) if kill_after else 0.0
            plan.kill_at_s = now().timestamp() + kill_after + jitter
        plan.malform = bool(self.settings.chaos_malform_plan_output) and self._rng.random() < 0.8
        plan.hang_approvals = bool(self.settings.chaos_hang_approval)
        plan.drop_probability = min(1.0, max(0.0, float(self.settings.chaos_drop_relay_messages)))

        self._plans[lane_id] = plan
        return plan

    # --- injection points (all advisory) -----------------------------------
    def should_kill(self, lane_id: str) -> bool:
        """Whether the supervisor should treat this lane as crashed right now."""
        plan = self.plan_for(lane_id)
        if plan.kill_at_s is None:
            return False
        if now().timestamp() >= plan.kill_at_s:
            plan.kill_at_s = None  # fire once — a fault that repeats is noise
            self._record("kill_lane", lane_id, f"at t+{self.settings.chaos_kill_lane_after_s}s")
            return True
        return False

    def malform_output(self, lane_id: str, text: str) -> str:
        """Corrupt a lane's structured output to exercise the fallback parser."""
        plan = self.plan_for(lane_id)
        if not plan.malform or not text:
            return text
        self._record("malform_output", lane_id, f"{len(text)} bytes corrupted")
        return "{not valid json, and not a diff either" if plan.malform else text

    def should_drop_message(self, lane_id: str) -> bool:
        """Whether an outbound A2A message should be silently swallowed."""
        plan = self.plan_for(lane_id)
        if plan.drop_probability <= 0.0:
            return False
        drop = self._rng.random() < plan.drop_probability
        if drop:
            self._record("drop_message", lane_id, f"p={plan.drop_probability}")
        return drop

    def approval_should_hang(self, lane_id: str) -> bool:
        plan = self.plan_for(lane_id)
        return plan.hang_approvals

    # --- scenario ----------------------------------------------------------
    def run_scripted_scenario(self) -> list[str]:
        """The four fault families in one plan (item 253).

        Returns the *description* of the scenario; the faults themselves fire
        through the injection points above as the session runs. This is what
        `chaos.status` reports so an operator knows what a daemon under chaos
        will actually do.
        """
        return [
            f"kill lane harness t+{self.settings.chaos_kill_lane_after_s}s"
            if self.settings.chaos_kill_lane_after_s
            else "no kill planned",
            "malform adapter plan output"
            if self.settings.chaos_malform_plan_output
            else "no output corruption",
            "hang approvals" if self.settings.chaos_hang_approval else "approvals flow normally",
            f"drop {self.settings.chaos_drop_relay_messages:.0%} of relay messages"
            if self.settings.chaos_drop_relay_messages
            else "no relay drops",
        ]

    # --- bookkeeping -------------------------------------------------------
    def _record(self, kind: str, lane_id: str, detail: str) -> None:
        event = ChaosEvent(kind=kind, lane_id=lane_id, at=now().isoformat(), detail=detail)
        self.events.append(event)
        log.warning("chaos.injected", kind=kind, lane_id=lane_id, detail=detail)

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "seed": self.settings.chaos_seed,
            "targeted_lanes": sorted(self.lane_ids) or "all",
            "scenario": self.run_scripted_scenario() if self.enabled else [],
            "events_injected": len(self.events),
            "recent": [
                {"kind": e.kind, "lane": e.lane_id, "at": e.at, "detail": e.detail}
                for e in self.events[-10:]
            ],
        }

    @property
    def injected_count(self) -> int:
        return len(self.events)


__all__ = ["ChaosEngine", "ChaosEvent"]
