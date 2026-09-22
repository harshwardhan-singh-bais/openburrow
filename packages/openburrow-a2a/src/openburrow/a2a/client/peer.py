"""A2A client — how one lane talks to another.

This is the outbound half of the bus. It is a plain A2A client: it discovers a
peer by fetching ``/.well-known/agent-card.json``, caches the card, and posts
JSON-RPC. Nothing here is OpenBurrow-specific except the metadata keys, which
means the same client is what talks to a *third-party* A2A agent in the interop
test — the code path is identical, which is the only way that test proves
anything.

Retry policy is deliberately narrow: retry transport failures and 5xx, never
retry a 4xx or an application-level error. A rejected delegation must not be
retried into a loop; that is a policy decision, and policy lives above this
layer.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from openburrow.a2a.transport import (
    coerce_task_payload,
    make_request,
)
from openburrow.core.errors import A2AProtocolError, ConformanceError
from openburrow.core.logging import bind_context, get_logger
from openburrow.core.models import A2ATask, BusMessage, Performative, TaskState

log = get_logger(__name__)

DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=120.0, write=30.0, pool=5.0)


class PeerClient:
    """Talks to one remote A2A peer."""

    def __init__(
        self,
        base_url: str,
        *,
        card_path: str = "/.well-known/agent-card.json",
        timeout: httpx.Timeout | None = None,
        max_retries: int = 2,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.card_path = card_path
        self.max_retries = max_retries
        self.headers = headers or {}
        self._client = httpx.AsyncClient(timeout=timeout or DEFAULT_TIMEOUT, headers=self.headers)
        self._card: dict[str, Any] | None = None

    async def __aenter__(self) -> PeerClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    # --- discovery ---------------------------------------------------------
    async def fetch_card(self, *, refresh: bool = False) -> dict[str, Any]:
        """Fetch and cache the peer's Agent Card."""
        if self._card is not None and not refresh:
            return self._card
        url = f"{self.base_url}{self.card_path}"
        response = await self._client.get(url)
        if response.status_code != 200:
            raise ConformanceError(
                f"peer at {self.base_url} returned {response.status_code} for its Agent Card",
                hint="Is the lane's A2A server running, and is the card path correct?",
                context={"url": url, "status": response.status_code},
            )
        card = response.json()
        if not isinstance(card, dict) or "skills" not in card:
            raise ConformanceError(
                f"peer at {self.base_url} served a malformed Agent Card",
                hint="A valid A2A card requires at least 'name', 'url' and 'skills'.",
                context={"url": url},
            )
        self._card = card
        log.debug("a2a.peer.discovered", url=self.base_url, skills=len(card.get("skills", [])))
        return card

    async def declared_skills(self) -> list[str]:
        card = await self.fetch_card()
        return [str(s.get("id", "")) for s in card.get("skills", []) if isinstance(s, dict)]

    async def is_reachable(self) -> bool:
        """Best-effort probe: is a usable Agent Card being served?

        The exception list is deliberate rather than ``except Exception``. A
        reachability probe that catches everything reports "unreachable" when
        the real fault is a bug in our own card handling, which sends whoever
        is on call looking at the network for a problem that is not there.
        """
        try:
            await self.fetch_card(refresh=True)
        except (httpx.HTTPError, ConformanceError, ValueError):
            return False
        return True

    async def health(self) -> dict[str, Any]:
        """Fetch the peer's ``/health`` payload.

        Distinct from :meth:`is_reachable`, and the distinction is the point:
        a lane can serve a static Agent Card while its JSON-RPC endpoint is
        wedged behind a blocked event loop. The card proves the process is
        listening; ``/health`` proves the loop is turning, and reports live
        state (lane status, attached SSE subscribers) rather than declared
        capability.

        Raises :class:`ConformanceError` when the peer answers with a non-200,
        so a caller that treats a failure as "unhealthy" can say *why*.
        """
        url = f"{self.base_url}/health"
        response = await self._client.get(url)
        if response.status_code != 200:
            raise ConformanceError(
                f"peer at {self.base_url} returned {response.status_code} for /health",
                hint="The lane process is listening but its health route is not answering.",
                context={"url": url, "status": response.status_code},
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise ConformanceError(
                f"peer at {self.base_url} served a non-object /health payload",
                context={"url": url, "type": type(payload).__name__},
            )
        return payload

    # --- JSON-RPC ----------------------------------------------------------
    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Issue a JSON-RPC call with the narrow retry policy described above."""
        payload = make_request(method, params)
        last_error: Exception | None = None

        for attempt in range(self.max_retries + 1):
            try:
                response = await self._client.post(self.base_url, json=payload)
            except httpx.TransportError as exc:
                last_error = exc
                if attempt < self.max_retries:
                    await asyncio.sleep(0.25 * (2**attempt))
                    continue
                raise A2AProtocolError(
                    f"could not reach peer {self.base_url}: {exc}",
                    hint="Check that the lane's A2A server is still running.",
                    context={"method": method, "attempts": attempt + 1},
                    cause=exc,
                ) from exc

            if response.status_code >= 500 and attempt < self.max_retries:
                last_error = A2AProtocolError(f"peer returned {response.status_code}")
                await asyncio.sleep(0.25 * (2**attempt))
                continue

            if response.status_code >= 400:
                raise A2AProtocolError(
                    f"peer rejected {method} with HTTP {response.status_code}",
                    context={"method": method, "body": response.text[:500]},
                )

            body = response.json()
            if "error" in body:
                error = body["error"]
                raise A2AProtocolError(
                    f"peer returned JSON-RPC error {error.get('code')}: {error.get('message')}",
                    context={"method": method, "error": error},
                )
            return dict(body.get("result") or {})

        raise A2AProtocolError(
            f"exhausted retries calling {method} on {self.base_url}",
            context={"last_error": str(last_error)},
        )

    # --- high level --------------------------------------------------------
    async def send_message(
        self,
        message: BusMessage,
        *,
        wait: bool = False,
        timeout_s: int = 900,
    ) -> A2ATask | None:
        """Deliver a bus message to this peer as an A2A ``message/send``.

        Returns the task the peer created, when it created one. A plain
        ``inform`` message typically creates no task — the peer just absorbs it.

        ``timeout_s`` bounds the whole call, retries included. It is enforced
        here rather than left to the HTTP client because a peer that accepts a
        connection and then never answers is the failure this deadline exists
        for, and a transport-level timeout does not cover it.
        """
        params = {
            "message": {
                "role": "user",
                "taskId": message.task_id or None,
                "parts": [{"kind": "text", "text": message.body}],
                "metadata": {
                    "openburrow:sessionId": message.session_id,
                    "openburrow:threadId": message.thread_id,
                    "openburrow:senderLane": message.sender_lane,
                    "openburrow:senderHarness": message.sender_harness,
                    "openburrow:subject": message.subject,
                    "openburrow:intent": str(message.intent),
                    "openburrow:requiresReply": message.requires_reply,
                    "openburrow:messageId": message.id,
                },
            },
            "configuration": {"blocking": wait, "historyLength": 0},
        }

        with bind_context(session_id=message.session_id, lane_id=message.sender_lane):
            # This parameter used to be accepted and ignored, which is worse than
            # not having it: a caller who passed timeout_s=5 believed they had
            # bounded the wait and had not. An unenforced deadline is a promise
            # the code does not keep, and callers plan around promises.
            try:
                async with asyncio.timeout(timeout_s):
                    result = await self.call("message/send", params)
            except TimeoutError as exc:
                raise A2AProtocolError(
                    f"peer {self.base_url} did not answer message/send within {timeout_s}s",
                    hint="Raise timeout_s, or check whether the peer's lane is still running.",
                    context={"method": "message/send", "timeout_s": timeout_s},
                    cause=exc,
                ) from exc

        task_payload = result.get("task")
        if not isinstance(task_payload, dict):
            log.debug("a2a.message.absorbed", peer=self.base_url, message_id=message.id)
            return None

        # A third-party peer emits camelCase (the A2A wire shape); OpenBurrow's
        # own server emits snake_case. coerce_task_payload accepts both, which
        # is the difference between speaking A2A and only speaking to
        # ourselves. Unknown keys still fail validation downstream.
        task = A2ATask.model_validate(coerce_task_payload(task_payload))
        log.info(
            "a2a.message.delivered",
            peer=self.base_url,
            message_id=message.id,
            task_id=task.id,
            state=str(task.state),
        )
        return task

    async def get_task(self, task_id: str) -> A2ATask | None:
        try:
            payload = await self.call("tasks/get", {"id": task_id})
        except A2AProtocolError as exc:
            log.debug("a2a.task.fetch_failed", task_id=task_id, error=str(exc))
            return None
        return A2ATask.model_validate(coerce_task_payload(payload))

    async def cancel_task(self, task_id: str, reason: str = "") -> A2ATask | None:
        try:
            payload = await self.call("tasks/cancel", {"id": task_id, "reason": reason})
        except A2AProtocolError:
            return None
        return A2ATask.model_validate(coerce_task_payload(payload))

    async def delegate_task(
        self,
        *,
        session_id: str,
        thread_id: str,
        requester_lane: str,
        title: str,
        instruction: str,
        authorized_by: str,
        skill: str = "",
        authority_scope: list[str] | None = None,
        delegation_id: str = "",
        delegation_depth: int = 0,
    ) -> A2ATask | None:
        """Delegate a unit of work to this peer, carrying the governance envelope.

        The ``openburrow:authorityScope`` metadata is what the receiving lane's
        governance layer checks before acting. Omitting it does not mean
        "unrestricted" — it means "no authority granted", and the receiver will
        reject the task.
        """
        message = BusMessage(
            session_id=session_id,
            thread_id=thread_id,
            sender_lane=requester_lane,
            recipients=[],
            subject=title,
            body=instruction,
            intent=Performative.PROPOSE,
            requires_reply=True,
            delegation_id=delegation_id,
            payload={
                "openburrow:skill": skill,
                "openburrow:authorizedBy": authorized_by,
                "openburrow:authorityScope": authority_scope or [],
                "openburrow:delegationId": delegation_id,
                "openburrow:delegationDepth": delegation_depth,
            },
        )
        return await self.send_message(message)

    async def stream_task(self, task_id: str) -> Any:
        """Subscribe to a task's SSE updates. Yields parsed frames."""
        from openburrow.a2a.transport import iterate_sse

        async with self._client.stream(
            "GET", f"{self.base_url}/stream", headers={"Accept": "text/event-stream"}
        ) as response:
            response.raise_for_status()
            async for frame in iterate_sse(response):
                data = frame.get("data")
                if isinstance(data, dict) and data.get("id") == task_id:
                    yield frame


class BusClientPool:
    """Keeps one :class:`PeerClient` per peer URL.

    Reusing connections matters more than it looks: a negotiation exchange is
    several round trips between the same two lanes, and re-establishing TCP for
    each move adds latency to the exact path that needs to feel responsive.
    """

    def __init__(self, **client_kwargs: Any) -> None:
        self._clients: dict[str, PeerClient] = {}
        self._kwargs = client_kwargs

    def client(self, base_url: str) -> PeerClient:
        url = base_url.rstrip("/")
        if url not in self._clients:
            self._clients[url] = PeerClient(url, **self._kwargs)
        return self._clients[url]

    async def broadcast(
        self,
        base_urls: list[str],
        message_factory: Any,
    ) -> dict[str, A2ATask | None]:
        """Send to several peers concurrently, returning per-peer results.

        Failures are captured per peer rather than raised, because one
        unreachable lane must not abort delivery to the others — that is the
        bulkhead principle applied at the client layer.
        """

        async def deliver(url: str) -> tuple[str, A2ATask | None]:
            try:
                return url, await self.client(url).send_message(message_factory(url))
            except Exception as exc:
                log.warning("a2a.broadcast.failed", peer=url, error=str(exc))
                return url, None

        results = await asyncio.gather(*(deliver(url) for url in base_urls))
        return dict(results)

    async def close(self) -> None:
        for client in self._clients.values():
            await client.close()
        self._clients.clear()


def card_is_conformant(card: dict[str, Any]) -> tuple[bool, list[str]]:
    """Minimal A2A conformance check on a received Agent Card.

    Returns ``(ok, problems)``. Used by ``burrow doctor`` and by the interop
    test to assert that a peer is genuinely speaking A2A rather than something
    that merely looks like it.
    """
    problems: list[str] = []
    for field in ("name", "url", "skills", "capabilities"):
        if field not in card:
            problems.append(f"missing required field '{field}'")
    if not isinstance(card.get("skills"), list):
        problems.append("'skills' must be a list")
    capabilities = card.get("capabilities")
    if capabilities is not None and not isinstance(capabilities, dict):
        problems.append("'capabilities' must be an object")
    protocol = card.get("protocolVersion")
    if protocol and not str(protocol).startswith("0.") and not str(protocol).startswith("1."):
        problems.append(f"unrecognised protocolVersion {protocol!r}")
    return (not problems, problems)


__all__ = ["BusClientPool", "PeerClient", "TaskState", "card_is_conformant"]
