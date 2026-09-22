"""Contract tests for the adapter base class.

``HarnessAdapter._after_start`` was declared as a plain ``def`` returning ``None``
while the start path called ``await self._after_start(lane)``. Awaiting ``None``
raises ``TypeError: object NoneType can't be used in 'await' expression``, and no
subclass overrode the hook — so the base implementation is the one that always
ran, and no adapter could start.

That is a defect with no visible symptom in any static check: the annotation said
``-> None``, the call site said ``await``, and both are individually plausible.
The only thing that catches it is asserting the two agree.
"""

from __future__ import annotations

import inspect

import pytest

from openburrow.adapters.base import HarnessAdapter
from openburrow.adapters.harnesses.mock import MockAdapter
from openburrow.core.config.settings import Settings
from openburrow.core.models import Lane

pytestmark = pytest.mark.unit


class TestAfterStartHook:
    def test_is_a_coroutine_function(self) -> None:
        """The call site awaits it, so it must be awaitable."""
        assert inspect.iscoroutinefunction(HarnessAdapter._after_start)

    async def test_awaiting_it_yields_none(self) -> None:
        """Awaiting the base hook is a no-op, not an error.

        Exercised on a concrete adapter rather than the abstract base, so this
        also proves a real subclass inherits a hook the start path can await.
        The previous bug was that the expression ``await self._after_start(lane)``
        could not be evaluated at all, and only actually awaiting it proves the
        shape is right.
        """
        adapter = MockAdapter(Settings())
        lane = Lane(name="probe", harness="mock", session_id="sess_probe")
        # Awaiting it is the whole assertion. The hook is declared `-> None`,
        # so there is no value to compare — and the previous bug was that the
        # expression could not be evaluated at all.
        await adapter._after_start(lane)
