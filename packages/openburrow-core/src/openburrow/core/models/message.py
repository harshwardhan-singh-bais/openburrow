"""Bus messages and negotiation exchanges.

A **BusMessage** is the envelope that travels between lanes. It is deliberately
protocol-shaped: the fields map one-to-one onto an A2A message plus the ACP
``intent`` performative, so serialising one to the wire is a field rename rather
than a translation layer.

A **NegotiationExchange** is a bounded sequence of performatives between two
lanes working out a conflict. It has an explicit maximum length, and hitting
that maximum is an *escalation*, not a failure — the design assumption is that
two agents looping forever is worse than a human being asked a question.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from pydantic import Field, computed_field, field_validator

from openburrow.core.models.base import BurrowModel, ensure_aware, now
from openburrow.core.models.enums import (
    MessagePriority,
    NegotiationOutcome,
    NegotiationTrigger,
    Performative,
    TrustBoundary,
    Urgency,
)

#: Above this, a message body is summarised rather than carried whole.
INLINE_BODY_LIMIT = 8192


class BusMessage(BurrowModel):
    """One message on the A2A bus.

    The field split that matters: ``body`` is what a *human* reads, and
    ``payload`` is what the *receiving adapter* consumes. Keeping them separate
    is what lets one message be simultaneously readable in the TUI and
    mechanically injectable into a harness that wants structured input.
    """

    id_kind: ClassVar[str] = "message"

    session_id: str = ""
    thread_id: str = ""
    task_id: str = ""

    # --- addressing --------------------------------------------------------
    sender_lane: str = ""
    sender_harness: str = ""
    recipients: list[str] = Field(default_factory=list)
    #: Empty recipients + ``broadcast`` means everyone in the session.
    broadcast: bool = False
    #: Set when a human typed this into the thread rather than a harness.
    sender_human: str = ""
    reply_to: str = ""

    # --- semantics ---------------------------------------------------------
    #: ACP performative. ``INFORM`` for plain chatter, the rest for negotiation.
    intent: Performative = Performative.INFORM
    priority: MessagePriority = MessagePriority.INFORMATIVE
    urgency: Urgency = Urgency.NORMAL

    subject: str = ""
    body: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)

    # --- provenance / governance ------------------------------------------
    trust_boundary: TrustBoundary = TrustBoundary.INTRA_REPO
    delegation_id: str = ""
    #: Signature over the canonical body, when ``A2A_SIGN_MESSAGES`` is on.
    signature: str = ""
    #: Secrets redaction already applied (item 201).
    scrubbed: bool = False

    # --- lifecycle ---------------------------------------------------------
    delivered_at: datetime | None = None
    acknowledged_at: datetime | None = None
    #: Blocking messages hold the receiving task in ``input_required``.
    requires_reply: bool = False
    expires_at: datetime | None = None

    # --- knowledge flags ---------------------------------------------------
    #: Tagged by a harness or the classifier as worth promoting (items 107, 140).
    brain_worthy: bool = False
    lesson_worthy: bool = False

    @field_validator("body")
    @classmethod
    def _cap_body(cls, value: str) -> str:
        if len(value) > INLINE_BODY_LIMIT:
            return value[:INLINE_BODY_LIMIT] + "\n… [truncated; see payload/blob]"
        return value

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_negotiation(self) -> bool:
        """A performative other than INFORM means this message is a negotiation move."""
        return self.intent != Performative.INFORM

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_from_human(self) -> bool:
        return bool(self.sender_human)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and now() > ensure_aware(self.expires_at)

    def target_lanes(self, session_lanes: list[str]) -> list[str]:
        """Resolve recipients against the session's live lanes."""
        if self.broadcast:
            return [lane for lane in session_lanes if lane != self.sender_lane]
        return [lane for lane in self.recipients if lane in session_lanes]

    def mark_delivered(self) -> None:
        self.delivered_at = now()
        self.touch()

    def mark_acknowledged(self) -> None:
        self.acknowledged_at = now()
        self.touch()

    def preview(self, width: int = 80) -> str:
        """One-line rendering for the TUI feed and `burrow watch`."""
        who = self.sender_human or self.sender_lane or "unknown"
        head = f"[{self.intent}] {who}"
        if self.subject:
            head += f": {self.subject}"
        text = self.body.replace("\n", " ").strip()
        room = max(0, width - len(head) - 3)
        return f"{head} — {text[:room]}…" if len(text) > room else f"{head} — {text}"

    @classmethod
    def inform(
        cls,
        *,
        sender_lane: str,
        recipients: list[str],
        subject: str,
        body: str,
        session_id: str = "",
        thread_id: str = "",
        **extra: Any,
    ) -> BusMessage:
        """Convenience constructor for the common "just telling you" case."""
        return cls(
            sender_lane=sender_lane,
            recipients=recipients,
            subject=subject,
            body=body,
            intent=Performative.INFORM,
            session_id=session_id,
            thread_id=thread_id,
            **extra,
        )

    @classmethod
    def propose(
        cls,
        *,
        sender_lane: str,
        recipient_lane: str,
        subject: str,
        body: str,
        payload: dict[str, Any] | None = None,
        session_id: str = "",
        thread_id: str = "",
    ) -> BusMessage:
        """Open a negotiation. Always blocking — a proposal needs an answer."""
        return cls(
            sender_lane=sender_lane,
            recipients=[recipient_lane],
            subject=subject,
            body=body,
            payload=payload or {},
            intent=Performative.PROPOSE,
            priority=MessagePriority.BLOCKING,
            requires_reply=True,
            session_id=session_id,
            thread_id=thread_id,
        )


