"""Stale-lane detection: who counts as an orphan, and who is merely new.

This file exists because ``stale_lanes`` had no test at all, and the bug it hid
made the daemon unable to run a lane. The check read

    last_heartbeat IS NULL OR last_heartbeat < cutoff

so a lane that had *not yet written its first heartbeat* was classified as stale
at **every** threshold — the ``IS NULL`` branch never consulted
``older_than_seconds``. A lane that has just started has no heartbeat yet; that
is the definition of young, not of dead.

It did not stay theoretical. The daemon started a mock lane, and 16 ms later:

    lane.stopped  reason='orphaned — stale heartbeat'

Two defects met. The heartbeat was written only to the in-memory lane, so the
record the detector reads was never maintained; and the detector treated a
missing heartbeat as proof of death rather than as absence of evidence. Either
one alone is survivable. Together they meant every lane was reaped within one
supervision tick of starting, and the evidence was written by the killing call:
``stop_lane`` persists the in-memory lane, so the row ended up carrying the
heartbeat of the lane it had just declared heartless.

Every test here is written against the *threshold*, not against the
implementation, because the bug was precisely that one branch ignored the
threshold it was given.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from openburrow.core.db.engine import Database
from openburrow.core.db.models import LaneRow
from openburrow.core.db.repository import Repository
from openburrow.core.models.base import now
from openburrow.core.models.session import Lane


def _lane_row(lane_id: str, **overrides: object) -> LaneRow:
    """A persistable lane row that satisfies the ``Lane`` model's validators.

    Built from the row class rather than the domain model so a test can express
    states the domain model cannot — specifically ``last_heartbeat=None``, which
    is the state a lane is in between being inserted and beating for the first
    time.
    """
    fields: dict[str, object] = {
        "session_id": "sess_test",
        "name": lane_id,
        "harness": "mock",
        "status": "idle",
        "owner": "human",
        "started_at": now(),
        "last_heartbeat": now(),
        "idle_timeout_s": 900,
    }
    fields.update(overrides)
    return LaneRow(id=lane_id, **fields)


async def _seed(database: Database, *rows: LaneRow) -> None:
    async with database.session() as db_session:
        db_session.add_all(list(rows))
        await db_session.commit()


# ---------------------------------------------------------------------------
# The regression: a lane that has not beaten yet is young, not dead
# ---------------------------------------------------------------------------
async def test_a_lane_that_has_not_beaten_yet_is_not_stale(database: Database) -> None:
    """The bug, stated directly.

    A lane inserted a moment ago with no heartbeat must not be an orphan at any
    sane threshold. Under the old query this returned the lane even at 900s,
    which is what let the supervisor reap a lane that had just started.
    """
    await _seed(database, _lane_row("never_beat", last_heartbeat=None))

    async with database.session() as db_session:
        repo = Repository(db_session)
        for threshold in (1, 5, 60, 900):
            stale = await repo.stale_lanes(older_than_seconds=threshold)
            assert [lane.id for lane in stale] == [], (
                f"a just-started lane was called stale at threshold={threshold}s"
            )


async def test_a_lane_that_started_long_ago_and_never_beat_is_stale(
    database: Database,
) -> None:
    """The other side of the same branch, so the fallback is not a blanket amnesty.

    Fixing the regression by never reporting a heartbeat-less lane would trade
    one bug for another: a lane whose harness hung before its first beat is
    exactly the case orphan detection exists for. It must be measured from when
    it started.
    """
    await _seed(
        database,
        _lane_row(
            "hung_at_start",
            last_heartbeat=None,
            started_at=now() - timedelta(seconds=600),
        ),
    )

    async with database.session() as db_session:
        repo = Repository(db_session)
        assert [x.id for x in await repo.stale_lanes(older_than_seconds=60)] == ["hung_at_start"]
        assert await repo.stale_lanes(older_than_seconds=900) == []


async def test_the_fallback_prefers_started_at_over_created_at(database: Database) -> None:
    """``created_at`` is the last resort, not the first choice.

    A lane can be created well before it is started — queued behind a worktree,
    say. Measuring its liveness from ``created_at`` would report it stale while
    it is still legitimately starting up, so ``started_at`` must win when both
    are present.
    """
    await _seed(
        database,
        _lane_row(
            "queued_then_started",
            last_heartbeat=None,
            created_at=now() - timedelta(seconds=3600),
            started_at=now(),
        ),
    )

    async with database.session() as db_session:
        repo = Repository(db_session)
        assert await repo.stale_lanes(older_than_seconds=60) == []


# ---------------------------------------------------------------------------
# The ordinary cases, so the fix is not just "return nothing"
# ---------------------------------------------------------------------------
async def test_a_fresh_heartbeat_is_never_stale(database: Database) -> None:
    await _seed(database, _lane_row("busy", last_heartbeat=now()))

    async with database.session() as db_session:
        repo = Repository(db_session)
        for threshold in (1, 5, 60, 900):
            assert await repo.stale_lanes(older_than_seconds=threshold) == []


async def test_an_old_heartbeat_is_stale_only_past_the_threshold(database: Database) -> None:
    """Boundary behaviour: the threshold has to actually be applied."""
    await _seed(
        database,
        _lane_row("quiet", last_heartbeat=now() - timedelta(seconds=300)),
    )

    async with database.session() as db_session:
        repo = Repository(db_session)
        assert [x.id for x in await repo.stale_lanes(older_than_seconds=60)] == ["quiet"]
        assert await repo.stale_lanes(older_than_seconds=900) == []


@pytest.mark.parametrize("status", ["stopped", "crashed"])
async def test_dead_lanes_are_not_orphans(database: Database, status: str) -> None:
    """A lane that is already finished cannot be orphaned; requeueing it would
    re-open work that was deliberately closed."""
    await _seed(
        database,
        _lane_row("done", status=status, last_heartbeat=now() - timedelta(days=1)),
    )

    async with database.session() as db_session:
        repo = Repository(db_session)
        assert await repo.stale_lanes(older_than_seconds=60) == []


# ---------------------------------------------------------------------------
# record_heartbeat: the write side of the same contract
# ---------------------------------------------------------------------------
async def test_record_heartbeat_advances_the_stored_heartbeat(database: Database) -> None:
    """A heartbeat must reach the record, because the detector reads records.

    This is the assertion that was missing: the daemon beat the in-memory lane
    and the row kept its insert-time value, so the liveness check read a record
    that nothing maintained.
    """
    stale_at = now() - timedelta(hours=1)
    await _seed(database, _lane_row("lane_hb", last_heartbeat=stale_at))

    async with database.session() as db_session:
        repo = Repository(db_session)
        # Before the beat it is an orphan; the detector is not simply broken.
        assert [x.id for x in await repo.stale_lanes(older_than_seconds=60)] == ["lane_hb"]

        assert await repo.record_heartbeat("lane_hb") is True
        assert await repo.stale_lanes(older_than_seconds=60) == []


async def test_record_heartbeat_reports_whether_a_row_matched(database: Database) -> None:
    """False, not an exception, for an unknown lane.

    The supervisor calls this on every tick for every lane, including one whose
    row has already been reaped. Raising there would end the supervision pass for
    every other lane.
    """
    async with database.session() as db_session:
        repo = Repository(db_session)
        assert await repo.record_heartbeat("lane_does_not_exist") is False


async def test_record_heartbeat_does_not_touch_other_columns(database: Database) -> None:
    """A single-column update, so a beat cannot clobber concurrent work.

    The heartbeat is written twelve times a minute per lane. Rewriting the whole
    row on that cadence is how a supervision pass overwrites a status another
    coroutine just set.
    """
    await _seed(database, _lane_row("lane_keep", status="working"))

    async with database.session() as db_session:
        repo = Repository(db_session)
        await repo.record_heartbeat("lane_keep")

        lane = await repo.get(Lane, "lane_keep")

    assert lane is not None
    # The status set by someone else survives the beat.
    assert lane.status == "working"
