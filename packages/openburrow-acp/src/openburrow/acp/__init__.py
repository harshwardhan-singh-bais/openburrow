"""OpenBurrow's ACP layer.

A2A specifies how agents exchange messages. It does not specify the *semantics
of disagreement* — what it means to make a proposal, counter it, or accept it.
ACP does, by deriving a typed performative set from FIPA-ACL.

OpenBurrow borrows that vocabulary verbatim rather than inventing verbs, so a
negotiation transcript reads the way the protocol family intends:

    propose → counter → counter → accept

The layer is intentionally small. There is no mature ACP SDK, the performative
set is six values, and hand-rolling them as Pydantic models is cheaper and
clearer than vendoring a half-finished implementation.
"""

from openburrow.acp.negotiation import (
    Deliverer,
    Escalator,
    NegotiationDriver,
    NegotiationResult,
    Responder,
    ResponderReply,
)
from openburrow.acp.performatives import (
    CONTINUING_PERFORMATIVES,
    TERMINAL_PERFORMATIVES,
    NegotiationPosition,
    build_performative_message,
    move_from_message,
    positions_from_moves,
    summarise_exchange,
    validate_reply,
)

__all__ = [
    "CONTINUING_PERFORMATIVES",
    "TERMINAL_PERFORMATIVES",
    "Deliverer",
    "Escalator",
    "NegotiationDriver",
    "NegotiationPosition",
    "NegotiationResult",
    "Responder",
    "ResponderReply",
    "build_performative_message",
    "move_from_message",
    "positions_from_moves",
    "summarise_exchange",
    "validate_reply",
]
