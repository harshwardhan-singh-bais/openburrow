"""Closing a session must be recorded, and must not collide with its creation.

The defect this file exists for: ``BusEventLog.append`` read ``payload["id"]`` and
used it as the bus event's *primary key*. ``session.created`` passes
``session.model_dump(mode="json")`` and ``session.closed`` passes
``session.summary()`` — both of which contain ``id`` — so the close emitted the
same primary key as the create and raised
``UNIQUE constraint failed: bus_events.id``.

Two things let it survive, and both are why the tests here are shaped the way
they are:

1. **Every daemon test stubbed the bus.** ``RecordingBus`` in
   ``test_lane_heartbeat.py`` records ``emit`` calls into a list with no database
   behind them, so the real write path was never exercised by a lifecycle test.
   The fixture here builds a genuine ``EventBus`` over the real ``BusEventLog``.
2. **The failure was silent in the direction that matters.** ``close_session``
   persists the session *before* it emits, so the session row said ``completed``
   while the append-only log — the source of truth that audit, report, replay
   and the standup all read — had no close entry at all. ``burrow session close``
   printed an error and the log disagreed with the database, with the log losing.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import text

from openburrow.core.db.engine import Database
from openburrow.core.db.repository import BusEventLog, Repository
from openburrow.core.models import Session
from openburrow.daemon.bus import EventBus
from openburrow.daemon.sessions import SessionManager

pytestmark = [pytest.mark.unit]


def make_manager(database: Database) -> SessionManager:
    """A manager whose bus actually writes, because that is what the bug needed."""
    config = SimpleNamespace(
        policy=SimpleNamespace(),
        paths=SimpleNamespace(repo_root=None),
        repo=SimpleNamespace(lanes=[]),
        settings=SimpleNamespace(
            governance_human_id="",
            governance_human_email="",
            worktree_cleanup="on-close",
        ),
        adapters=SimpleNamespace(crash_restart="never", max_restarts=0),
    )
    return SessionManager(
        config=config,  # type: ignore[arg-type]
        database=database,
        bus=EventBus(lambda: BusEventLog(database.session_factory())),
        registry=None,  # type: ignore[arg-type]
    )


async def event_types(database: Database) -> list[str]:
    async with database.session() as db_session:
        result = await db_session.execute(text("select event_type from bus_events order by seq"))
        return [row[0] for row in result.fetchall()]


async def event_ids(database: Database) -> list[str]:
    async with database.session() as db_session:
        result = await db_session.execute(text("select id from bus_events order by seq"))
        return [row[0] for row in result.fetchall()]


async def test_two_events_about_one_entity_do_not_collide(database: Database) -> None:
    """The root cause, tested where it lives.

    Both payloads carry the same ``id``, which is what any entity dump does.
    Under the old code the second insert violated the primary key.
    """
    payload = {"id": "sess_shared", "name": "one entity, two events"}
    bus = EventBus(lambda: BusEventLog(database.session_factory()))

    await bus.emit(event_type="session.created", session_id="sess_shared", payload=payload)
    await bus.emit(event_type="session.closed", session_id="sess_shared", payload=payload)

    assert await event_types(database) == ["session.created", "session.closed"]

    ids = await event_ids(database)
    assert "sess_shared" not in ids, "the payload's id is not the event's identity"
    assert len(set(ids)) == 2, "each event gets its own identity"


async def test_closing_a_session_records_the_close_event(database: Database) -> None:
    """The end-to-end symptom: this call used to raise ``bus_error``.

    The ``session.created`` emit is not decoration. Without it this test passes
    against the defect, because a lone close has no earlier row to collide with
    — the collision needs the pair, which is exactly what
    ``SessionManager.start_session`` produces. Measured: with the defect
    reintroduced, this test failed only once the create was emitted first.
    """
    manager = make_manager(database)
    session = Session(name="close-me")
    session.thread_id = session.id

    async with database.session() as db_session:
        await Repository(db_session).save(session)

    # What `start_session` does, reproduced so the pair exists.
    await manager.bus.emit(
        event_type="session.created",
        session_id=session.id,
        thread_id=session.thread_id,
        payload=session.model_dump(mode="json"),
    )

    await manager.close_session(session.id)

    events = await event_types(database)
    assert events == ["session.created", "session.closed"], "the close must reach the log"

    # The log and the row agree, which is the property the bug broke.
    # Asserted against the database rather than the local `session` object:
    # `get_session` loads a fresh instance, so closing updates *that* one and
    # the object this test holds would still read `created`. A test that read it
    # would be checking its own stale reference.
    async with database.session() as db_session:
        result = await db_session.execute(text("select status from sessions"))
        assert [row[0] for row in result.fetchall()] == ["completed"]


async def test_closing_twice_still_records_both_attempts(database: Database) -> None:
    """A second close is a second event, not a collision.

    Worth pinning because the old failure looked like "already closed" when it
    was really "this row cannot be inserted at all".
    """
    manager = make_manager(database)
    session = Session(name="close-twice")
    session.thread_id = session.id

    async with database.session() as db_session:
        await Repository(db_session).save(session)

    await manager.close_session(session.id)
    await manager.close_session(session.id)

    events = await event_types(database)
    assert events.count("session.closed") == 2
