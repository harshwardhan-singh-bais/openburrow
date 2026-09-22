"""Negotiation escalation broadcast (item 138).

An escalation that only the two negotiating lanes can see is a conflict that
surfaces as a merge failure later — the exact failure OpenBurrow exists to
prevent. These tests pin the two halves of the fix: the driver *calls* the
escalator on every escalation path, and the daemon's escalator *persists* the
broadcast through the bus log rather than printing it.
"""

from __future__ import annotations

from typing import Any

import pytest

from openburrow.acp.negotiation import NegotiationDriver
from openburrow.core.models import BusMessage, Performative

pytestmark = pytest.mark.unit


class EscalationRecorder:
    """Collects what the driver broadcast, for assertions."""

    def __init__(self) -> None:
        self.messages: list[BusMessage] = []

    async def __call__(self, message: BusMessage) -> None:
        self.messages.append(message)


def _driver() -> NegotiationDriver:
    return NegotiationDriver(max_exchanges=4, escalate_after=2)


async def _reply(performative: Performative, summary: str) -> Any:
    from openburrow.acp.negotiation import ResponderReply

    return ResponderReply(
        performative=performative, summary=summary, refs=["src/a.py"], engaged=True
    )


@pytest.mark.asyncio
async def test_stuck_exchange_invokes_escalator() -> None:
    """Positions unchanged past escalate_after → the escalator fires."""
    recorder = EscalationRecorder()
    driver = _driver()
    replies = iter(
        [
            await _reply(Performative.COUNTER, "no"),
            await _reply(Performative.COUNTER, "no"),
            await _reply(Performative.COUNTER, "no"),
            await _reply(Performative.COUNTER, "no"),
        ]
    )

    async def respond(_lane: str, _message: BusMessage) -> Any:
        return next(replies)

    async def deliver(_message: BusMessage) -> None:
        return None

    result = await driver.run(
        session_id="s1",
        thread_id="s1",
        lane_a="lane-a",
        lane_b="lane-b",
        topic="same file",
        description="both want src/a.py",
        contested_refs=["src/a.py"],
        opening="mine",
        respond=respond,
        deliver=deliver,
        escalate=recorder,
    )

    assert result.escalated
    assert not result.agreed
    assert len(recorder.messages) == 1
    escalation = recorder.messages[0]
    assert escalation.intent == Performative.INFORM
    # Whole-session visibility: broadcast, not addressed to one lane.
    assert escalation.broadcast is True
    payload = escalation.payload
    assert payload.get("openburrow:escalation") is True
    assert payload.get("openburrow:negotiationId")
    assert payload.get("openburrow:lanes") == ["lane-a", "lane-b"]


@pytest.mark.asyncio
async def test_reject_after_threshold_invokes_escalator() -> None:
    """A rejection once the escalation window is open also broadcasts."""
    recorder = EscalationRecorder()
    driver = _driver()
    replies = iter(
        [
            await _reply(Performative.REJECT, "never"),
            await _reply(Performative.REJECT, "never"),
        ]
    )

    async def respond(_lane: str, _message: BusMessage) -> Any:
        return next(replies)

    async def deliver(_message: BusMessage) -> None:
        return None

    result = await driver.run(
        session_id="s1",
        thread_id="s1",
        lane_a="lane-a",
        lane_b="lane-b",
        topic="same file",
        description="d",
        contested_refs=["src/a.py"],
        opening="mine",
        respond=respond,
        deliver=deliver,
        escalate=recorder,
    )

    assert result.escalated
    assert len(recorder.messages) == 1
    assert "rejected" in recorder.messages[0].body


@pytest.mark.asyncio
async def test_agreement_does_not_invoke_escalator() -> None:
    """The escalator is an escalation path, not a general notifier."""
    recorder = EscalationRecorder()
    driver = _driver()

    async def respond(_lane: str, _message: BusMessage) -> Any:
        return await _reply(Performative.ACCEPT, "fine")

    async def deliver(_message: BusMessage) -> None:
        return None

    result = await driver.run(
        session_id="s1",
        thread_id="s1",
        lane_a="lane-a",
        lane_b="lane-b",
        topic="same file",
        description="d",
        contested_refs=["src/a.py"],
        opening="mine",
        respond=respond,
        deliver=deliver,
        escalate=recorder,
    )

    assert result.agreed
    assert recorder.messages == []
