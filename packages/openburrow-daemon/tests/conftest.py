"""Fixtures for the daemon package's tests.

The daemon package had no tests before this pass, which is precisely where the
23-method gap lived: the request surface is not an engine, so no engine test
could see it. The fixtures here are the minimum needed to drive that surface
with a real database under it.

``_isolate_state_dir`` is not ceremonial. ``Settings`` reads
``OPENBURROW_STATE_DIR``, and ``get_settings`` is ``lru_cache``d, so without
clearing the cache a settings object built by an earlier test keeps answering
after the environment changed — making the isolation real only for whichever
test ran first.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from openburrow.core.config.settings import clear_settings_cache
from openburrow.core.db.engine import Database, close_all, init_database


@pytest_asyncio.fixture
async def db_url(tmp_path) -> str:
    """A unique on-disk SQLite URL.

    A file, not ``:memory:``: ``get_database`` caches engines by URL, and an
    in-memory SQLite gives every connection its own private database — so a
    schema created on one connection is invisible on the next and a test fails
    for a reason that has nothing to do with the code under test.
    """
    return f"sqlite+aiosqlite:///{(tmp_path / 'burrow.db').as_posix()}"


@pytest_asyncio.fixture
async def database(db_url: str) -> Database:
    """An initialised database with the schema created."""
    return await init_database(db_url)


@pytest.fixture(autouse=True)
def _isolate_state_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENBURROW_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("OPENBURROW_ENV", "development")
    clear_settings_cache()
    yield
    clear_settings_cache()


@pytest_asyncio.fixture(autouse=True)
async def _close_engines():
    """Close every engine this test opened, so the file lock is released."""
    yield
    await close_all()
