"""Approval delivery back into the harness (item 176).

The rule under test: a resolved approval is not finished until the decision
reaches the harness, and an *edited* approval delivers the edited text — not the
original the human just overrode. Delivery failure is reported as data on the
result rather than raised, because the human has already decided; whether the
pipe delivered it is a separate fact that must be visible, not thrown away.

``Handlers`` is bound to a daemon, but ``_deliver_approval`` touches only
``sessions`` and ``_fetch_task`` — both stubbed here — so the real decision
logic runs without a database or a subprocess.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from openburrow.core.models import ApprovalRequest, BusMessage
from openburrow.daemon.handlers import Handlers

pytestmark = pytest.mark.unit


class FakeAdapter:
    def __init__(self, *, raises: bool = False) -> None:
        self.injected: list[BusMessage] = []
        self.tasks: list[Any] = []
        self._raises = raises

    async def inject_message(self, message: BusMessage, task: Any = None) -> bool:
        if self._raises:
            raise RuntimeError("harness gone")
        self.injected.append(message)
        self.tasks.append(task)
        return True


async def no_task(_task_id: str) -> None:
    """Stand-in for a session whose task lookup finds nothing."""
    return


def make_handler(
    running: dict[str, FakeAdapter],
    *,
    fetch_task: Any = None,
) -> Handlers:
    # Deliberately not a `Daemon`: this stub supplies only the four
    # attributes `_deliver_approval` reads, and says so rather than
    # pretending to be the real thing.
    daemon: Any = SimpleNamespace(
        database=None,
        bus=None,
        sessions=SimpleNamespace(
            # The parameter is part of the real `running_lanes` contract; this
            # stand-in returns every lane it was given regardless of session, so
            # the name is marked a dummy rather than dropped.
            running_lanes=lambda _session_id: [
                SimpleNamespace(lane=SimpleNamespace(id=lane_id), adapter=adapter)
                for lane_id, adapter in running.items()
            ],
            fetch_task=fetch_task or no_task,
        ),
        config=SimpleNamespace(settings=SimpleNamespace(governance_human_id="h-1")),
    )
    handler = Handlers.__new__(Handlers)  # skip __init__'s daemon binding shape
    handler.daemon = daemon
    return handler


def make_approval(*, lane_id: str = "lane-1", action: str = "rm -rf build/") -> ApprovalRequest:
    request = ApprovalRequest()
    request.session_id = "sess-1"
    request.lane_id = lane_id
    request.action = action
    request.task_id = ""
    return request


class TestDelivery:
    async def test_approved_action_is_injected(self) -> None:
        adapters = {"lane-1": FakeAdapter()}
        handler = make_handler(adapters)
        approval = make_approval()

        result = await handler._deliver_approval(approval, deny=False, by="h-1")
        assert result["delivered_to_harness"] is True
        assert len(adapters["lane-1"].injected) == 1
        assert "rm -rf build/" in adapters["lane-1"].injected[0].body

    async def test_edited_approval_delivers_the_edit(self) -> None:
        adapters = {"lane-1": FakeAdapter()}
        handler = make_handler(adapters)
        approval = make_approval()
        approval.approve(by="h-1", edited_action="rm -rf build/tmp")
        await handler._deliver_approval(approval, deny=False, by="h-1")
        body = adapters["lane-1"].injected[0].body
        assert "rm -rf build/tmp" in body
        assert "instead of the original" in body
        # The original must not ride along: a human rewrote it.
        assert body.count("rm -rf") == 1

    async def test_denial_is_not_injected(self) -> None:
        adapters = {"lane-1": FakeAdapter()}
        handler = make_handler(adapters)
        result = await handler._deliver_approval(make_approval(), deny=True, by="h-1")
        assert result["delivered_to_harness"] is False
        assert adapters["lane-1"].injected == []

    async def test_dead_lane_reports_not_raises(self) -> None:
        adapters = {"lane-1": FakeAdapter(raises=True)}
        handler = make_handler(adapters)
        result = await handler._deliver_approval(make_approval(), deny=False, by="h-1")
        assert result["delivered_to_harness"] is False
        assert "harness gone" in result["delivery_error"]

    async def test_unknown_lane_reports_not_raises(self) -> None:
        handler = make_handler({})  # nothing running
        result = await handler._deliver_approval(
            make_approval(lane_id="ghost"), deny=False, by="h-1"
        )
        assert result["delivered_to_harness"] is False
        assert "not running" in result["delivery_error"]

    async def test_named_task_is_fetched_and_attached(self) -> None:
        # The branch every other case skips. `make_approval` leaves `task_id`
        # empty, which short-circuits before the fetch — so a call to a method
        # that did not exist on `Handlers` was never made and never failed. An
        # approval that names a task is the ordinary case: the harness needs it
        # to know which A2A task the decision belongs to.
        adapters = {"lane-1": FakeAdapter()}
        fetched: list[str] = []
        sentinel = object()

        async def fetch_task(task_id: str) -> object:
            fetched.append(task_id)
            return sentinel

        handler = make_handler(adapters, fetch_task=fetch_task)
        approval = make_approval()
        approval.task_id = "task-42"

        result = await handler._deliver_approval(approval, deny=False, by="h-1")

        assert result["delivered_to_harness"] is True
        assert fetched == ["task-42"]
        assert adapters["lane-1"].tasks == [sentinel]
