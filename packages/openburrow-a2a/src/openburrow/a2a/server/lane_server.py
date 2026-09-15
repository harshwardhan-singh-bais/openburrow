"""Per-lane A2A server.

Every lane gets one of these, bound to ``127.0.0.1:(port_base + lane_index)``.
From the outside, the lane *is* an A2A agent: a peer fetches the Agent Card,
sees the skills, and posts JSON-RPC to the same URL. It does not know — and
cannot tell — that behind the endpoint sits a Claude Code PTY, a Codex process,
or a mock adapter in a test.

That opacity is the design goal. It is what makes the third-party interop test
(item 55) meaningful: an external A2A agent talking to a lane is not talking to
a bespoke OpenBurrow protocol with an A2A veneer, it is using the same surface
it would use against any other agent.

The server deliberately does **not** own task state. It translates inbound A2A
calls into bus events and hands them to the injector; the
:class:`~openburrow.a2a.lifecycle.TaskLifecycleManager` owns the state machine.
A server that also owned state would be a second source of truth, which is the
one thing the architecture forbids.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from openburrow.a2a.card import HarnessCapabilities, SkillSpec, build_agent_card
from openburrow.a2a.transport import (
    INTERNAL_ERROR,
    INVALID_REQUEST,
    TASK_NOT_FOUND,
    JsonRpcError,
    JsonRpcRequest,
    make_error,
    make_response,
    sse_frame,
)
from openburrow.core.errors import A2AProtocolError, OpenBurrowError
from openburrow.core.logging import bind_context, get_logger
from openburrow.core.models import A2ATask, BusMessage, Lane

log = get_logger(__name__)

#: Signature of the handler an adapter supplies for inbound messages.
MessageHandler = Callable[[BusMessage], Awaitable[A2ATask | None]]
TaskFetcher = Callable[[str], Awaitable[A2ATask | None]]
TaskCanceller = Callable[[str, str], Awaitable[A2ATask | None]]


class LaneA2AServer:
    """Serves one lane's Agent Card and JSON-RPC endpoint."""

    def __init__(
        self,
        lane: Lane,
        *,
        capabilities: HarnessCapabilities | None = None,
        skills: list[SkillSpec] | None = None,
        host: str = "127.0.0.1",
        port: int = 0,
        on_message: MessageHandler | None = None,
        fetch_task: TaskFetcher | None = None,
        cancel_task: TaskCanceller | None = None,
        card_path: str = "/.well-known/agent-card.json",
    ) -> None:
        self.lane = lane
        self.capabilities = capabilities or HarnessCapabilities()
        self.skills = skills or []
        self.host = host
        self.port = port
        self.card_path = card_path
        self._on_message = on_message
        self._fetch_task = fetch_task
        self._cancel_task = cancel_task
        self._server: Any = None
        self._app: Starlette | None = None
        #: Subscribers waiting on SSE. Each is an asyncio.Queue of frames.
        self._streams: set[asyncio.Queue[dict[str, Any]]] = set()

    # --- lifecycle ---------------------------------------------------------
    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def card_url(self) -> str:
        return f"{self.base_url}{self.card_path}"

    def build_app(self) -> Starlette:
        """Construct the ASGI app. Idempotent — safe to call from tests."""
        if self._app is not None:
            return self._app

        async def agent_card(_request: Request) -> JSONResponse:
            card = build_agent_card(
                self.lane,
                capabilities=self.capabilities,
                skills=self.skills,
                base_url=self.base_url,
            )
            return JSONResponse(card, headers={"Cache-Control": "no-store"})

        async def jsonrpc(request: Request) -> Response:
            return await self._handle_jsonrpc(request)

        async def stream(request: Request) -> Response:
            return await self._handle_stream(request)

        async def health(_request: Request) -> JSONResponse:
            return JSONResponse(
                {
                    "ok": True,
                    "lane": self.lane.id,
                    "harness": self.lane.harness,
                    "status": str(self.lane.status),
                    "subscribers": len(self._streams),
                    # `status` is the adapter's to own, so a lane whose harness
                    # has not transitioned it yet legitimately reports
                    # `starting` while this server is already answering. A
                    # healthcheck that only read `status` would call such a lane
                    # unhealthy and be wrong; `serving` is the part this object
                    # can actually attest to.
                    "serving": self._server is not None,
                }
            )

        self._app = Starlette(
            routes=[
                Route(self.card_path, agent_card, methods=["GET"]),
                Route("/", jsonrpc, methods=["POST"]),
                Route("/stream", stream, methods=["GET"]),
                Route("/health", health, methods=["GET"]),
            ],
        )
        return self._app

    async def start(self) -> None:
        """Bind the port and begin serving. Resolves an ephemeral port if 0."""
        import uvicorn

        self.build_app()
        config = uvicorn.Config(
            self._app,
            host=self.host,
            port=self.port,
            log_level="warning",
            access_log=False,
            lifespan="off",
        )
        self._server = uvicorn.Server(config)
        task = asyncio.create_task(self._server.serve())
        await self._wait_until_ready()
        self.port = self._resolve_bound_port()
        # Endpoint URLs are this server's to publish: it is the only thing that
        # knows the resolved port. `lane.status` is deliberately left alone —
        # the adapter owns the lane lifecycle and sets STARTING/IDLE itself, so
        # transitioning it here would fight the adapter for the same field and
        # would be wrong for a server that is serving a lane already running.
        self.lane.a2a_endpoint = self.base_url
        self.lane.agent_card_url = self.card_url
        log.info(
            "a2a.server.started",
            lane_id=self.lane.id,
            harness=self.lane.harness,
            url=self.base_url,
        )
        self._serve_task = task

    async def _wait_until_ready(self, timeout: float = 10.0) -> None:
        """Wait for the socket to accept, rather than assuming it does.

        A server that reports "started" before it can accept connections is a
        race that shows up as a flaky test three weeks later.
        """
        import httpx

        deadline = asyncio.get_running_loop().time() + timeout
        port = self.port
        while asyncio.get_running_loop().time() < deadline:
            if self._server is not None and getattr(self._server, "started", False):
                return
            try:
                target = f"http://{self.host}:{port or self.port}/health"
                async with httpx.AsyncClient(timeout=0.5) as client:
                    await client.get(target)
                return
            except Exception:
                await asyncio.sleep(0.05)
        log.warning("a2a.server.start_timeout", lane_id=self.lane.id)

    def _resolve_bound_port(self) -> int:
        if self._server is None:
            return self.port
        servers = getattr(self._server, "servers", None)
        if servers:
            sockets = getattr(servers[0], "sockets", None)
            if sockets:
                return int(sockets[0].getsockname()[1])
        return self.port

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
            await asyncio.sleep(0)
        for queue in list(self._streams):
            await queue.put({"event": "closed", "data": {"lane": self.lane.id}})
        self._streams.clear()
        log.info("a2a.server.stopped", lane_id=self.lane.id)

    # --- request handling --------------------------------------------------
    async def _handle_jsonrpc(self, request: Request) -> Response:
        try:
            payload = await request.json()
        except Exception as exc:
            return JSONResponse(
                make_error(INVALID_REQUEST, f"body is not JSON: {exc}", request_id=None),
                status_code=400,
            )

        try:
            rpc = JsonRpcRequest.parse(payload)
        except A2AProtocolError as exc:
            return JSONResponse(
                make_error(INVALID_REQUEST, exc.message, request_id=payload.get("id")),
                status_code=400,
            )

        with bind_context(lane_id=self.lane.id, harness=self.lane.harness):
            try:
                result = await self._dispatch(rpc)
            except OpenBurrowError as exc:
                log.warning("a2a.request.failed", method=rpc.method, error=str(exc))
                err = JsonRpcError.from_exception(exc)
                return JSONResponse(
                    make_error(err.code, err.message, request_id=rpc.id, data=err.data),
                    status_code=200,
                )
            except Exception as exc:
                log.exception("a2a.request.crashed", method=rpc.method)
                return JSONResponse(
                    make_error(INTERNAL_ERROR, str(exc), request_id=rpc.id),
                    status_code=200,
                )

        if rpc.is_notification:
            return Response(status_code=204)
        return JSONResponse(make_response(result, request_id=rpc.id))

    async def _dispatch(self, rpc: JsonRpcRequest) -> Any:
        method = rpc.method
        params = rpc.params

        if method == "message/send":
            return await self._method_message_send(params)
        if method == "message/stream":
            return await self._method_message_send(params)
        if method == "tasks/get":
            return await self._method_tasks_get(params)
        if method == "tasks/cancel":
            return await self._method_tasks_cancel(params)
        if method == "tasks/resubscribe":
            return {"ok": True, "note": "resubscribe acknowledged"}
        if method.startswith("tasks/pushNotificationConfig"):
            return {"ok": False, "note": "push notifications are not supported"}
        if method == "agent/getAuthenticatedExtendedCard":
            return build_agent_card(
                self.lane,
                capabilities=self.capabilities,
                skills=self.skills,
                base_url=self.base_url,
            )
        raise A2AProtocolError(
            f"unknown method {method!r}",
            hint="See A2A_METHODS for the supported set.",
            context={"method": method},
        )

    async def _method_message_send(self, params: dict[str, Any]) -> dict[str, Any]:
        """Inbound A2A message -> a BusMessage -> the injector.

        The translation is thin on purpose: A2A's message shape is close enough
        to OpenBurrow's that this is field mapping, not interpretation. Anything
        that *does* need interpretation (what does this text mean for this
        harness?) belongs in the adapter, not here.
        """
        message = self._to_bus_message(params)
        if self._on_message is None:
            raise A2AProtocolError(
                "this lane has no message handler attached",
                hint="The lane is discoverable but not wired to a harness yet.",
                context={"lane_id": self.lane.id},
            )

        await self._broadcast({"event": "message", "data": message.model_dump(mode="json")})
        task = await self._on_message(message)
        if task is None:
            return {"ok": True, "messageId": message.id, "taskId": None}
        return {"ok": True, "messageId": message.id, "task": task.model_dump(mode="json")}

    def _to_bus_message(self, params: dict[str, Any]) -> BusMessage:
        """Map an A2A ``message/send`` params object onto a :class:`BusMessage`."""
        raw_message = params.get("message") or params
        if not isinstance(raw_message, dict):
            raise A2AProtocolError(
                "message/send requires a 'message' object",
                context={"received": type(raw_message).__name__},
            )

        text = self._extract_text(raw_message)
        metadata = raw_message.get("metadata") or {}

        return BusMessage(
            session_id=str(metadata.get("openburrow:sessionId") or self.lane.session_id),
            thread_id=str(metadata.get("openburrow:threadId") or self.lane.session_id),
            task_id=str(raw_message.get("taskId") or ""),
            sender_lane=str(metadata.get("openburrow:senderLane") or ""),
            sender_harness=str(metadata.get("openburrow:senderHarness") or ""),
            recipients=[self.lane.id],
            subject=str(metadata.get("openburrow:subject") or text[:80]),
            body=text,
            payload={"a2a": raw_message},
            intent=metadata.get("openburrow:intent", "inform"),
            requires_reply=bool(metadata.get("openburrow:requiresReply", False)),
        )

    @staticmethod
    def _extract_text(message: dict[str, Any]) -> str:
        """Pull plain text out of A2A's parts array, tolerating both shapes."""
        parts = message.get("parts") or []
        chunks: list[str] = []
        for part in parts:
            if not isinstance(part, dict):
                continue
            if part.get("kind") == "text" or "text" in part:
                chunks.append(str(part.get("text", "")))
            elif part.get("kind") == "data":
                chunks.append(str(part.get("data", "")))
        if chunks:
            return "\n".join(chunk for chunk in chunks if chunk)
        return str(message.get("text") or message.get("body") or "")

    async def _method_tasks_get(self, params: dict[str, Any]) -> dict[str, Any]:
        task_id = str(params.get("id") or params.get("taskId") or "")
        if not task_id:
            raise A2AProtocolError("tasks/get requires an 'id'")
        if self._fetch_task is None:
            raise A2AProtocolError("this lane does not expose task lookup")
        task = await self._fetch_task(task_id)
        if task is None:
            raise A2AProtocolError(
                f"no task {task_id!r}",
                context={"code": TASK_NOT_FOUND, "task_id": task_id},
            )
        return task.model_dump(mode="json")

    async def _method_tasks_cancel(self, params: dict[str, Any]) -> dict[str, Any]:
        task_id = str(params.get("id") or params.get("taskId") or "")
        reason = str(params.get("reason") or "canceled by peer")
        if not task_id:
            raise A2AProtocolError("tasks/cancel requires an 'id'")
        if self._cancel_task is None:
            raise A2AProtocolError("this lane does not accept cancellation")
        task = await self._cancel_task(task_id, reason)
        if task is None:
            raise A2AProtocolError(f"no task {task_id!r}")
        return task.model_dump(mode="json")

    # --- streaming ---------------------------------------------------------
    async def _handle_stream(self, request: Request) -> Response:
        """SSE endpoint. Peers subscribe here for live task updates."""
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=512)
        self._streams.add(queue)

        async def publisher() -> Any:
            try:
                yield sse_frame(
                    {"lane": self.lane.id, "harness": self.lane.harness},
                    event="open",
                    retry_ms=3000,
                )
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        frame = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    yield sse_frame(frame.get("data"), event=frame.get("event", "message"))
                    if frame.get("event") == "closed":
                        break
            finally:
                self._streams.discard(queue)

        return StreamingResponse(
            publisher(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def _broadcast(self, frame: dict[str, Any]) -> None:
        """Fan a frame out to every SSE subscriber, dropping for slow readers.

        Dropping rather than blocking is the correct trade here: one stalled
        browser tab must not back-pressure the lane's event loop.
        """
        for queue in list(self._streams):
            try:
                queue.put_nowait(frame)
            except asyncio.QueueFull:
                log.debug("a2a.stream.dropped", lane_id=self.lane.id)

    async def publish_task_update(self, task: A2ATask) -> None:
        """Called by the lifecycle manager's listener to feed SSE subscribers."""
        await self._broadcast(
            {
                "event": "task",
                "data": {
                    "id": task.id,
                    "state": str(task.state),
                    "summary": task.result_summary or task.blocking_reason,
                },
            }
        )


async def start_lane_servers(
    lanes: list[Lane],
    *,
    host: str = "127.0.0.1",
    port_base: int = 7400,
    **kwargs: Any,
) -> list[LaneA2AServer]:
    """Start one server per lane, assigning ports from ``port_base``.

    Ports are assigned deterministically by index so a restarted daemon puts the
    same lane back on the same port — which keeps bookmarked Agent Card URLs and
    the external-peer allowlist stable across restarts.
    """
    servers: list[LaneA2AServer] = []
    for index, lane in enumerate(lanes):
        server = LaneA2AServer(lane, host=host, port=port_base + index, **kwargs)
        await server.start()
        servers.append(server)
    return servers


__all__ = ["LaneA2AServer", "MessageHandler", "start_lane_servers"]
