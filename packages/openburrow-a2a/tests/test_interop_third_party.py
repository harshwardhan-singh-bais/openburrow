"""Third-party A2A interop (roadmap item 55).

The standard-grounding claim is only worth what this test can prove: that the
bus speaks real A2A, not a bespoke protocol with an A2A veneer. Both directions
are exercised, because "interop" that only works outbound would be a client
library, not a protocol:

**Inbound** — a client that has never heard of OpenBurrow speaks raw JSON-RPC
2.0 at a live lane server: discovers the Agent Card at the well-known path,
sends a message carrying no ``openburrow:`` metadata at all, and walks the task
lifecycle through ``tasks/get`` and ``tasks/cancel``. The helpers in
:mod:`openburrow.a2a.transport` are deliberately NOT used to build requests —
a conformance test that reuses the implementation under test proves nothing.

**Outbound** — :class:`~openburrow.a2a.client.PeerClient` talks to a foreign
A2A agent: a minimal Starlette app that serves a spec-shaped card and answers
``message/send`` with a task envelope no OpenBurrow model produced.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import httpx
import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from openburrow.a2a import LaneA2AServer
from openburrow.a2a.client import PeerClient
from openburrow.core.models import A2ATask, BusMessage, Lane, LaneRole, Performative, TaskState

pytestmark = pytest.mark.integration


def make_lane(name: str = "interop") -> Lane:
    return Lane(name=name, harness="mock", session_id="sess_interop", role=LaneRole.IMPLEMENTER)


def jsonrpc(method: str, params: dict[str, Any], request_id: str) -> dict[str, Any]:
    """Hand-built JSON-RPC 2.0 envelope — the shape a spec reader writes."""
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}


def a2a_message_params(text: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    """The A2A ``message/send`` params shape, with no OpenBurrow extensions."""
    params: dict[str, Any] = {
        "message": {"role": "user", "parts": [{"kind": "text", "text": text}]}
    }
    if metadata:
        params["message"]["metadata"] = metadata
    return params


# ---------------------------------------------------------------------------
# Inbound: a third-party client against a live OpenBurrow lane
# ---------------------------------------------------------------------------
class TestForeignClientToOpenBurrowLane:
    @pytest.fixture()
    async def server(self) -> AsyncGenerator[LaneA2AServer, None]:
        received: list[BusMessage] = []
        holder: dict[str, A2ATask] = {}

        async def on_message(message: BusMessage) -> A2ATask | None:
            received.append(message)
            task = A2ATask(
                session_id=message.session_id,
                title="interop",
                instruction=message.body,
                requester_lane=message.sender_lane,
                assignee_lane="interop",
            )
            holder[task.id] = task
            return task

        async def fetch(task_id: str) -> A2ATask | None:
            return holder.get(task_id)

        async def cancel(task_id: str, reason: str) -> A2ATask | None:
            task = holder.get(task_id)
            if task is None:
                return None
            task.transition(TaskState.CANCELED, reason=reason)
            return task

        srv = LaneA2AServer(
            make_lane(),
            on_message=on_message,
            fetch_task=fetch,
            cancel_task=cancel,
            port=0,
        )
        await srv.start()
        yield srv
        await srv.stop()

    async def test_card_is_discoverable_at_the_well_known_path(self, server: LaneA2AServer) -> None:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(server.card_url)
        assert response.status_code == 200
        card = response.json()
        for field in ("name", "url", "skills", "capabilities"):
            assert field in card, f"agent card is missing A2A field {field!r}"
        assert card["url"] == server.base_url
        assert isinstance(card["skills"], list) and card["skills"]

    async def test_foreign_message_without_openburrow_metadata_is_accepted(
        self, server: LaneA2AServer
    ) -> None:
        """A client that has never heard of OpenBurrow sends no extension keys."""
        request_id = uuid.uuid4().hex
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(
                server.base_url,
                json=jsonrpc(
                    "message/send", a2a_message_params("hello from a foreign agent"), request_id
                ),
            )
        body = response.json()
        assert response.status_code == 200, body
        assert body["jsonrpc"] == "2.0"
        assert body["id"] == request_id
        assert "error" not in body
        assert body["result"]["task"]["state"] == str(TaskState.SUBMITTED)

    async def test_task_lifecycle_is_walkable_over_the_wire(self, server: LaneA2AServer) -> None:
        async with httpx.AsyncClient(timeout=5.0) as client:
            sent = await client.post(
                server.base_url,
                json=jsonrpc("message/send", a2a_message_params("do a thing"), "id-1"),
            )
            task_id = sent.json()["result"]["task"]["id"]

            fetched = await client.post(
                server.base_url, json=jsonrpc("tasks/get", {"id": task_id}, "id-2")
            )
            assert fetched.json()["result"]["id"] == task_id

            canceled = await client.post(
                server.base_url,
                json=jsonrpc("tasks/cancel", {"id": task_id, "reason": "done"}, "id-3"),
            )
            assert canceled.json()["result"]["state"] == str(TaskState.CANCELED)

    async def test_unknown_task_maps_to_the_spec_error_code(self, server: LaneA2AServer) -> None:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(
                server.base_url, json=jsonrpc("tasks/get", {"id": "nope"}, "id-4")
            )
        body = response.json()
        assert body["error"]["code"] == -32001  # A2A TASK_NOT_FOUND

    async def test_malformed_body_is_a_parse_error(self, server: LaneA2AServer) -> None:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(
                server.base_url, content=b"not json", headers={"content-type": "application/json"}
            )
        body = response.json()
        assert body["error"]["code"] == -32700  # PARSE_ERROR

    async def test_unknown_method_is_method_not_found(self, server: LaneA2AServer) -> None:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(
                server.base_url, json=jsonrpc("no/such/method", {}, "id-5")
            )
        body = response.json()
        assert body["error"]["code"] == -32601  # METHOD_NOT_FOUND


# ---------------------------------------------------------------------------
# Outbound: OpenBurrow's PeerClient against a foreign A2A agent
# ---------------------------------------------------------------------------
_FOREIGN_TASK: dict[str, Any] = {
    "id": "foreign-task-1",
    "sessionId": "sess_foreign",
    "title": "foreign work",
    "instruction": "did it",
    "state": "completed",
    "requesterLane": "their-agent",
    "assigneeLane": "our-lane",
}


def foreign_agent_app() -> Starlette:
    """A minimal third-party A2A agent: spec-shaped, zero OpenBurrow imports."""

    async def card(_request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "name": "Foreign Agent",
                "url": "http://set-at-startup",
                "version": "1.0.0",
                "capabilities": {"streaming": False},
                "skills": [{"id": "echo", "name": "Echo", "description": "echoes"}],
            }
        )

    async def jsonrpc(request: Request) -> JSONResponse:
        payload = await request.json()
        if payload.get("method") == "message/send":
            return JSONResponse(
                {"jsonrpc": "2.0", "id": payload.get("id"), "result": {"task": _FOREIGN_TASK}}
            )
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": payload.get("id"),
                "error": {"code": -32601, "message": "method not found"},
            }
        )

    return Starlette(
        routes=[
            Route("/.well-known/agent-card.json", card, methods=["GET"]),
            Route("/", jsonrpc, methods=["POST"]),
        ]
    )


class TestOpenBurrowClientToForeignAgent:
    @pytest.fixture()
    async def foreign_base_url(self) -> AsyncGenerator[str, None]:
        config = uvicorn.Config(
            foreign_agent_app(), host="127.0.0.1", port=0, log_level="warning", lifespan="off"
        )
        server = uvicorn.Server(config)
        task = asyncio.create_task(server.serve())
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.02)
        yield f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
        server.should_exit = True
        await task

    async def test_card_discovery(self, foreign_base_url: str) -> None:
        async with PeerClient(foreign_base_url) as peer:
            card = await peer.fetch_card()
        assert card["name"] == "Foreign Agent"
        assert "echo" in await peer.declared_skills()

    async def test_send_message_parses_the_foreign_task(self, foreign_base_url: str) -> None:
        message = BusMessage(
            session_id="sess_foreign",
            sender_lane="our-lane",
            body="please do a thing",
            intent=Performative.PROPOSE,
        )
        async with PeerClient(foreign_base_url) as peer:
            task = await peer.send_message(message)
        assert task is not None
        assert task.id == "foreign-task-1"
        assert task.state == TaskState.COMPLETED

    async def test_transport_failure_is_reported_not_swallowed(self) -> None:
        peer = PeerClient("http://127.0.0.1:1", max_retries=0)
        try:
            assert await peer.is_reachable() is False
        finally:
            await peer.close()
