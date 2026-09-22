"""The heartbeat must reach the record, not just the object.

This file pins the second half of the stale-lane fix. ``Repository.stale_lanes``
reads lanes **from records** — its own docstring says so — and
``SessionManager._check_lane`` used to call ``lane.heartbeat()``, which mutates
the in-memory ``Lane`` and writes nothing. The liveness check therefore read a
record that nothing maintained, and answered "dead".

The consequence was not subtle. With both defects present the daemon reaped every
lane within one supervision tick of starting:

    lane.started   name=mock-1
    lane.stopped   reason='orphaned — stale heartbeat'    # 16 ms later

The row looked innocent afterwards because ``stop_lane`` persists the in-memory
lane — so the call that killed the lane also wrote the heartbeat that made it
look alive. The evidence was manufactured by the mistake.

``test_lane_heartbeat_reaches_the_record`` is the regression: it drives the real
``_check_lane`` against a real database and asserts the stored heartbeat moved.
It fails against the old code no matter how the repository query is written,
which is why it is a separate file from ``openburrow-core``'s
``test_stale_lanes.py``: the two defects are independent, and a fix to either one
alone leaves the other reachable.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from openburrow.core.db.engine import Database
from openburrow.core.db.repository import Repository
from openburrow.core.models import Lane, LaneRole, LaneStatus
from openburrow.core.models.base import now
from openburrow.daemon.sessions import RunningLane, SessionManager

pytestmark = [pytest.mark.unit]


class RecordingBus:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def emit(self, **payload: Any) -> None:
        self.events.append(payload)


class LiveAdapter:
    """The minimum a lane needs to survive one supervision pass.

    ``is_running`` must be True or ``_check_lane`` takes the crash path and the
    heartbeat is never reached — the test would then pass for the wrong reason.
    """

    is_running = True
    returncode = None

    async def stop(self, *, force: bool = False) -> None:  # pragma: no cover - not called
        raise AssertionError("a healthy lane must not be stopped by a supervision pass")


def make_manager(database: Database) -> SessionManager:
    config = SimpleNamespace(
        policy=SimpleNamespace(),
        paths=SimpleNamespace(repo_root=None),
        settings=SimpleNamespace(governance_human_id="", governance_human_email=""),
        adapters=SimpleNamespace(crash_restart="never", max_restarts=0),
    )
    return SessionManager(
        config=config,  # type: ignore[arg-type]
        database=database,
        bus=RecordingBus(),  # type: ignore[arg-type]
        registry=None,  # type: ignore[arg-type]
    )


def make_lane(*, heartbeat: Any) -> Lane:
    return Lane(
        session_id="sess_hb",
        name="mock-1",
        harness="mock",
        role=LaneRole.IMPLEMENTER,
        status=LaneStatus.IDLE,
        owner="human",
        started_at=now(),
        last_heartbeat=heartbeat,
    )


async def _persist(database: Database, lane: Lane) -> None:
    async with database.session() as db_session:
        await Repository(db_session).save(lane)


async def test_lane_heartbeat_reaches_the_record(database: Database) -> None:
    """The regression. Fails against the old code however the query is written.

    The lane is seeded with a heartbeat old enough to be an orphan. One
    supervision pass must make it young again *in the database*, because that is
    where the orphan detector looks.
    """
    manager = make_manager(database)
    lane = make_lane(heartbeat=now() - timedelta(hours=1))
    await _persist(database, lane)

    async with database.session() as db_session:
        repo = Repository(db_session)
        assert [x.id for x in await repo.stale_lanes(older_than_seconds=60)] == [lane.id]

    running = RunningLane(lane=lane, adapter=LiveAdapter())  # type: ignore[arg-type]
    await manager._check_lane(running)

    async with database.session() as db_session:
        repo = Repository(db_session)
        assert await repo.stale_lanes(older_than_seconds=60) == [], (
            "the heartbeat never reached the record, so the orphan detector still sees a dead lane"
        )


async def test_a_supervision_pass_does_not_orphan_a_fresh_lane(database: Database) -> None:
    """The end-to-end shape of the original bug, in one test.

    A lane is persisted without a heartbeat — exactly the state between insert
    and first beat — and one supervision pass must leave it alone. This is the
    case that reaped a live lane 16 ms after it started.
    """
    manager = make_manager(database)
    lane = make_lane(heartbeat=None)
    await _persist(database, lane)

    running = RunningLane(lane=lane, adapter=LiveAdapter())  # type: ignore[arg-type]
    await manager._check_lane(running)

    assert lane.status is LaneStatus.IDLE
    async with database.session() as db_session:
        repo = Repository(db_session)
        assert await repo.stale_lanes(older_than_seconds=60) == []


async def test_a_healthy_pass_emits_no_events(database: Database) -> None:
    """Supervision of a healthy lane is silent.

    A pass that publishes on every tick turns the bus into a heartbeat log and
    buries the events that matter. ``is_running`` is True and the budget is not
    exceeded, so nothing should be published at all.
    """
    manager = make_manager(database)
    lane = make_lane(heartbeat=now())
    await _persist(database, lane)

    running = RunningLane(lane=lane, adapter=LiveAdapter())  # type: ignore[arg-type]
    await manager._check_lane(running)

    assert manager.bus.events == []  # type: ignore[attr-defined]
