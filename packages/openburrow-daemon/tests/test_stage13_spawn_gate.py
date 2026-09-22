"""The spawn gate: the call that turns the policy from a report into a gate.

Stage 13's fourth defect was structural rather than logical. The rules were
correct enough to be believed and reachable from exactly one place — the
``--dry-run`` subcommand — so ``policy.enforce`` was read by nothing and
``PolicyViolation``, whose docstring reads "The pre-execution policy gate blocked
an action", was exported from ``openburrow.core`` and never raised anywhere in the
project.

These tests pin the wiring, not the rules. The rules have their own file
(``openburrow-governance/tests/test_stage13_policy.py``); what matters here is
that a denied spawn plan stops a lane from starting, that an advisory denial does
not, and that the plan which is inspected is the same object that is executed.

``SessionManager.__init__`` only stores its arguments, so a manager can be built
with a real ``PolicyConfig`` and stubs for everything else. That is deliberate —
it means these tests exercise the real decision path without a database, a bus,
or a subprocess.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from openburrow.adapters import SpawnSpec
from openburrow.core.config.repo_config import PolicyConfig
from openburrow.core.errors import PolicyViolation
from openburrow.core.models import Lane, LaneRole, LaneStatus
from openburrow.daemon.sessions import SessionManager

pytestmark = [pytest.mark.unit, pytest.mark.governance]


class RecordingBus:
    """Captures emitted events so a test can assert what was published."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def emit(self, **payload: Any) -> None:
        self.events.append(payload)

    def event_types(self) -> list[str]:
        return [str(event.get("event_type")) for event in self.events]


def make_manager(
    policy: PolicyConfig,
    *,
    repo_root: Path,
    bus: RecordingBus | None = None,
) -> SessionManager:
    config = SimpleNamespace(
        policy=policy,
        paths=SimpleNamespace(repo_root=repo_root),
        settings=SimpleNamespace(governance_human_id="", governance_human_email=""),
    )
    return SessionManager(
        config=config,  # type: ignore[arg-type]
        database=None,  # type: ignore[arg-type]
        bus=bus or RecordingBus(),  # type: ignore[arg-type]
        registry=None,  # type: ignore[arg-type]
    )


def make_lane(*, session_id: str = "s-1") -> Lane:
    return Lane(
        session_id=session_id,
        name="alice",
        harness="claude-code",
        role=LaneRole.IMPLEMENTER,
        status=LaneStatus.STARTING,
    )


class TestDeniedSpawn:
    async def test_a_denied_plan_raises_before_the_lane_starts(self, tmp_path: Path) -> None:
        bus = RecordingBus()
        manager = make_manager(
            PolicyConfig(denied_commands=["--dangerously-skip-permissions"]),
            repo_root=tmp_path,
            bus=bus,
        )
        spec = SpawnSpec(command=["claude", "--dangerously-skip-permissions"], cwd=tmp_path, env={})
        with pytest.raises(PolicyViolation) as caught:
            await manager._gate_spawn(make_lane(), spec, role="implementer")
        assert "denied_commands" in str(caught.value)
        assert caught.value.context["matched_rule"] == (
            "denied_commands: --dangerously-skip-permissions"
        )

    async def test_a_denial_is_published_to_the_bus(self, tmp_path: Path) -> None:
        """A block that leaves no trace is a block nobody can audit."""
        bus = RecordingBus()
        manager = make_manager(
            PolicyConfig(denied_commands=["docker"]), repo_root=tmp_path, bus=bus
        )
        with pytest.raises(PolicyViolation):
            await manager._gate_spawn(
                make_lane(),
                SpawnSpec(command=["docker", "run", "x"], cwd=tmp_path, env={}),
                role="implementer",
            )
        assert bus.event_types() == ["governance.policy_denied"]
        payload = bus.events[0]["payload"]
        assert payload["enforced"] is True
        assert payload["action"] == "deny"

    async def test_an_advisory_denial_does_not_stop_the_lane(self, tmp_path: Path) -> None:
        """``enforce=false`` means the gate reports and gets out of the way.

        Reported as advisory rather than silently downgraded: the bus event and
        the log line both say so, which is the difference between a gate the
        operator chose to disable and a gate that quietly stopped working.
        """
        bus = RecordingBus()
        manager = make_manager(
            PolicyConfig(enforce=False, denied_commands=["docker"]),
            repo_root=tmp_path,
            bus=bus,
        )
        await manager._gate_spawn(
            make_lane(),
            SpawnSpec(command=["docker", "run", "x"], cwd=tmp_path, env={}),
            role="implementer",
        )
        assert bus.event_types() == ["governance.policy_denied"]
        assert bus.events[0]["payload"]["enforced"] is False

    async def test_an_allowed_low_risk_plan_is_silent(self, tmp_path: Path) -> None:
        """No event, no log, no cost on the common path."""
        bus = RecordingBus()
        manager = make_manager(PolicyConfig(), repo_root=tmp_path, bus=bus)
        await manager._gate_spawn(
            make_lane(),
            SpawnSpec(command=["pytest", "-x"], cwd=tmp_path, env={}),
            role="implementer",
        )
        assert bus.events == []


