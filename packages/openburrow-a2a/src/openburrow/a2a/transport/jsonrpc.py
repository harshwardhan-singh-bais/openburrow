"""JSON-RPC 2.0 envelope and SSE framing.

OpenBurrow adopts A2A's transport rather than inventing one, so this module is
deliberately thin: it builds and validates spec-shaped envelopes and knows
nothing about sessions or lanes. Keeping the transport ignorant of the domain is
what makes it reusable for the third-party interop test (item 55), where the peer
on the other end has never heard of OpenBurrow.

Methods implemented, matching A2A's surface:

============================  ==============================================
``message/send``              send a message, get back a task or an update
``message/stream``            same, but streamed as SSE
``tasks/get``                 fetch a task by id
``tasks/cancel``              request cancellation
``tasks/pushNotificationConfig/set``  register a webhook (not supported yet)
``agent/getAuthenticatedExtendedCard``  richer card for authenticated callers
============================  ==============================================
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from openburrow.core.errors import A2AProtocolError

#: Standard JSON-RPC 2.0 error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

#: A2A-specific codes, in the space the spec reserves for applications.
TASK_NOT_FOUND = -32001
TASK_NOT_CANCELABLE = -32002
PUSH_NOT_SUPPORTED = -32003
UNSUPPORTED_OPERATION = -32004
CONTENT_TYPE_NOT_SUPPORTED = -32005
INVALID_AGENT_RESPONSE = -32006

A2A_METHODS: tuple[str, ...] = (
    "message/send",
    "message/stream",
    "tasks/get",
    "tasks/cancel",
    "tasks/resubscribe",
    "tasks/pushNotificationConfig/set",
    "tasks/pushNotificationConfig/get",
    "agent/getAuthenticatedExtendedCard",
)


@dataclass(slots=True)
class JsonRpcRequest:
    """A validated JSON-RPC request."""

    method: str
    params: dict[str, Any] = field(default_factory=dict)
    id: str | int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": self.id, "method": self.method, "params": self.params}

    @classmethod
    def parse(cls, payload: dict[str, Any] | str | bytes) -> JsonRpcRequest:
        if isinstance(payload, (str, bytes)):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise A2AProtocolError(
                    f"request body is not valid JSON: {exc}",
                    hint="JSON-RPC requires a JSON object body.",
                ) from exc

        if not isinstance(payload, dict):
            raise A2AProtocolError(
                f"JSON-RPC request must be an object, got {type(payload).__name__}"
            )
        if payload.get("jsonrpc") != "2.0":
            raise A2AProtocolError(
                "missing or wrong 'jsonrpc' version",
                hint='Every request must carry "jsonrpc": "2.0".',
                context={"received": payload.get("jsonrpc")},
            )
        method = payload.get("method")
        if not isinstance(method, str) or not method:
            raise A2AProtocolError("request is missing a 'method' string")

        params = payload.get("params") or {}
        if not isinstance(params, dict):
            raise A2AProtocolError(
                "JSON-RPC 'params' must be an object",
                context={"received_type": type(params).__name__},
            )
        return cls(method=method, params=params, id=payload.get("id"))

    @property
    def is_notification(self) -> bool:
        """A request with no id expects no response."""
        return self.id is None


@dataclass(slots=True)
class JsonRpcError:
    code: int
    message: str
    data: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data:
            payload["data"] = self.data
        return payload

    @classmethod
    def from_exception(cls, exc: Exception) -> JsonRpcError:
        """Map an OpenBurrow exception onto the closest JSON-RPC error code.

        Doing this in one place is what keeps error handling honest across the
        wire: a caller gets a stable code it can branch on, not a stack trace
        wrapped in a string.
        """
        from openburrow.core.errors import IllegalTaskTransitionError

        if isinstance(exc, IllegalTaskTransitionError):
            return cls(TASK_NOT_CANCELABLE, str(exc.message), exc.context or None)
        if isinstance(exc, A2AProtocolError):
            return cls(INVALID_PARAMS, str(exc.message), exc.context or None)
        return cls(INTERNAL_ERROR, "internal error", {"type": type(exc).__name__})


def make_request(
    method: str, params: dict[str, Any], *, request_id: str | None = None
) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id or uuid.uuid4().hex[:16],
        "method": method,
        "params": params,
    }


def make_response(result: Any, *, request_id: str | int | None) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def make_error(
    code: int,
    message: str,
    *,
    request_id: str | int | None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }
    if data:
        payload["error"]["data"] = data
    return payload


# ---------------------------------------------------------------------------
# SSE framing
# ---------------------------------------------------------------------------
def sse_frame(
    data: dict[str, Any] | str,
    *,
    event: str = "message",
    event_id: str | None = None,
    retry_ms: int | None = None,
) -> str:
    """Serialise one Server-Sent Event.

    Kept as an explicit function rather than delegating to a framework helper so
    the exact wire bytes are testable — SSE's newline rules are easy to get
    subtly wrong, and a malformed frame is invisible until a peer rejects it.
    """
    body = data if isinstance(data, str) else json.dumps(data, separators=(",", ":"))
    lines: list[str] = []
    if event:
        lines.append(f"event: {event}")
    if event_id:
        lines.append(f"id: {event_id}")
    if retry_ms is not None:
        lines.append(f"retry: {retry_ms}")
    for line in body.splitlines() or [""]:
        lines.append(f"data: {line}")
    return "\n".join(lines) + "\n\n"


def parse_sse_frames(raw: str) -> list[dict[str, Any]]:
    """Parse an SSE byte stream into structured frames.

    Tolerates the two forms a peer may send: ``data:`` lines (the common case)
    and comment/keepalive lines beginning with ``:``, which are discarded.
    """
    frames: list[dict[str, Any]] = []
    for raw_block in raw.split("\n\n"):
        block = raw_block.strip()
        if not block or block.startswith(":"):
            continue
        frame: dict[str, Any] = {"event": "message", "id": None, "data": []}
        for line in block.splitlines():
            if line.startswith(":"):
                continue
            field_name, _, value = line.partition(":")
            value = value.lstrip(" ")
            if field_name == "event":
                frame["event"] = value
            elif field_name == "id":
                frame["id"] = value
            elif field_name == "data":
                frame["data"].append(value)
        joined = "\n".join(frame["data"])
        try:
            frame["data"] = json.loads(joined) if joined else None
        except json.JSONDecodeError:
            frame["data"] = joined
        frames.append(frame)
    return frames


async def iterate_sse(response: Any) -> AsyncIterator[dict[str, Any]]:
    """Yield parsed SSE frames from an ``httpx`` streaming response.

    Buffers on the double-newline boundary, because a chunk boundary can fall
    mid-frame and a naive per-chunk parse silently drops events.
    """
    buffer = ""
    async for chunk in response.aiter_text():
        buffer += chunk
        while "\n\n" in buffer:
            raw_frame, _, buffer = buffer.partition("\n\n")
            for frame in parse_sse_frames(raw_frame + "\n\n"):
                yield frame


__all__ = [
    "A2A_METHODS",
    "CONTENT_TYPE_NOT_SUPPORTED",
    "INTERNAL_ERROR",
    "INVALID_PARAMS",
    "INVALID_REQUEST",
    "METHOD_NOT_FOUND",
    "PARSE_ERROR",
    "PUSH_NOT_SUPPORTED",
    "TASK_NOT_CANCELABLE",
    "TASK_NOT_FOUND",
    "UNSUPPORTED_OPERATION",
    "JsonRpcError",
    "JsonRpcRequest",
    "iterate_sse",
    "make_error",
    "make_request",
    "make_response",
    "parse_sse_frames",
    "sse_frame",
]
