"""The negotiation driver — runs one ACP exchange to a decision.

A negotiation here is a *bounded* process, and the bound is the important part.
Two agents that disagree and keep countering will burn tokens indefinitely; the
design position is that **a human being asked a question is a better outcome
than two agents looping**. So the driver has a hard exchange cap, and hitting it
escalates rather than failing.

The escalation is visible to the whole session, not just the two lanes involved.
That matters because the third teammate may be the one who can break the tie —
and because a conflict nobody else can see is a conflict that surfaces as a
merge failure later, which is precisely what this system exists to prevent.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from openburrow.acp.performatives import (
    NegotiationPosition,
    build_performative_message,
    positions_from_moves,
    summarise_exchange,
    validate_reply,
)
from openburrow.core.errors import ACPNegotiationError
from openburrow.core.logging import bind_context, get_logger
from openburrow.core.models import (
    BusMessage,
    NegotiationExchange,
    NegotiationOutcome,
    NegotiationTrigger,
    Performative,
    TrustBoundary,
)

log = get_logger(__name__)

#: Asks a lane to respond to an incoming performative.
#: Returns the performative it chose, plus the reasoning and any concrete refs.
Responder = Callable[[str, BusMessage], Awaitable["ResponderReply"]]

#: Delivers a message to a lane (over the bus, or straight into a mock in tests).
Deliverer = Callable[[BusMessage], Awaitable[None]]

#: Broadcasts an escalation to every lane in the session.
Escalator = Callable[[BusMessage], Awaitable[None]]


@dataclass(slots=True)
class ResponderReply:
    """What a lane came back with."""

    performative: Performative
    summary: str
    refs: list[str]
    requested_change: str = ""
    body: str = ""
    #: False when the responder declined to engage at all.
    engaged: bool = True


@dataclass(slots=True)
class NegotiationResult:
    """The outcome of a completed exchange."""

    exchange: NegotiationExchange
    agreed: bool
    escalated: bool
    #: What the two lanes settled on, when they settled.
    resolution: str = ""

    @property
    def collision_avoided(self) -> bool:
        return self.exchange.collision_avoided

    def render(self) -> str:
        header = "AGREED" if self.agreed else "ESCALATED" if self.escalated else "UNRESOLVED"
        return f"[{header}] {self.exchange.topic}\n{summarise_exchange(self.exchange.moves)}"


class NegotiationDriver:
    """Drives a single exchange between two lanes."""

    def __init__(
        self,
        *,
        max_exchanges: int,
        escalate_after: int,
        escalate_to_humans: bool = True,
    ) -> None:
        self.max_exchanges = max(1, max_exchanges)
        self.escalate_after = max(1, min(escalate_after, self.max_exchanges))
        self.escalate_to_humans = escalate_to_humans

    async def run(
        self,
        *,
        session_id: str,
        thread_id: str,
        lane_a: str,
        lane_b: str,
        topic: str,
        description: str,
        contested_refs: list[str],
        opening: str,
        respond: Responder,
        deliver: Deliverer,
        escalate: Escalator | None = None,
        trigger: NegotiationTrigger = NegotiationTrigger.MERGE_RADAR,
        confidence: float = 0.0,
        task_id: str = "",
        trust_boundary: TrustBoundary = TrustBoundary.INTRA_REPO,
    ) -> NegotiationResult:
        """Run the exchange until agreement, rejection, or escalation.

        Alternates strictly between the two lanes, one performative each turn,
        so the transcript has a deterministic shape. Concurrency here would buy
        nothing and would make the transcript unreadable.
        """
        exchange = NegotiationExchange(
            session_id=session_id,
            task_id=task_id,
            trigger=trigger,
            lane_a=lane_a,
            lane_b=lane_b,
            topic=topic,
            description=description,
            contested_refs=contested_refs,
            predicted_conflict_confidence=confidence,
            max_exchanges=self.max_exchanges,
        )

        with bind_context(session_id=session_id, task_id=task_id):
            log.info(
                "negotiation.started",
                negotiation_id=exchange.id,
                lane_a=lane_a,
                lane_b=lane_b,
                topic=topic,
            )

            # --- opening move: lane A proposes -----------------------------
            opener = build_performative_message(
                performative=Performative.PROPOSE,
                sender_lane=lane_a,
                recipient_lane=lane_b,
                session_id=session_id,
                thread_id=thread_id,
                topic=topic,
                body=opening,
                refs=contested_refs,
                negotiation_id=exchange.id,
            )
            await deliver(opener)
            exchange.add_move(
                lane_id=lane_a,
                performative=Performative.PROPOSE,
                summary=opening[:200],
                refs=contested_refs,
                message_id=opener.id,
            )

            previous = Performative.PROPOSE
            current_lane = lane_b
            last_message = opener
            turns = 1

            async def escalate_and_finish(reason: str) -> NegotiationResult:
                # Every escalation funnels through here (item 138): the model
                # records it, and the whole session hears about it. A negotiation
                # that died quietly between two lanes is how a third teammate
                # first learns of the conflict as a merge failure a day later.
                exchange.escalate(reason=reason)
                if escalate is not None:
                    await escalate(
                        build_performative_message(
                            performative=Performative.INFORM,
                            sender_lane="",
                            recipient_lane="",
                            session_id=session_id,
                            thread_id=thread_id,
                            topic=topic,
                            body=f"negotiation escalated: {reason}",
                            refs=contested_refs,
                            negotiation_id=exchange.id,
                            payload={
                                "openburrow:escalation": True,
                                "openburrow:negotiationId": exchange.id,
                                "openburrow:escalationReason": reason,
                                "openburrow:lanes": [lane_a, lane_b],
                            },
                        )
                    )
                return await self._finish(exchange, agreed=False, escalated=True)

            # --- the exchange loop -----------------------------------------
            while turns < self.max_exchanges:
                reply = await respond(current_lane, last_message)

                if not reply.engaged:
                    return await escalate_and_finish(f"{current_lane} declined to engage")

                try:
                    validate_reply(previous, reply.performative)
                except ACPNegotiationError as exc:
                    log.warning(
                        "negotiation.illegal_move",
                        negotiation_id=exchange.id,
                        lane=current_lane,
                        error=str(exc),
                    )
                    # An illegal move is a protocol bug, not a disagreement.
                    # Escalating is the honest response; silently coercing it
                    # into a legal one would hide the bug.
                    return await escalate_and_finish(
                        f"illegal performative from {current_lane}: {exc}"
                    )

                message = build_performative_message(
                    performative=reply.performative,
                    sender_lane=current_lane,
                    recipient_lane=lane_a if current_lane == lane_b else lane_b,
                    session_id=session_id,
                    thread_id=thread_id,
                    topic=topic,
                    body=reply.body or reply.summary,
                    refs=reply.refs,
                    requested_change=reply.requested_change,
                    negotiation_id=exchange.id,
                )
                await deliver(message)
                exchange.add_move(
                    lane_id=current_lane,
                    performative=reply.performative,
                    summary=reply.summary,
                    refs=reply.refs,
                    requested_change=reply.requested_change,
                    message_id=message.id,
                )
                last_message = message
                turns += 1

                # --- terminal moves ----------------------------------------
                if reply.performative == Performative.ACCEPT:
                    exchange.resolve(
                        NegotiationOutcome.AGREED,
                        by=current_lane,
                        collision_avoided=True,
                    )
                    return await self._finish(
                        exchange,
                        agreed=True,
                        escalated=False,
                        resolution=reply.summary,
                    )

                if reply.performative == Performative.WITHDRAW:
                    exchange.resolve(NegotiationOutcome.ABANDONED, by=current_lane)
                    return await self._finish(exchange, agreed=False, escalated=False)

                if reply.performative == Performative.REJECT and turns >= self.escalate_after:
                    return await escalate_and_finish(f"{current_lane} rejected after {turns} turns")

                # --- escalate early if we are clearly stuck -----------------
                if turns >= self.escalate_after and self._is_stuck(exchange):
                    return await escalate_and_finish(f"positions unchanged after {turns} turns")

                previous = reply.performative
                current_lane = lane_a if current_lane == lane_b else lane_b

            # --- cap reached without agreement -----------------------------
            return await escalate_and_finish(
                f"exchange cap of {self.max_exchanges} reached without agreement",
            )

    # --- helpers -----------------------------------------------------------
    async def _finish(
        self,
        exchange: NegotiationExchange,
        *,
        agreed: bool,
        escalated: bool,
        resolution: str = "",
    ) -> NegotiationResult:
        if escalated and self.escalate_to_humans:
            exchange.outcome = NegotiationOutcome.ESCALATED
        log.info(
            "negotiation.finished",
            negotiation_id=exchange.id,
            outcome=str(exchange.outcome),
            turns=exchange.exchange_count,
            agreed=agreed,
            escalated=escalated,
        )
        return NegotiationResult(
            exchange=exchange,
            agreed=agreed,
            escalated=escalated,
            resolution=resolution,
        )

    @staticmethod
    def _is_stuck(exchange: NegotiationExchange) -> bool:
        """Have the last two moves said the same thing as the two before them?

        A cheap loop detector. Two identical counter-proposals in a row means
        neither side is moving, and another round will not change that.
        """
        if len(exchange.moves) < 4:
            return False
        recent = exchange.moves[-2:]
        earlier = exchange.moves[-4:-2]
        return all(
            r.performative == e.performative and r.summary == e.summary
            for r, e in zip(recent, earlier, strict=False)
        )

    def positions(
        self, exchange: NegotiationExchange
    ) -> tuple[NegotiationPosition, NegotiationPosition]:
        return positions_from_moves(exchange.moves, exchange.lane_a, exchange.lane_b)


__all__ = [
    "Deliverer",
    "Escalator",
    "NegotiationDriver",
    "NegotiationResult",
    "Responder",
    "ResponderReply",
]