class TestApprovalTier:
    async def test_a_high_risk_spawn_is_recorded(self, tmp_path: Path) -> None:
        """Recorded, not paused — and the test says which.

        Pausing a lane start needs the approvals store wired into the daemon, and
        the CLI's ``approvals.*`` methods are not registered on the IPC surface
        yet. Publishing the event is what keeps the gap visible instead of letting
        ``would_require_approval`` imply a pause that does not happen.
        """
        bus = RecordingBus()
        manager = make_manager(PolicyConfig(), repo_root=tmp_path, bus=bus)
        await manager._gate_spawn(
            make_lane(),
            SpawnSpec(command=["git", "push", "origin", "main"], cwd=tmp_path, env={}),
            role="implementer",
        )
        assert bus.event_types() == ["governance.approval_required"]
        assert bus.events[0]["payload"]["risk_tier"] == "high"

    async def test_a_denied_plan_does_not_also_claim_it_would_pause(self, tmp_path: Path) -> None:
        """A denied command does not pause for approval; it does not run."""
        bus = RecordingBus()
        manager = make_manager(
            PolicyConfig(denied_commands=["git push --force"]), repo_root=tmp_path, bus=bus
        )
        with pytest.raises(PolicyViolation):
            await manager._gate_spawn(
                make_lane(),
                SpawnSpec(command=["git", "push", "--force"], cwd=tmp_path, env={}),
                role="implementer",
            )
        assert bus.event_types() == ["governance.policy_denied"]
        assert bus.events[0]["payload"]["would_require_approval"] is False


class TestTheGateIsWiredIntoTheRealPath:
    async def test_the_gate_reads_the_merged_policy_not_the_raw_yaml(self, tmp_path: Path) -> None:
        """``repo_root`` matters: policy paths are relative, cwd is absolute.

        Without it, ``allowed_paths: ["."]`` denies every lane start — the rule
        means "keep it in the repository" and would have read as "keep it
        nowhere", which is the kind of thing that gets a security control
        switched off on the first day.
        """
        manager = make_manager(PolicyConfig(allowed_paths=["."]), repo_root=tmp_path)
        worktree = tmp_path / ".openburrow" / "worktrees" / "lane-1"
        worktree.mkdir(parents=True)
        await manager._gate_spawn(
            make_lane(), SpawnSpec(command=["pytest"], cwd=worktree, env={}), role="implementer"
        )

    async def test_a_worktree_outside_the_repo_is_denied(self, tmp_path: Path) -> None:
        manager = make_manager(PolicyConfig(allowed_paths=["."]), repo_root=tmp_path / "repo")
        (tmp_path / "repo").mkdir()
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        with pytest.raises(PolicyViolation):
            await manager._gate_spawn(
                make_lane(), SpawnSpec(command=["pytest"], cwd=outside, env={}), role="implementer"
            )

    def test_the_gate_is_built_once_and_cached(self, tmp_path: Path) -> None:
        """Built lazily so the policy is read at a known moment.

        A per-lane construction would re-read the config on every start, so a
        mid-session edit to ``openburrow.yaml`` would apply to some lanes and not
        others — the worst of both, because it looks like it worked.
        """
        manager = make_manager(PolicyConfig(), repo_root=tmp_path)
        assert manager.policy_gate is manager.policy_gate

    async def test_the_role_is_passed_through(self, tmp_path: Path) -> None:
        """A reviewer lane is stricter, and the gate is where that happens."""
        manager = make_manager(
            PolicyConfig(
                allowed_commands=["pytest"],
                role_overrides={"reviewer": {"denied_commands": ["pytest"]}},
            ),
            repo_root=tmp_path,
        )
        await manager._gate_spawn(
            make_lane(), SpawnSpec(command=["pytest"], cwd=tmp_path, env={}), role="implementer"
        )
        with pytest.raises(PolicyViolation):
            await manager._gate_spawn(
                make_lane(), SpawnSpec(command=["pytest"], cwd=tmp_path, env={}), role="reviewer"
            )


class TestTheCallSite:
    """The wiring, asserted at the source level because that is the claim.

    ``_gate_spawn`` existing and being correct is not the same as ``start_lane``
    calling it — and the defect this stage removes was exactly that gap: correct
    rules, reachable from one place, consulted by nothing. A behavioural test of
    ``_gate_spawn`` passes with the call site deleted.
    """

    def test_the_plan_gated_is_the_plan_spawned(self) -> None:
        from openburrow.daemon import sessions as daemon_sessions

        source = Path(daemon_sessions.__file__).read_text(encoding="utf-8")
        # Built once, gated, then handed to `start` — not rebuilt inside it.
        assert "spec = adapter.prepare_spawn_spec(lane)" in source
        assert "await self._gate_spawn(lane, spec, role=role)" in source
        assert "await adapter.start(lane, spec=spec)" in source
        # And the gate runs before the spawn, not after.
        assert source.index("await self._gate_spawn(lane, spec, role=role)") < source.index(
            "await adapter.start(lane, spec=spec)"
        )
