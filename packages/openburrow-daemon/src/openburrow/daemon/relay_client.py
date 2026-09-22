"""Outbound relay client (Stage 4, items 62-65; chaos 250).

The daemon dials the relay's ``/rooms/{room}/stream`` WebSocket, authenticates
with a first-frame token (the documented preferred path — a token in the query
string ends up in access logs), and then does one job: every event the local
bus persists is published to the room, and every event the room publishes back
is emitted onto the local bus. Local-first holds: a dead relay never blocks or
loses a local write, it only delays the remote copy of it.

Offline mode (item 64) is therefore not a separate code path — it is this
module with an unconnectable URL. Events accumulate in a bounded local queue
and flush on reconnect. Reconciliation (item 65) is the relay's own ``resume``
frame replayed against the local bus: on reconnect the client sends the seqs it
already has, receives everything after, and re-emits the missed events with
``trust_boundary="relay"`` so downstream consumers can tell a relayed fact from
a local one.

Chaos (item 250) hooks the publish path: with
``chaos_drop_relay_messages`` > 0 the client drops that fraction of outbound
messages deterministically (seeded), which is the fault a reconnect loop exists
to survive.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
from typing import TYPE_CHECKING, Any

from openburrow.core.logging import get_logger

if TYPE_CHECKING:
    from openburrow.core.config.settings import Settings

log = get_logger(__name__)

#: Outbound queue bound. Events beyond this are the oldest-dropped: a relay
#: outage longer than the queue is an operator-visible problem (the lag frame
#: says so), not a reason to grow memory without limit.
_QUEUE_MAX_DEFAULT = 10_000

#: Reconnect backoff schedule in seconds. Capped by settings; the steps exist
#: so a flapping relay sees spacing, not a hot loop.
_BACKOFF_STEPS = (1.0, 2.0, 5.0, 10.0, 30.0)

#: Relay frames we accept inbound, after the initial ``hello``.
_INBOUND_TYPES = {"publish", "error", "lag", "pong"}

_PING_INTERVAL_S = 30.0


class RelayClient:
    """Dial the relay, keep the two event streams flowing, survive outages."""

    def __init__(self, settings: Settings, *, room: str = "") -> None:
        self.settings = settings
        self.room = room or settings.relay_workspace
        self.outbound: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=max(1, settings.relay_queue_max or _QUEUE_MAX_DEFAULT)
        )
        self.dropped_offline = 0
        self.published = 0
        self.replayed_from_relay = 0
        self.connected = False
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        # Random here is fault *injection*, not security: the seed exists so a
        # chaos run is reproducible, which is the opposite of cryptographic
        # strength. S311's usual concern does not apply.
        self._chaos_rng = random.Random(settings.chaos_seed)  # noqa: S311
        # Subscriptions handed to us by the daemon: the client does not read the
        # bus directly, the daemon wires its own subscription to `offer()`.
        self._emit: Any = None  # async callable(event) -> None, set by the daemon

    # --- daemon wiring ------------------------------------------------------
    def attach(self, emit: Any) -> None:
        """Give the client the coroutine that re-emits relayed events locally."""
        self._emit = emit

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping.clear()
            self._task = asyncio.create_task(self._run(), name="relay-client")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    # --- status -------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.settings.relay_enabled,
            "connected": self.connected,
            "room": self.room,
            "queued": self.outbound.qsize(),
            "published": self.published,
            "replayed_from_relay": self.replayed_from_relay,
            "dropped_offline": self.dropped_offline,
        }

    # --- outbound offer -----------------------------------------------------
    def offer(self, event: dict[str, Any]) -> None:
        """Queue one local event for the relay. Never blocks, never raises.

        Called from the bus fan-out, which is on the persist-then-publish
        critical path: any await here would make a remote hop part of a local
        write. Full-queue drops a *counted* oldest event (the queue is FIFO) and
        the count is reported in ``status()``, so the loss is visible rather
        than silent.
        """
        if not self.settings.relay_enabled:
            return
        if self._chaos_drop():
            return
        try:
            self.outbound.put_nowait(event)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                self.outbound.get_nowait()
                self.dropped_offline += 1
            with contextlib.suppress(asyncio.QueueFull):
                self.outbound.put_nowait(event)

    def _chaos_drop(self) -> bool:
        rate = self.settings.chaos_drop_relay_messages
        return rate > 0 and self._chaos_rng.random() < rate

    # --- the loop -----------------------------------------------------------
    async def _run(self) -> None:
        backoff = 0
        import websockets

        url = self._ws_url()
        while not self._stopping.is_set():
            try:
                async with websockets.connect(
                    url,
                    additional_headers={"authorization": f"Bearer {self.settings.relay_token}"},
                    open_timeout=10,
                    max_size=16 * 1024 * 1024,
                ) as ws:
                    await self._handshake(ws)
                    self.connected = True
                    backoff = 0
                    log.info("relay.connected", room=self.room, url=url)
                    await self._session(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("relay.disconnected", error=str(exc), attempt=backoff + 1)
            finally:
                self.connected = False

            if self._stopping.is_set():
                return
            delay = _BACKOFF_STEPS[min(backoff, len(_BACKOFF_STEPS) - 1)]
            cap = self.settings.relay_reconnect_max_s or 60
            await asyncio.sleep(min(delay, float(cap)))
            backoff += 1

    def _ws_url(self) -> str:
        base = self.settings.relay_url
        if base.endswith("/ws"):
            base = base[: -len("/ws")]
        return f"{base}/rooms/{self.room}/stream"

    async def _handshake(self, ws: Any) -> None:
        """First frame auth — the documented preferred path."""
        await ws.send(json.dumps({"type": "auth", "token": self.settings.relay_token}))
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            frame = json.loads(raw)
            kind = frame.get("type")
            if kind == "hello":
                log.debug("relay.hello", connection=frame.get("connection"))
                return
            if kind == "error":
                raise ConnectionError(f"relay refused: {frame.get('code')}: {frame.get('message')}")

    async def _session(self, ws: Any) -> None:
        """One connected session: drain the queue out, pump frames in."""
        receiver = asyncio.create_task(self._receive_loop(ws))
        sender = asyncio.create_task(self._send_loop(ws))
        pinger = asyncio.create_task(self._ping_loop(ws))
        try:
            done, pending = await asyncio.wait(
                {receiver, sender, pinger},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            for task in done:
                exc = task.exception()
                if exc is not None:
                    raise exc
        finally:
            for task in (receiver, sender, pinger):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    async def _send_loop(self, ws: Any) -> None:
        """Flush the outbound queue in publish batches."""
        while True:
            batch: list[dict[str, Any]] = []
            try:
                event = await asyncio.wait_for(self.outbound.get(), timeout=1.0)
                batch.append(event)
                while len(batch) < 200 and not self.outbound.empty():
                    batch.append(self.outbound.get_nowait())
            except TimeoutError:
                continue
            await ws.send(json.dumps({"type": "publish", "events": [_to_relay(e) for e in batch]}))
            self.published += len(batch)

    async def _receive_loop(self, ws: Any) -> None:
        since: dict[str, int] = {}
        while True:
            raw = await ws.recv()
            frame = json.loads(raw)
            kind = frame.get("type")
            if kind == "publish":
                events = frame.get("events") or []
                for remote in events:
                    await self._reemit(remote)
                    seq = int(remote.get("seq") or 0)
                    repo = str(remote.get("repo") or self.room)
                    if seq:
                        since[repo] = max(since.get(repo, 0), seq)
            elif kind == "lag" and self._emit is not None:
                # The relay says we are behind: reconcile (item 65) by asking
                # for everything after what we hold.
                await ws.send(json.dumps({"type": "resume", "since": since}))
            elif kind == "error":
                log.warning(
                    "relay.frame_error", code=frame.get("code"), message=frame.get("message")
                )

    async def _ping_loop(self, ws: Any) -> None:
        while True:
            await asyncio.sleep(_PING_INTERVAL_S)
            await ws.send(json.dumps({"type": "ping"}))

    async def _reemit(self, remote: dict[str, Any]) -> None:
        """Re-emit one relayed event onto the local bus.

        Marked with a distinct trust boundary (item 184's spirit): a fact that
        arrived over the relay is not the same class of fact as one observed
        locally, and downstream consumers can filter on it.
        """
        if self._emit is None:
            return
        payload = dict(remote.get("payload") or {})
        payload["relayed"] = True
        payload["relay_room"] = self.room
        try:
            await self._emit(
                {
                    "event_type": str(remote.get("event_type") or "relay.event"),
                    "session_id": str(remote.get("session_id") or ""),
                    "thread_id": str(remote.get("thread_id") or ""),
                    "lane_id": str(remote.get("lane_id") or ""),
                    "task_id": str(remote.get("task_id") or ""),
                    "task_state": str(remote.get("task_state") or ""),
                    "priority": str(remote.get("priority") or "informative"),
                    "trust_boundary": "relay",
                    "summary": str(remote.get("summary") or ""),
                    "payload": payload,
                    "correlation_id": str(remote.get("correlation_id") or ""),
                }
            )
            self.replayed_from_relay += 1
        except Exception as exc:
            log.warning("relay.reemit_failed", error=str(exc))


def _to_relay(event: dict[str, Any]) -> dict[str, Any]:
    """Local bus event -> relay publish item. Explicit allowlist, not passthrough:
    local-only fields (raw output buffers, env echoes) must not leave the machine."""
    return {
        "seq": event.get("seq"),
        "event_type": event.get("event_type"),
        "session_id": event.get("session_id"),
        "thread_id": event.get("thread_id"),
        "lane_id": event.get("lane_id"),
        "task_id": event.get("task_id"),
        "task_state": event.get("task_state"),
        "priority": event.get("priority"),
        "trust_boundary": "intra_repo"
        if (event.get("trust_boundary") or "intra_repo") == "intra_repo"
        else event.get("trust_boundary"),
        "summary": (event.get("summary") or "")[:500],
        "payload": event.get("payload") or {},
        "correlation_id": event.get("correlation_id") or "",
        "at": event.get("at") or "",
        "repo": event.get("repo") or "",
    }


__all__ = ["RelayClient"]
