"""Fixtures for the core package's tests.

The core package had no tests at all until the bus-write path turned out to be
dead: ``BusEventLog`` was used as an async context manager without implementing
the protocol, every task transition raised ``TypeError`` into a ``debug``-level
log, and nothing noticed because nothing exercised it. These fixtures exist so
that path has somewhere to be exercised.

A real SQLite file per test, not an in-memory database: ``get_database`` caches
engines by URL, and ``sqlite+aiosqlite:///:memory:`` gives every connection its
own private database — so a schema created on one connection is invisible on the
next, and the test would fail for a reason that has nothing to do with the code
under test.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from openburrow.core.config.settings import clear_settings_cache
from openburrow.core.db.engine import close_all, init_database


@pytest_asyncio.fixture
async def db_url(tmp_path) -> str:
    """A unique on-disk SQLite URL, torn down with its engine."""
    url = f"sqlite+aiosqlite:///{(tmp_path / 'burrow.db').as_posix()}"
    yield url
    await close_all()


@pytest_asyncio.fixture
async def database(db_url: str):
    """An initialised database with the schema created."""
    return await init_database(db_url)


@pytest.fixture(autouse=True)
def _isolate_state_dir(monkeypatch, tmp_path):
    """Keep the developer's real state directory out of every test.

    This fixture used to be decorative, and the way it failed is worth keeping
    written down. It set ``OPENBURROW_STATE_DIR`` and ``OPENBURROW_ENV``, and
    ``Settings`` read neither: with an empty ``env_prefix`` pydantic-settings
    matches the *bare* field name, so the variables were dropped by
    ``extra="ignore"`` and every test that resolved a path got the developer's
    real ``.openburrow``. It also set ``OPENBURROW_ENV=test``, which is not a
    member of the field's ``Literal["development", "staging", "production"]`` —
    proof it was never validated, and a value that would have been rejected the
    moment the prefix started working.

    ``clear_settings_cache`` is not optional either. ``get_settings`` is
    ``lru_cache``d, so a settings object built by an earlier test would keep
    answering after the environment changed, and the isolation would be real only
    for whichever test ran first.
    """
    monkeypatch.setenv("OPENBURROW_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("OPENBURROW_ENV", "development")
    clear_settings_cache()
    yield
    clear_settings_cache()
