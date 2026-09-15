"""Shared fixtures for the relay tests.

The relay's interesting behaviour is almost all reachable without a database:
configuration validation, token minting and verification, the rate limiter, the
fan-out hub, and the request validation inside `append_events`. Only the SQL
itself needs Postgres, and those tests are marked ``integration`` and skipped
unless ``OPENBURROW_TEST_DB_URL`` is set.

That split is deliberate. A test suite that requires a database to check that a
tampered JWT is rejected is a suite that stops running.

Everything is exposed as a fixture rather than a plain module constant because
the relay is one of eleven packages, each with its own ``tests/`` directory and
no ``__init__.py``. Relative imports between test modules therefore do not work,
and a shared helper module would collide on ``sys.modules`` across packages.
Fixtures have neither problem.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator

import pytest

from openburrow.relay.config import RelaySettings
from openburrow.relay.metrics import build_metrics
from openburrow.relay.state import AppState

#: A secret long enough to satisfy the 32-byte floor.
TEST_SECRET = "test-secret-that-is-long-enough-for-hs256-x"

TEST_DB_URL = "postgresql://burrow:burrow@localhost:5432/burrow_test"


@pytest.fixture
def test_secret() -> str:
    return TEST_SECRET


@pytest.fixture
def test_db_url() -> str:
    return TEST_DB_URL


@pytest.fixture
def settings_factory() -> Callable[..., RelaySettings]:
    """Build settings from an explicit mapping, never the real environment.

    Mutating ``os.environ`` in a fixture leaks across tests when one of them
    fails before teardown, which produces a suite whose failures depend on order.
    """

    def _make(**overrides: object) -> RelaySettings:
        env = {
            "OPENBURROW_RELAY_JWT_SECRET": TEST_SECRET,
            "OPENBURROW_RELAY_DB_URL": TEST_DB_URL,
            "OPENBURROW_RELAY_LOG_FORMAT": "console",
        }
        env.update({f"OPENBURROW_RELAY_{key}": str(value) for key, value in overrides.items()})
        return RelaySettings.from_env(env)

    return _make


@pytest.fixture
def settings(settings_factory: Callable[..., RelaySettings]) -> RelaySettings:
    return settings_factory()


@pytest.fixture
def state(settings: RelaySettings) -> AppState:
    """An AppState with its own metric registry.

    A fresh registry per test matters: prometheus_client raises on a duplicate
    collector name, so a shared registry makes the second test in a file fail for
    reasons that have nothing to do with the code under test.
    """
    return AppState.build(settings, metrics=build_metrics())


@pytest.fixture
def integration_db() -> Iterator[str]:
    """The URL of a real Postgres, or skip.

    Read from the environment rather than a hard-coded default so that CI decides
    whether to run these. A default that happens to exist on one developer's
    machine produces tests that pass there and skip everywhere else, which is
    worse than an explicit skip.
    """
    url = os.environ.get("OPENBURROW_TEST_DB_URL")
    if not url:
        pytest.skip("set OPENBURROW_TEST_DB_URL to run relay integration tests")
    yield url
