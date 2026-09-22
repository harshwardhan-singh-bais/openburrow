"""Repository-wide pytest hooks.

One hook, for the ``e2e`` marker. ``pyproject.toml`` declares that marker as
"spawns real harnesses (skipped unless ``OPENBURROW_E2E=1``)", ``.env.example``
documents ``OPENBURROW_E2E``, and ``Settings.e2e`` carries it — but until this
file existed nothing implemented the skip and no test carried the marker, so the
documented behaviour was a comment in three places and a mechanism in none. That
is the declared-and-inert shape this repository keeps finding, and the cost of it
here is concrete rather than theoretical: a test that spawns Claude Code on
somebody's laptop, in CI, without anyone having asked for it.

The skip lives in a hook rather than in a ``@pytest.mark.skipif`` on each test so
that *forgetting* the decorator is impossible. A marker is checked for every
collected item; a decorator is only checked where somebody remembered to write
one, and the failure mode of forgetting is a real harness running by accident.
"""

from __future__ import annotations

import os

import pytest


def real_harnesses_enabled() -> bool:
    """Whether tests may spawn the harness binaries installed on this machine.

    Reads ``Settings.e2e`` rather than ``os.environ`` directly, for two reasons.
    It makes the documented switch work from a ``.env`` file as well as from the
    shell, which is how every other setting in this project behaves; and it gives
    the declared field the consumer that makes it real, which is what the rule
    "grep for a setting's consumers before believing it works" asks for.

    Falls back to the raw variable if settings cannot load. A malformed ``.env``
    turning into "the whole suite fails to collect" would be a far worse outcome
    than reading one flag the wrong way — and the fallback is still off unless
    the variable is explicitly set, so the failure direction is safe.
    """
    try:
        from openburrow.core.config.settings import get_settings

        return bool(get_settings().e2e)
    except Exception:
        return os.environ.get("OPENBURROW_E2E", "0") not in ("", "0")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Skip every ``e2e`` item unless real harnesses were explicitly requested.

    Takes only ``items`` from the hook's signature. Pluggy passes a subset of the
    declared arguments, so dropping ``config`` is allowed and cheaper than taking
    it and suppressing the unused-argument warning it would earn.
    """
    if real_harnesses_enabled():
        return
    skip = pytest.mark.skip(reason="e2e: spawns a real harness — set OPENBURROW_E2E=1 to run")
    for item in items:
        if "e2e" in item.keywords:
            item.add_marker(skip)