class NegotiationMove(BurrowModel):
    """One performative inside an exchange, with the reasoning attached."""

    id_kind: ClassVar[str] = "message"

    message_id: str = ""
    lane_id: str = ""
    performative: Performative = Performative.INFORM
    summary: str = ""
    #: Concrete, addressable references — file:line, symbol, step id. Item 45
    #: requires structured feedback rather than prose, so this is not optional
    #: in practice even though the schema permits it.
    refs: list[str] = Field(default_factory=list)
    #: The specific change being asked for, in the receiver's own terms.
    requested_change: str = ""
    at: datetime = Field(default_factory=now)


class NegotiationExchange(BurrowModel):
    """A bounded back-and-forth resolving one conflict between two lanes.

    The cap is the design: ``max_exchanges`` reached without agreement escalates
    to a human rather than continuing to burn tokens on a disagreement the two
    agents are not going to resolve. Item 137 makes that explicit, and this model
    is where it is enforced.
    """

    id_kind: ClassVar[str] = "negotiation"

    session_id: str = ""
    task_id: str = ""
    trigger: NegotiationTrigger = NegotiationTrigger.MERGE_RADAR

    lane_a: str = ""
    lane_b: str = ""
    #: What they actually disagree about — the conflict's subject, not its text.
    topic: str = ""
    description: str = ""
    #: Files or symbols in dispute.
    contested_refs: list[str] = Field(default_factory=list)

    moves: list[NegotiationMove] = Field(default_factory=list)
    outcome: NegotiationOutcome = NegotiationOutcome.PENDING
    #: Set when the Radar opened this, so we can score whether it was worthwhile.
    predicted_conflict_confidence: float = 0.0
    #: Whether the conflict would have happened without this exchange.
    collision_avoided: bool = False
    resolved_by: str = ""
    escalation_message_id: str = ""
    max_exchanges: int = 6
    started_at: datetime = Field(default_factory=now)
    ended_at: datetime | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def exchange_count(self) -> int:
        return len(self.moves)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_open(self) -> bool:
        return self.outcome == NegotiationOutcome.PENDING

    @computed_field  # type: ignore[prop-decorator]
    @property
    def should_escalate(self) -> bool:
        return self.is_open and self.exchange_count >= self.max_exchanges

    @property
    def duration_seconds(self) -> float:
        end = ensure_aware(self.ended_at) if self.ended_at else now()
        return (end - ensure_aware(self.started_at)).total_seconds()

    def add_move(
        self,
        *,
        lane_id: str,
        performative: Performative,
        summary: str,
        refs: list[str] | None = None,
        requested_change: str = "",
        message_id: str = "",
    ) -> NegotiationMove:
        move = NegotiationMove(
            lane_id=lane_id,
            performative=performative,
            summary=summary,
            refs=refs or [],
            requested_change=requested_change,
            message_id=message_id,
        )
        self.moves.append(move)
        self.touch()

        if performative == Performative.ACCEPT:
            self.resolve(NegotiationOutcome.AGREED, by=lane_id)
        elif performative == Performative.REJECT and self.exchange_count >= self.max_exchanges:
            self.resolve(NegotiationOutcome.REJECTED, by=lane_id)
        return move

    def resolve(
        self,
        outcome: NegotiationOutcome,
        *,
        by: str = "",
        collision_avoided: bool = False,
    ) -> None:
        self.outcome = outcome
        self.resolved_by = by
        self.collision_avoided = collision_avoided
        self.ended_at = now()
        self.touch()

    def escalate(self, *, message_id: str = "", reason: str = "") -> None:
        self.outcome = NegotiationOutcome.ESCALATED
        self.escalation_message_id = message_id
        if reason:
            self.description = f"{self.description}\n[escalated] {reason}".strip()
        self.ended_at = now()
        self.touch()

    def transcript(self) -> str:
        """Plain-text rendering used by the CLI, the Slack notifier, and exports."""
        lines = [
            f"Negotiation {self.id} — {self.topic or self.description}",
            f"  between {self.lane_a} and {self.lane_b}",
            f"  trigger: {self.trigger}, outcome: {self.outcome}",
            "",
        ]
        for index, move in enumerate(self.moves, start=1):
            lines.append(f"  {index}. [{move.performative}] {move.lane_id}: {move.summary}")
            if move.requested_change:
                lines.append(f"     → wants: {move.requested_change}")
            if move.refs:
                lines.append(f"     refs: {', '.join(move.refs[:5])}")
        return "\n".join(lines)


__all__ = ["INLINE_BODY_LIMIT", "BusMessage", "NegotiationExchange", "NegotiationMove"]
