"""Round-tripping a domain model through its row.

The case that motivated this file: ``Session`` declares ``is_open`` and
``lane_count`` as ``@computed_field``, so ``model_dump()`` emits them. They are
not columns, and the row object rejects them::

    ValueError: "SessionRow" object has no field "is_open"

Only the update path noticed. ``SessionRow(**data)`` on insert quietly ignores
extras — pydantic's default ``extra="ignore"`` — so a session saved cleanly the
first time and then failed the instant anything wrote to it again. That is
exactly the sequence ``burrow session start`` performs, which is how a session
could be created, listed, and shown while the command that created it still
exited non-zero.

The assertions are on what comes back out of the database rather than on return
values, because a mapper that drops half the model also returns a plausible
``model``.
"""

from __future__ import annotations

from openburrow.core.db.engine import Database
from openburrow.core.db.repository import Repository
from openburrow.core.models import Session
from openburrow.core.models.session import SessionStatus


async def test_a_session_survives_a_second_save(database: Database) -> None:
    """The second save takes the ``setattr`` branch, which used to raise."""
    async with database.session() as db_session:
        repo = Repository(db_session)
        session = Session(name="round-trip")
        await repo.save(session)  # insert

        session.status = SessionStatus.ACTIVE
        await repo.save(session)  # update — the branch that raised

        loaded = await repo.get(Session, session.id)

    assert loaded is not None
    assert loaded.status is SessionStatus.ACTIVE


async def test_computed_fields_are_never_written_to_a_row(database: Database) -> None:
    """``is_open``/``lane_count`` are derived, so they must not reach the row."""
    async with database.session() as db_session:
        repo = Repository(db_session)
        session = Session(name="computed")
        await repo.save(session)
        loaded = await repo.get(Session, session.id)

    assert loaded is not None
    # Still computed on the way back out, from the column that *was* stored.
    assert loaded.is_open is True
    assert loaded.lane_count == 0


async def test_the_mapper_emits_only_declared_fields() -> None:
    """The guard at the level of the mapper, not the database.

    This is the assertion that would have failed loudly instead of turning into
    a ``ValueError`` inside SQLAlchemy, and it keeps failing when the next
    computed field is added to a model.
    """
    from openburrow.core.db.repository import _to_row_data

    session = Session(name="mapper")
    data = _to_row_data(session)
    declared = set(type(session).model_fields)

    assert "is_open" not in data
    assert "lane_count" not in data
    # ``metadata`` is renamed to ``metadata_json`` on the way to the row, so the
    # column name is the one extra key that is allowed to appear.
    assert set(data) - {"metadata_json"} <= declared
    assert "metadata" not in data
    # And the fields that *are* columns still made it through.
    assert data["name"] == "mapper"
    assert data["id"] == session.id
