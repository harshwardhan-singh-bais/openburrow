"""Fixtures for the brain package's tests.

This directory was empty until the effect-accounting pass. That is worth stating
plainly, because the package's own module docstrings described behaviour that no
test had ever exercised: ``select_for_injection`` persisting its counter, the
merge counting as corroboration rather than delivery, and ``evict_noise``
refusing to retire on a signal nobody sent. All three were changed in that pass,
and all three were changed with no coverage — so the fixtures exist first and the
tests are the falsification.

``_isolate_state_dir`` is copied from the daemon's conftest rather than imported.
``Settings`` reads ``OPENBURROW_STATE_DIR`` and ``get_settings`` is
``lru_cache``d, so without clearing the cache a settings object built by an
earlier test keeps answering after the environment changed. Cross-package fixture
imports are not supported by pytest without a plugin, and the duplication is
three lines of setup against a class of failure that is invisible when it
happens.
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
    in-memory SQLite gives every connection its own private database — so the
    schema created on one connection is invisible on the next and a test fails
    for a reason that has nothing to do with the code under test. The counter
    assertions in these tests read the row back on a *new* session, which is
    exactly the case that would break.
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
