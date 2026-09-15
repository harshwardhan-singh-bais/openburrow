"""The fan-out hub.

The behaviour worth testing here is entirely about the unhappy path. The happy
path — a frame goes to everyone in the room — is four lines. Everything else is
about what happens when one client stops reading, which is the case that decides
whether a single slow laptop can take down a room.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from openburrow.relay.config import RelaySettings
from openburrow.relay.errors import RoomFullError
from openburrow.relay.hub import Hub
from openburrow.relay.metrics import build_metrics
from openburrow.relay.models import RoomRole

pytestmark = pytest.mark.unit

ROOM = "room_test"


@pytest.fixture
def hub(settings_factory: Callable[..., RelaySettings]) -> Hub:
    return Hub(settings_factory(), metrics=build_metrics())


@pytest.fixture
def small_hub(settings_factory: Callable[..., RelaySettings]) -> Hub:
    """A hub whose outbound queues hold two frames, to reach overflow quickly."""
    return Hub(settings_factory(OUTBOUND_QUEUE_SIZE="2"), metrics=build_metrics())


def admit(hub: Hub, *, room: str = ROOM, member: str = "mbr_a", kind: str = "events"):
    return hub.admit(
        kind=kind, room=room, member=member, subject=f"{member}@example.com", role=RoomRole.VIEWER
    )


class TestAdmission:
    def test_admits_and_registers(self, hub: Hub) -> None:
        admit(hub)
        assert len(hub) == 1
        assert hub.rooms() == {ROOM: 1}
        assert hub.counts() == {"events": 1}

    def test_room_cap_is_enforced(self, settings_factory: Callable[..., RelaySettings]) -> None:
        hub = Hub(settings_factory(MAX_CONNECTIONS_PER_ROOM="2"), metrics=build_metrics())
        admit(hub, member="a")
        admit(hub, member="b")
        with pytest.raises(RoomFullError) as excinfo:
            admit(hub, member="c")
        assert "open connections" in excinfo.value.message
        assert excinfo.value.context["limit"] == 2

    def test_member_cap_is_enforced_separately(
        self, settings_factory: Callable[..., RelaySettings]
    ) -> None:
        """One client opening many tabs must not consume the whole room.

        If the only cap were per-room, a single user with a reconnect loop would
        lock everyone else out — and the room cap is 64 by default, so it would
        take a while to notice.
        """
        hub = Hub(
            settings_factory(MAX_CONNECTIONS_PER_ROOM="50", MAX_CONNECTIONS_PER_MEMBER="2"),
            metrics=build_metrics(),
        )
        admit(hub, member="greedy")
        admit(hub, member="greedy")
        with pytest.raises(RoomFullError) as excinfo:
            admit(hub, member="greedy")
        assert "greedy" in excinfo.value.message
        # And another member can still join.
        admit(hub, member="polite")

    def test_release_frees_a_slot(self, hub: Hub) -> None:
        conn = admit(hub)
        hub.release(conn)
        assert len(hub) == 0
        assert hub.rooms() == {}
        admit(hub)  # does not raise

    def test_release_is_idempotent(self, hub: Hub) -> None:
        conn = admit(hub)
        hub.release(conn)
        hub.release(conn)
        hub.release(conn)
        assert len(hub) == 0

    def test_release_puts_the_close_sentinel_on_the_queue(self, hub: Hub) -> None:
        conn = admit(hub)
        hub.release(conn)
        assert conn.queue.get_nowait() is None

    def test_release_closes_the_connection(self, hub: Hub) -> None:
        conn = admit(hub)
        hub.release(conn)
        assert conn.closed is True


class TestPublishing:
    def test_delivers_to_everyone_in_the_room(self, hub: Hub) -> None:
        a = admit(hub, member="a")
        b = admit(hub, member="b")
        assert hub.publish(ROOM, {"type": "event"}) == 2
        assert a.queue.get_nowait() == {"type": "event"}
        assert b.queue.get_nowait() == {"type": "event"}

    def test_does_not_cross_rooms(self, hub: Hub) -> None:
        mine = admit(hub, room="room_a", member="a")
        admit(hub, room="room_b", member="b")
        assert hub.publish("room_a", {"type": "event"}) == 1
        assert mine.queue.qsize() == 1

    def test_does_not_cross_kinds(self, hub: Hub) -> None:
        """An event frame must not reach a brain-doc socket.

        Both kinds live in the same room, so without this a CRDT update would be
        delivered to a client that has no idea what to do with it.
        """
        events = admit(hub, member="a", kind="events")
        doc = admit(hub, member="b", kind="doc")
        assert hub.publish(ROOM, {"type": "event"}, kind="events") == 1
        assert events.queue.qsize() == 1
        assert doc.queue.qsize() == 0

    def test_exclude_skips_the_sender(self, hub: Hub) -> None:
        sender = admit(hub, member="a")
        other = admit(hub, member="b")
        assert hub.publish(ROOM, {"type": "event"}, exclude=sender.id) == 1
        assert sender.queue.qsize() == 0
        assert other.queue.qsize() == 1

    def test_publishing_to_an_empty_room_is_not_an_error(self, hub: Hub) -> None:
        assert hub.publish("nobody-here", {"type": "event"}) == 0

    def test_origin_seq_is_tracked_for_the_resume_point(self, hub: Hub) -> None:
        conn = admit(hub)
        hub.publish(ROOM, {"type": "event"}, origin_repo="repo/a", origin_seq=5)
        hub.publish(ROOM, {"type": "event"}, origin_repo="repo/a", origin_seq=9)
        hub.publish(ROOM, {"type": "event"}, origin_repo="repo/b", origin_seq=2)
        assert conn.resume_point() == {"repo/a": 9, "repo/b": 2}

    def test_origin_seq_never_goes_backwards(self, hub: Hub) -> None:
        # An out-of-order replay must not lower the resume point, or the client
        # would re-fetch events it already has.
        conn = admit(hub)
        hub.publish(ROOM, {"type": "event"}, origin_repo="repo/a", origin_seq=9)
        hub.publish(ROOM, {"type": "event"}, origin_repo="repo/a", origin_seq=3)
        assert conn.resume_point() == {"repo/a": 9}


class TestBackpressure:
    def test_overflow_drops_rather_than_blocking(self, small_hub: Hub) -> None:
        """The core guarantee: publishing never blocks on a slow client.

        `publish` is synchronous by contract. If it awaited a socket, one
        suspended laptop would stall the fan-out for the whole room.
        """
        conn = admit(small_hub)
        for index in range(20):
            small_hub.publish(ROOM, {"type": "event", "n": index})
        assert conn.dropped > 0

    def test_a_lag_notice_is_queued_on_overflow(self, small_hub: Hub) -> None:
        conn = admit(small_hub)
        for index in range(20):
            small_hub.publish(ROOM, {"type": "event", "n": index})

        frames = []
        while not conn.queue.empty():
            frames.append(conn.queue.get_nowait())

        notices = [f for f in frames if isinstance(f, dict) and f.get("type") == "lag"]
        assert notices, "a lagged client must be told, not silently under-served"
        notice = notices[0]
        assert notice["dropped"] > 0
        assert "resume_from" in notice
        assert "Re-tail" in notice["hint"]

    def test_a_fast_client_is_unaffected_by_a_slow_one(self, small_hub: Hub) -> None:
        """The isolation property. This is the whole reason the queue exists."""
        slow = admit(small_hub, member="slow")
        fast = admit(small_hub, member="fast")

        # Fill only the slow client's queue by publishing while it never drains.
        for index in range(50):
            small_hub.publish(ROOM, {"type": "event", "n": index})
        assert slow.dropped > 0

        # Drain the fast client and confirm it can still receive.
        while not fast.queue.empty():
            fast.queue.get_nowait()
        fast._lag_notice_queued = False
        assert small_hub.publish(ROOM, {"type": "event", "n": "after"}) >= 1
        assert fast.queue.qsize() >= 1

    def test_lag_notice_is_queued_only_once_per_gap(self, small_hub: Hub) -> None:
        conn = admit(small_hub)
        for index in range(20):
            small_hub.publish(ROOM, {"type": "event", "n": index})
        notices = 0
        while not conn.queue.empty():
            frame = conn.queue.get_nowait()
            if isinstance(frame, dict) and frame.get("type") == "lag":
                notices += 1
        assert notices == 1

    def test_released_connection_is_not_published_to(self, hub: Hub) -> None:
        conn = admit(hub)
        hub.release(conn)
        assert hub.publish(ROOM, {"type": "event"}) == 0


class TestPump:
    async def test_pump_drains_frames_in_order(self, hub: Hub) -> None:
        conn = admit(hub)
        received: list[dict] = []

        async def send(frame: dict) -> None:
            received.append(frame)

        hub.publish(ROOM, {"n": 1})
        hub.publish(ROOM, {"n": 2})
        hub.publish(ROOM, {"n": 3})

        task = asyncio.create_task(hub.pump(conn, send))
        await asyncio.sleep(0.01)
        hub.release(conn)
        await asyncio.wait_for(task, timeout=1.0)

        assert [f["n"] for f in received] == [1, 2, 3]

    async def test_pump_stops_on_the_close_sentinel(self, hub: Hub) -> None:
        conn = admit(hub)
        received: list[dict] = []

        async def send(frame: dict) -> None:
            received.append(frame)

        hub.publish(ROOM, {"n": 1})
        task = asyncio.create_task(hub.pump(conn, send))
        await asyncio.sleep(0.01)
        hub.release(conn)  # queues None
        await asyncio.wait_for(task, timeout=1.0)
        assert len(received) == 1

    async def test_pump_survives_a_failing_send(self, hub: Hub) -> None:
        """A broken socket must not leave the task running forever."""
        conn = admit(hub)
        calls = {"n": 0}

        async def send(frame: dict) -> None:
            calls["n"] += 1
            raise RuntimeError("socket is gone")

        hub.publish(ROOM, {"n": 1})
        hub.publish(ROOM, {"n": 2})
        await asyncio.wait_for(hub.pump(conn, send), timeout=1.0)
        # Stopped after the first failure rather than retrying into the void.
        assert calls["n"] == 1

    async def test_pump_calls_on_close(self, hub: Hub) -> None:
        conn = admit(hub)
        closed = {"called": False}

        async def send(frame: dict) -> None:
            return None

        async def on_close() -> None:
            closed["called"] = True

        task = asyncio.create_task(hub.pump(conn, send, on_close=on_close))
        await asyncio.sleep(0.01)
        hub.release(conn)
        await asyncio.wait_for(task, timeout=1.0)
        assert closed["called"] is True

    async def test_pump_delivers_the_lag_notice(self, small_hub: Hub) -> None:
        conn = admit(small_hub)
        received: list[dict] = []

        async def send(frame: dict) -> None:
            received.append(frame)

        for index in range(20):
            small_hub.publish(ROOM, {"type": "event", "n": index})

        task = asyncio.create_task(small_hub.pump(conn, send))
        await asyncio.sleep(0.05)
        small_hub.release(conn)
        await asyncio.wait_for(task, timeout=1.0)

        assert any(f.get("type") == "lag" for f in received)


class TestIntrospection:
    def test_describe_carries_the_useful_fields(self, hub: Hub) -> None:
        conn = admit(hub)
        described = conn.describe()
        assert described["room"] == ROOM
        assert described["role"] == "viewer"
        assert described["sent"] == 0
        assert described["dropped"] == 0

    def test_snapshot_can_be_scoped_to_a_room(self, hub: Hub) -> None:
        admit(hub, room="room_a", member="a")
        admit(hub, room="room_b", member="b")
        assert len(hub.snapshot(room="room_a")) == 1
        assert len(hub.snapshot()) == 2

    def test_counts_break_down_by_kind(self, hub: Hub) -> None:
        admit(hub, member="a", kind="events")
        admit(hub, member="b", kind="doc")
        assert hub.counts() == {"events": 1, "doc": 1}
