"""ACP performatives — typed negotiation moves.

OpenBurrow does not invent negotiation verbs. ACP derives its performative set
from FIPA-ACL, and those verbs already mean something precise to anyone who has
read the family of specs. Reusing them means a negotiation transcript is
readable to a reviewer who has never seen OpenBurrow.

The six performatives and what they commit the sender to:

============  ==================================================================
``propose``   "Here is a concrete change. I will act on it if you accept."
``counter``   "Not that change — this one. The proposal is still live."
``accept``    "Agreed. Proceed." (terminal for this exchange)
``reject``    "No. Here is why." (terminal once the exchange cap is reached)
``inform``    "A statement of fact. No action requested."
``withdraw``  "I retract my outstanding proposal." (terminal)
============  ==================================================================

The value of typing these rather than passing strings is that illegal sequences
become unrepresentable. A ``counter`` in reply to an ``accept`` is not a
judgement call — it is a protocol violation, and :func:`validate_reply` says so.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from openburrow.core.errors import ACPNegotiationError
from openburrow.core.models import (
    PERFORMATIVE_REPLIES,
    BusMessage,
    MessagePriority,
    NegotiationMove,
    Performative,
)

#: Performatives that end an exchange, whatever the exchange count.
TERMINAL_PERFORMATIVES: frozenset[Performative] = frozenset(
    {Performative.ACCEPT, Performative.WITHDRAW}
)

#: Performatives that keep an exchange alive.
CONTINUING_PERFORMATIVES: frozenset[Performative] = frozenset(
    {Performative.PROPOSE, Performative.COUNTER, Performative.REJECT}
)


@dataclass(slots=True)
class NegotiationPosition:
    """One lane's stance in an exchange.

    Kept explicit rather than inferred from the transcript, because the
    escalation path needs to summarise "what does each side want?" and reading
    that out of a message history with a heuristic is exactly the kind of
    guesswork this project exists to avoid.
    """

    lane_id: str
    proposal: str = ""
    #: Concrete, addressable things this lane insists on. ``file:line``,
    #: ``symbol``, or ``step_id`` — never prose.
    refs: list[str] = field(default_factory=list)
    #: What the lane is willing to give up, if anything.
    concessions: list[str] = field(default_factory=list)
    #: True once the lane has accepted; a lane that accepted should not counter.
    accepted: bool = False
    rejected: bool = False

    def summarise(self) -> str:
        parts = [f"{self.lane_id}:"]
        if self.accepted:
            parts.append("ACCEPTED")
        elif self.rejected:
            parts.append("REJECTED")
        else:
            parts.append("PROPOSES")
        if self.proposal:
            parts.append(self.proposal)
        if self.refs:
            parts.append(f"(refs: {', '.join(self.refs[:4])})")
        return " ".join(parts)


def validate_reply(previous: Performative | None, reply: Performative) -> None:
    """Raise if ``reply`` is not a legal answer to ``previous``.

    ``previous=None`` means the exchange is opening, where only ``propose`` and
    ``inform`` are legal — you cannot accept a proposal nobody made.
    """
    if previous is None:
        if reply not in {Performative.PROPOSE, Performative.INFORM}:
            raise ACPNegotiationError(
                f"cannot open a negotiation with '{reply}'",
                hint="Open with 'propose' (or 'inform' to state a fact first).",
                context={"reply": str(reply)},
            )
        return

    allowed = PERFORMATIVE_REPLIES[previous]
    if reply not in allowed:
        raise ACPNegotiationError(
            f"'{reply}' is not a legal reply to '{previous}'",
            hint=f"Legal replies to '{previous}': {', '.join(sorted(allowed))}.",
            context={"previous": str(previous), "reply": str(reply)},
        )


def build_performative_message(
    *,
    performative: Performative,
    sender_lane: str,
    recipient_lane: str,
    session_id: str,
    thread_id: str,
    topic: str,
    body: str,
    refs: list[str] | None = None,
    requested_change: str = "",
    task_id: str = "",
    negotiation_id: str = "",
    payload: dict[str, Any] | None = None,
) -> BusMessage:
    """Build the :class:`BusMessage` that carries one performative.

    Priority is derived from the performative rather than passed in, because
    getting it wrong is a real bug: a ``propose`` marked informative would let
    the receiving harness keep working past a decision it needs to make.
    """
    blocking = performative in {Performative.PROPOSE, Performative.COUNTER}
    # An empty recipient means the whole session (item 138's escalation
    # broadcast is the in-repo user); every real move names its lane.
    return BusMessage(
        session_id=session_id,
        thread_id=thread_id,
        task_id=task_id,
        sender_lane=sender_lane,
        recipients=[recipient_lane] if recipient_lane else [],
        broadcast=not recipient_lane,
        subject=topic,
        body=body,
        intent=performative,
        priority=MessagePriority.BLOCKING if blocking else MessagePriority.INFORMATIVE,
        requires_reply=blocking,
        payload={
            "openburrow:refs": refs or [],
            "openburrow:requestedChange": requested_change,
            "openburrow:negotiationId": negotiation_id,
            **(payload or {}),
        },
    )


def move_from_message(message: BusMessage) -> NegotiationMove:
    """Convert a negotiation message into a transcript entry."""
    payload = message.payload or {}
    return NegotiationMove(
        message_id=message.id,
        lane_id=message.sender_lane,
        performative=Performative(str(message.intent)),
        summary=message.subject or message.body[:200],
        refs=list(payload.get("openburrow:refs") or []),
        requested_change=str(payload.get("openburrow:requestedChange") or ""),
    )


def summarise_exchange(moves: list[NegotiationMove]) -> str:
    """One-line rendering of an exchange, for the TUI alerts strip and Slack.

    Item 223 needs both harnesses' positions summarised in a notification, and
    the summariser is the same one the terminal uses — so a Slack message and a
    terminal alert cannot disagree about what happened.
    """
    if not moves:
        return "no exchanges yet"
    parts = [f"{m.lane_id} {m.performative}" for m in moves]
    tail = ""
    if moves[-1].requested_change:
        tail = f" — outstanding ask: {moves[-1].requested_change}"
    return " → ".join(parts) + tail


def positions_from_moves(
    moves: list[NegotiationMove], lane_a: str, lane_b: str
) -> tuple[NegotiationPosition, NegotiationPosition]:
    """Derive each lane's current position from the transcript."""
    a = NegotiationPosition(lane_id=lane_a)
    b = NegotiationPosition(lane_id=lane_b)
    for move in moves:
        target = a if move.lane_id == lane_a else b if move.lane_id == lane_b else None
        if target is None:
            continue
        if move.performative in {Performative.PROPOSE, Performative.COUNTER}:
            target.proposal = move.summary
            target.refs = list(move.refs)
        elif move.performative == Performative.ACCEPT:
            target.accepted = True
        elif move.performative == Performative.REJECT:
            target.rejected = True
    return a, b


__all__ = [
    "CONTINUING_PERFORMATIVES",
    "TERMINAL_PERFORMATIVES",
    "NegotiationPosition",
    "build_performative_message",
    "move_from_message",
    "positions_from_moves",
    "summarise_exchange",
    "validate_reply",
]
