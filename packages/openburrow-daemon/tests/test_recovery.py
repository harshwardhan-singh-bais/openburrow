"""Stage 12 recovery engine tests.

The scaffolding existed; the engine is what was missing. These tests drive the
real decision paths — checkpoint take/resume round trip, checksum verification,
orphan requeue, retry → dead-letter, and the circuit breaker — against a real
SQLite database, with stub lanes where a live harness would be.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from openburrow.core.config.repo_config import PolicyConfig
from openburrow.core.db.engine import Database, init_database
from openburrow.core.db.repository import Repository
from openburrow.core.models import Lane, LaneStatus, Session
from openburrow.core.models.base import now
from openburrow.daemon.recovery import (
    Bulkhead,
    CircuitBreaker,
    RecoveryManager,
    checkpoint_checksum,
)

pytestmark = [pytest.mark.unit]


class RecordingBus:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def emit(self, **payload: Any) -> None:
        self.events.append(payload)


class StubAdapter:
    """Just enough adapter for checkpointing: liveness and a buffer."""

    def __init__(self, *, running: bool = True) -> None:
        self._running = running
        self.prompts: list[str] = []

    @property
    def is_running(self) -> bool:
        return self._running

    def recent_output(self, limit: int = 50) -> list:
        return []


def make_settings(**overrides: Any) -> SimpleNamespace:
    base = {
        "recovery_auto_resume": False,
        "recovery_checkpoint_interval_s": 300,
        "recovery_dead_letter_after": 3,
        "recovery_silent_failure": True,
        "recovery_crash_log_keep": 5,
        "recovery_circuit_threshold": 5,
        "recovery_circuit_cooldown_s": 300,
        "recovery_bulkhead_queue": 16,
        **overrides,
    }
    return SimpleNamespace(**base)


def make_manager(database: Database, *, settings: Any | None = None) -> RecoveryManager:
    config = SimpleNamespace(
        settings=settings or make_settings(),
        policy=PolicyConfig(),
        paths=SimpleNamespace(repo_root="."),
    )
    bus = RecordingBus()
    return RecoveryManager(
        config,  # type: ignore[arg-type]
        database,
        bus,  # type: ignore[arg-type]
        sessions=None,  # type: ignore[arg-type]
    )


async def test_checkpoint_round_trip(tmp_path: Any) -> None:
    db = await init_database(f"sqlite+aiosqlite:///{(tmp_path / 'a.db').as_posix()}")
    manager = make_manager(db)
    lane = Lane(session_id="s1", name="alice", harness="mock")
    lane.status = LaneStatus.WORKING
    adapter = StubAdapter()
    running = SimpleNamespace(lane=lane, adapter=adapter)

    checkpoint = await manager.take_checkpoint(running, reason="test")  # type: ignore[arg-type]
    assert checkpoint is not None
    assert checkpoint.sequence == 1
    assert checkpoint.lane_id == lane.id
    assert checkpoint.last_event_id  # the bus position was recorded

    # The second checkpoint increments the sequence.
    second = await manager.take_checkpoint(running, reason="test")  # type: ignore[arg-type]
    assert second is not None and second.sequence == 2
    await db.close()


async def test_resume_conumes_checkpoints_idempotently(tmp_path: Any, monkeypatch: Any) -> None:
    db = await init_database(f"sqlite+aiosqlite:///{(tmp_path / 'b.db').as_posix()}")
    manager = make_manager(db)

    # A checkpoint that carries no harness state verifies and is consumed,
    # but the lane comes back "fresh" — stated, not papered over.
    from openburrow.core.models import Checkpoint

    checkpoint = Checkpoint(session_id="s1", lane_id="lane-1", sequence=1)
    checkpoint.checksum = checkpoint_checksum(checkpoint)

    from openburrow.core.db.repository import Repository

    async with db.session() as db_session:
        await Repository(db_session).save(checkpoint)
        await db_session.commit()

    class FakeSessions:
        async def get_session(self, reference: str) -> Session:
            session = Session(name="s", owner="o")
            session.id = reference  # the checkpoint is keyed to "s1"
            return session

        async def get_lane(self, session_id: str, reference: str) -> Lane:
            return Lane(session_id=session_id, name="alice", harness="mock")

        async def start_lane(self, *args: Any, **kwargs: Any) -> Lane:
            return Lane(session_id="s1", name="alice", harness="mock")

    manager.sessions = FakeSessions()  # type: ignore[assignment]
    result = await manager.resume_session("s1")

    assert result["resumed"] == []
    assert len(result["fresh"]) == 1
    assert "no harness state" in result["fresh"][0]["reason"]

    # Second resume: nothing left to consume (idempotency, item 160).
    result2 = await manager.resume_session("s1")
    assert result2["resumed"] == [] and result2["fresh"] == []
    assert "no unconsumed checkpoints" in result2["message"]
    await db.close()


async def test_corrupt_checkpoint_is_skipped(tmp_path: Any) -> None:
    db = await init_database(f"sqlite+aiosqlite:///{(tmp_path / 'c.db').as_posix()}")
    manager = make_manager(db)

    from openburrow.core.models import Checkpoint

    bad = Checkpoint(session_id="s1", lane_id="lane-1", sequence=1, checksum="deadbeef")

    from openburrow.core.db.repository import Repository

    async with db.session() as db_session:
        await Repository(db_session).save(bad)
        await db_session.commit()

    class FakeSessions:
        async def get_session(self, reference: str) -> Session:
            session = Session(name="s", owner="o")
            session.id = reference
            return session

    manager.sessions = FakeSessions()  # type: ignore[assignment]
    result = await manager.resume_session("s1")
    assert len(result["fresh"]) == 1
    assert "failed verification" in result["fresh"][0]["reason"]
    # The corrupt checkpoint was flagged on the bus.
    assert any(e["event_type"] == "recovery.checkpoint_corrupt" for e in manager.bus.events)  # type: ignore[attr-defined]
    await db.close()


async def test_failure_accounting_reaches_dead_letter(tmp_path: Any) -> None:
    db = await init_database(f"sqlite+aiosqlite:///{(tmp_path / 'd.db').as_posix()}")
    manager = make_manager(db)
    lane = Lane(session_id="s1", name="alice", harness="mock")

    verdicts = [await manager.record_task_failure(lane, error=f"fail {i}") for i in range(1, 4)]
    assert verdicts == ["retry", "retry", "dead_letter"]
    assert manager.dead_lettered == {lane.id}
    assert any(e["event_type"] == "recovery.dead_letter" for e in manager.bus.events)  # type: ignore[attr-defined]

    # A success clears the ledger.
    manager.record_task_success(lane)
    assert manager.dead_lettered == set()
    await db.close()


async def test_circuit_breaker_opens_and_half_opens(monkeypatch: Any) -> None:
    from datetime import timedelta

    from openburrow.core.models.base import now as real_now

    breaker = CircuitBreaker(threshold=3, cooldown_s=300.0)
    harness = "mock"
    assert not breaker.record_failure(harness)
    assert not breaker.record_failure(harness)
    assert breaker.record_failure(harness)  # third consecutive failure opens it
    assert breaker.status()[harness]["open"] is True

    # While open (and within the cooldown), spawns are refused.
    assert not breaker.allow(harness)

    # Past the cooldown, one probe is let through (half-open). Time is shifted
    # rather than slept: a chaos/recovery test that sleeps is a slow test.
    future = real_now() + timedelta(seconds=600)
    monkeypatch.setattr("openburrow.daemon.recovery.now", lambda: future)
    assert breaker.allow(harness)  # the probe
    breaker.record_success(harness)
    assert breaker.status()[harness]["open"] is False


async def test_bulkhead_flags_pressure() -> None:
    bulkhead = Bulkhead(queue_size=8)
    bulkhead.record("lane-1", depth=4)
    assert bulkhead.overflowed() == set()
    bulkhead.record("lane-1", depth=12)
    assert bulkhead.overflowed() == {"lane-1"}
    assert bulkhead.snapshot()["overflowed_lanes"] == ["lane-1"]


async def test_crash_log_ring_is_bounded(tmp_path: Any) -> None:
    db = await init_database(f"sqlite+aiosqlite:///{(tmp_path / 'e.db').as_posix()}")
    manager = make_manager(db)
    lane = Lane(session_id="s1", name="alice", harness="mock")
    adapter = StubAdapter(running=False)
    running = SimpleNamespace(lane=lane, adapter=adapter)

    for _ in range(8):
        await manager.record_crash(running, reason="boom", exit_code=1)  # type: ignore[arg-type]
    assert len(manager.crash_logs(lane.id)) == 5  # crash_log_keep
    await db.close()


def test_settings_defaults_are_safe() -> None:
    """auto_resume off — a human is asked before a crashed session self-restarts."""
    assert make_settings().recovery_auto_resume is False


# ---------------------------------------------------------------------------
# Orphan requeue
#
# The module docstring above has claimed "orphan requeue" was covered since this
# file was written. It was not — there was no orphan test in it at all, and the
# gap was load-bearing. A lane left behind by a *previous* daemon is not in this
# process's running map, so ``stop_lane`` returns False **without writing the
# row**; the row stayed non-terminal, stayed stale, and was requeued again on the
# next tick. Observed in the wild: 117 requeues of one lane over ten minutes,
# each one emitting a bus event and clearing task assignees, with the count still
# climbing when the daemon was stopped.
#
# A test that only asserted "requeue_orphans returns the orphan" would have
# passed throughout. What matters is that the *second* call finds nothing.
# ---------------------------------------------------------------------------
class FakeSessions:
    """``stop_lane`` as the real one behaves for a lane it does not own."""

    def __init__(self, *, stopped: bool) -> None:
        self._stopped = stopped
        self.stopped_ids: list[str] = []

    async def stop_lane(self, lane_id: str, *, reason: str = "") -> bool:
        self.stopped_ids.append(lane_id)
        return self._stopped


def make_manager_with_sessions(database: Database, sessions: Any) -> RecoveryManager:
    config = SimpleNamespace(
        settings=make_settings(),
        policy=PolicyConfig(),
        paths=SimpleNamespace(repo_root="."),
    )
    return RecoveryManager(
        config,  # type: ignore[arg-type]
        database,
        RecordingBus(),  # type: ignore[arg-type]
        sessions=sessions,
    )


async def _seed_stale_lane(db: Database, *, name: str = "alice") -> Lane:
    """A lane whose heartbeat stopped long ago and whose process is gone."""
    lane = Lane(session_id="s1", name=name, harness="mock", owner="human")
    lane.status = LaneStatus.IDLE
    lane.started_at = now() - timedelta(seconds=600)
    lane.last_heartbeat = now() - timedelta(seconds=600)
    async with db.session() as session:
        await Repository(session).save(lane)
        await session.commit()
    return lane


async def test_an_orphan_without_a_process_is_marked_crashed_and_not_retried(
    tmp_path: Any,
) -> None:
    """The loop-breaker. The second pass is the assertion that matters.

    Reporting the orphan once is the easy half. A detector that reports the same
    dead lane on every tick is not detecting, it is repeating — and it grows a
    retry counter and a bus event without bound while it does.
    """
    db = await init_database(f"sqlite+aiosqlite:///{(tmp_path / 'orph.db').as_posix()}")
    sessions = FakeSessions(stopped=False)
    manager = make_manager_with_sessions(db, sessions)
    lane = await _seed_stale_lane(db)

    first = await manager.requeue_orphans(older_than_s=60)
    assert [row["lane_id"] for row in first] == [lane.id]
    assert sessions.stopped_ids == [lane.id]

    # The whole point: the lane is terminal now, so the next tick is silent.
    assert await manager.requeue_orphans(older_than_s=60) == []

    async with db.session() as session:
        loaded = await Repository(session).get(Lane, lane.id)
    assert loaded is not None
    assert loaded.status is LaneStatus.CRASHED
    assert loaded.stopped_at is not None
    await db.close()


async def test_a_lane_the_process_can_stop_is_left_to_stop_lane(tmp_path: Any) -> None:
    """The other branch, so the fix is not a blanket "mark everything crashed".

    When ``stop_lane`` succeeds it has already written the terminal row and
    emitted ``lane.stopped``. Requeueing must not second-guess that write, or
    every ordinary orphan would be recorded twice under two different reasons.
    """
    db = await init_database(f"sqlite+aiosqlite:///{(tmp_path / 'orph2.db').as_posix()}")
    sessions = FakeSessions(stopped=True)
    manager = make_manager_with_sessions(db, sessions)
    lane = await _seed_stale_lane(db, name="bob")

    await manager.requeue_orphans(older_than_s=60)

    async with db.session() as session:
        loaded = await Repository(session).get(Lane, lane.id)
    assert loaded is not None
    # Untouched: stop_lane owns the write in this branch.
    assert loaded.status is LaneStatus.IDLE
    await db.close()
