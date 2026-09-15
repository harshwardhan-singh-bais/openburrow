"""``LogTimer`` — the timing context manager.

It was broken in a way that no import, lint, or type check could see: the class
declares ``__slots__``, and ``_start`` was not in it. ``__slots__`` without a
``__dict__`` means assigning an undeclared name *raises* rather than creating
one, so ``__enter__`` raised ``AttributeError`` on every use and the timer could
never time anything.

Nothing caught it because nothing used it. It is exported as public API, so the
fix is to make it work rather than to delete it — and to keep a test that fails
if the slot list drifts from the attributes again.
"""

from __future__ import annotations

import pytest

from openburrow.core.logging import LogTimer

pytestmark = pytest.mark.unit


class TestLogTimer:
    def test_can_be_entered_and_exited(self) -> None:
        """The whole bug, in one line: this used to raise ``AttributeError``."""
        with LogTimer("test.timer"):
            pass

    def test_exit_does_not_raise_when_the_body_raises(self) -> None:
        """The ``error`` branch in ``__exit__`` reads ``_start`` too."""
        with pytest.raises(ValueError, match="boom"), LogTimer("test.timer"):
            raise ValueError("boom")

    def test_slots_covers_every_attribute_the_timer_assigns(self) -> None:
        """``__slots__`` is a hand-maintained mirror of the class's attributes.

        Nothing checks the two agree, which is why this test exists: it exercises
        both paths and then asserts the instance carries no ``__dict__``, so an
        attribute assigned outside ``__slots__`` fails here rather than in
        production. Adding a slot without a corresponding attribute is harmless;
        the reverse is what raises.
        """
        timer = LogTimer("test.timer")
        with timer:
            pass

        assert not hasattr(timer, "__dict__")
        for name in ("_start", "_event", "_fields", "_level", "_logger"):
            assert hasattr(timer, name), f"__slots__ declares {name} but it was never assigned"
