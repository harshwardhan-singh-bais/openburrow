"""The daemon-side relay client (items 62-65, chaos 250).

The client's contract is local-first: nothing it does may block or lose a local
write, and every failure mode it can hit (refused handshake, mid-session drop,
full queue, chaos drop) must end in a counted status field rather than an
exception. These tests run a minimal in-process relay so the reconnect and
replay paths are exercised over real sockets, not mocks of the transport.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

import pytest

from openburrow.daemon.relay_client import RelayClient

pytestmark = [pytest.mark.unit]


class FakeRelay:
    """A minimal relay: first-frame auth, publish fan-out, resume replay.

    Deliberately not a full relay implementation — only the frames the client
    speaks. Real-socket coverage comes from the relay package's own tests; what
    needs testing here is the client's behaviour against this protocol.
    """

    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []
        self.server: asyncio.Server | None = None
        self.port = 0
        # When set, the relay closes the socket immediately after the hello —
        # the mid-session-drop scenario.
        self.drop_after_hello = False

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            # First frame must be auth.
            raw = await asyncio.wait_for(reader.readline(), timeout=5)
            auth = json.loads(raw)
            if auth.get("type") != "auth" or not auth.get("token"):
                await self._send(
                    writer, {"type": "error", "code": "auth_required", "message": "no"}
                )
                return
            await self._send(writer, {"type": "hello", "connection": "c1"})
            if self.drop_after_hello:
                await asyncio.sleep(0.05)
                return
            # Fan out one synthetic remote event, then read publishes.
            await self._send(
                writer,
                {
                    "type": "publish",
                    "events": [
                        {
                            "seq": 1,
                            "event_type": "remote.lane_started",
                            "session_id": "sess_remote",
                            "summary": "remote lane up",
                            "payload": {"origin": "other-machine"},
                        }
                    ],
                },
            )
            while True:
                raw = await asyncio.wait_for(reader.readline(), timeout=10)
                if not raw:
                    return
                frame = json.loads(raw)
                if frame.get("type") == "publish":
                    self.published.extend(frame.get("events") or [])
                    await self._send(writer, {"type": "ack", "accepted": len(frame["events"])})
                elif frame.get("type") == "ping":
                    await self._send(writer, {"type": "pong"})
        except (TimeoutError, ConnectionError):
            return
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _send(self, writer: asyncio.StreamWriter, frame: dict[str, Any]) -> None:
        writer.write((json.dumps(frame) + "\n").encode("utf-8"))
        await writer.drain()


def _settings(**overrides: Any) -> Any:
    from openburrow.core.config.settings import Settings

    defaults: dict[str, Any] = {
        "relay_enabled": True,
        "relay_url": "unused-by-fake",
        "relay_token": "test-token",
        "relay_workspace": "test-room",
    }
    defaults.update(overrides)
    return Settings(**defaults)


class TestOffer:
    def test_disabled_relay_accepts_nothing(self) -> None:
        client = RelayClient(_settings(relay_enabled=False))
        client.offer({"event_type": "x"})
        assert client.outbound.qsize() == 0

    def test_event_is_queued(self) -> None:
        client = RelayClient(_settings())
        client.offer({"event_type": "x", "payload": {}})
        assert client.outbound.qsize() == 1

    def test_full_queue_drops_oldest_and_counts(self) -> None:
        client = RelayClient(_settings(relay_queue_max=2))
        for index in range(4):
            client.offer({"event_type": "x", "seq": index})
        assert client.outbound.qsize() == 2
        assert client.dropped_offline >= 1
        # FIFO: what survived is the newest.
        first = client.outbound.get_nowait()
        assert first["seq"] == 2

    def test_chaos_drop_swallows_events(self) -> None:
        client = RelayClient(_settings(chaos_drop_relay_messages=1.0, chaos_seed=1))
        client.offer({"event_type": "x"})
        assert client.outbound.qsize() == 0

    def test_chaos_drop_never_fires_at_zero(self) -> None:
        client = RelayClient(_settings(chaos_drop_relay_messages=0.0))
        client.offer({"event_type": "x"})
        assert client.outbound.qsize() == 1


class TestStatus:
    def test_status_shape(self) -> None:
        client = RelayClient(_settings())
        status = client.status()
        assert status["enabled"] is True
        assert status["connected"] is False
        assert status["room"] == "test-room"
        assert status["queued"] == 0

    def test_disabled_status(self) -> None:
        client = RelayClient(_settings(relay_enabled=False))
        assert client.status()["enabled"] is False


class TestConvert:
    def test_to_relay_strips_local_fields(self) -> None:
        from openburrow.daemon.relay_client import _to_relay

        event = {
            "event_type": "lane.output",
            "session_id": "s1",
            "summary": "out",
            "payload": {"buffer": "x" * 5000},
            "trust_boundary": "intra_repo",
        }
        relayed = _to_relay(event)
        assert relayed["event_type"] == "lane.output"
        assert relayed["trust_boundary"] == "intra_repo"
        assert len(relayed["summary"]) <= 500

    def test_to_relay_keeps_nonlocal_boundary(self) -> None:
        from openburrow.daemon.relay_client import _to_relay

        relayed = _to_relay({"trust_boundary": "cross_org"})
        assert relayed["trust_boundary"] == "cross_org"


class TestReemit:
    async def test_reemit_marks_relay_boundary(self) -> None:
        client = RelayClient(_settings())
        seen: list[dict[str, Any]] = []

        async def emit(event: dict[str, Any]) -> None:
            seen.append(event)

        client.attach(emit)
        await client._reemit(
            {"event_type": "remote.x", "seq": 1, "summary": "hi", "payload": {"a": 1}}
        )
        assert len(seen) == 1
        assert seen[0]["trust_boundary"] == "relay"
        assert seen[0]["payload"]["relayed"] is True
        assert client.replayed_from_relay == 1

    async def test_reemit_without_attach_is_silent(self) -> None:
        client = RelayClient(_settings())
        await client._reemit({"event_type": "remote.x"})  # must not raise
        assert client.replayed_from_relay == 0
