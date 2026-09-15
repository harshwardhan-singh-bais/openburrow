"""The bus event log: the canonical write path.

These tests exist because that path was silently dead. ``BusEventLog`` was used
as ``async with factory() as log`` while implementing neither ``__aenter__`` nor
``__aexit__``, so every call raised ``TypeError``; the one caller that mattered
caught it and logged at ``debug`` level. Meanwhile ``append`` only ever called
``flush()``, so even a call that got past the protocol would have been discarded
when the session closed.

Both halves of that are asserted below, and deliberately at the level of
*persistence* rather than return values: ``append`` returning a plausible
sequence number is exactly what a broken implementation also does.
"""

from __future__ import annotations

import typing
from collections.abc import Callable

import pytest

from openburrow.a2a.lifecycle.manager import TaskLifecycleManager
from openburrow.core.db.engine import Database
from openburrow.core.db.repository import BusEventLog

pytestmark = pytest.mark.integration


class TestContextManagerProtocol:
    """The protocol itself, because its absence was the whole bug."""

    def test_bus_event_log_is_an_async_context_manager(self) -> None:
        assert hasattr(BusEventLog, "__aenter__")
        assert hasattr(BusEventLog, "__aexit__")

    async def test_entering_returns_the_log_itself(self, database: Database) -> None:
        async with BusEventLog(database.session_factory()) as bus_log:
            assert isinstance(bus_log, BusEventLog)

    async def test_exiting_commits(self, database: Database) -> None:
        """A write inside the block must outlive the block."""
        async with BusEventLog(database.session_factory()) as bus_log:
            seq = await bus_log.append(event_type="test.committed", session_id="sess_cm")
        assert seq > 0

        # A brand new session, so nothing can be served from the identity map.
        async with BusEventLog(database.session_factory()) as reader:
            events = await reader.stream(session_id="sess_cm")
        assert [e["event_type"] for e in events] == ["test.committed"]

    async def test_exiting_rolls_back_on_error(self, database: Database) -> None:
        """A write inside a failing block must not be persisted.

        Without this, a partially-applied transition would land in the audit log
        as though it had succeeded — the log is what everything else trusts.
        """
        with pytest.raises(RuntimeError):
            async with BusEventLog(database.session_factory()) as bus_log:
                await bus_log.append(event_type="test.rolled_back", session_id="sess_rb")
                raise RuntimeError("transition failed after the write")

        async with BusEventLog(database.session_factory()) as reader:
            events = await reader.stream(session_id="sess_rb")
        assert events == []

    async def test_no_transaction_is_left_open_after_the_block(self, database: Database) -> None:
        """The connection must go back to the pool, not be held by an open transaction.

        Not asserted as "the session is closed": SQLAlchemy sessions are
        explicitly reusable after ``close()``, and ``AsyncSession.is_active``
        reports partial-rollback state rather than closure — my first version of
        this test asserted the latter and failed against correct code. An open
        transaction is the part that actually leaks a connection, so that is
        what is checked.
        """
        log = BusEventLog(database.session_factory())
        async with log:
            await log.append(event_type="test.closed", session_id="sess_close")
        assert not log.session.in_transaction()


class TestAppend:
    """Behaviour that must hold regardless of how the session is owned."""

    async def test_sequence_numbers_are_increasing(self, database: Database) -> None:
        async with BusEventLog(database.session_factory()) as bus_log:
            first = await bus_log.append(event_type="a", session_id="sess_seq")
            second = await bus_log.append(event_type="b", session_id="sess_seq")
        assert second > first

    async def test_duplicate_content_hash_is_refused(self, database: Database) -> None:
        """``A2A_DEDUPE_WINDOW_S`` depends on this."""
        from openburrow.core.errors import BusError

        async with BusEventLog(database.session_factory()) as bus_log:
            await bus_log.append(event_type="a", session_id="sess_dup", content_hash="same-hash")
            with pytest.raises(BusError):
                await bus_log.append(
                    event_type="a", session_id="sess_dup", content_hash="same-hash"
                )

    async def test_stream_filters_by_session(self, database: Database) -> None:
        async with BusEventLog(database.session_factory()) as bus_log:
            await bus_log.append(event_type="one", session_id="sess_x")
            await bus_log.append(event_type="two", session_id="sess_y")

        async with BusEventLog(database.session_factory()) as reader:
            events = await reader.stream(session_id="sess_x")
        assert [e["event_type"] for e in events] == ["one"]


class TestTheFailureModeThatWasThere:
    """Regression guards aimed at the specific shape of the original bug."""

    async def test_a_factory_returning_a_log_supports_async_with(self, database: Database) -> None:
        """This is the exact call the manager makes, and the exact call that raised."""

        def factory() -> BusEventLog:
            return BusEventLog(database.session_factory())

        async with factory() as bus_log:
            seq = await bus_log.append(event_type="test.manager_call_shape", session_id="sess_m")

        assert seq > 0

    def test_the_manager_declares_a_synchronous_factory(self) -> None:
        """The manager's ``bus`` parameter must be a factory returning a log.

        This is the signature that drifted. It was annotated ``BusEventLog``
        while both callers passed a factory, and the mismatch was silenced with
        a ``type: ignore[arg-type]`` at one call site — which is how a type error
        became an ``AttributeError`` on ``self.bus.append`` at runtime.

        Reading the annotation back is a real guard rather than a restatement:
        an earlier version of this test asserted that a locally-defined ``def``
        was not a coroutine, which is true of every ``def`` and therefore
        asserted nothing about the code that broke.
        """
        hints = typing.get_type_hints(TaskLifecycleManager.__init__)
        assert hints["bus"] == Callable[[], BusEventLog]
