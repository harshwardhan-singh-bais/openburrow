"""OpenBurrow's A2A layer.

The bus speaks A2A rather than a bespoke format, for a reason that is worth
restating: a bespoke protocol is only interoperable with itself. By fronting
every lane with a standard A2A server — Agent Card at a well-known path,
JSON-RPC for messages, the eight-state task lifecycle — OpenBurrow inherits the
emerging standard instead of competing with it.

Three parts:

* :mod:`~openburrow.a2a.card`      — Agent Card generation from harness capabilities
* :mod:`~openburrow.a2a.lifecycle` — the task state machine, bound to the bus log
* :mod:`~openburrow.a2a.transport` — JSON-RPC 2.0 envelopes and SSE framing
* :mod:`~openburrow.a2a.server`    — one A2A server per lane
* :mod:`~openburrow.a2a.client`    — outbound peer communication

What this layer does **not** do: it does not decide who may delegate to whom,
what authority transfers, or how a cross-boundary action is audited. Those are
open questions in A2A itself, and they are answered one layer up in
``openburrow-governance``.
"""

from openburrow.a2a.card import (
    HarnessCapabilities,
    SkillSpec,
    build_agent_card,
    declared_skills,
)
from openburrow.a2a.client import BusClientPool, PeerClient, card_is_conformant
from openburrow.a2a.lifecycle import TaskLifecycleManager
from openburrow.a2a.server import LaneA2AServer, start_lane_servers
from openburrow.a2a.transport import (
    A2A_METHODS,
    JsonRpcError,
    JsonRpcRequest,
    make_request,
    parse_sse_frames,
    sse_frame,
)

__all__ = [
    "A2A_METHODS",
    "BusClientPool",
    "HarnessCapabilities",
    "JsonRpcError",
    "JsonRpcRequest",
    "LaneA2AServer",
    "PeerClient",
    "SkillSpec",
    "TaskLifecycleManager",
    "build_agent_card",
    "card_is_conformant",
    "declared_skills",
    "make_request",
    "parse_sse_frames",
    "sse_frame",
    "start_lane_servers",
]
