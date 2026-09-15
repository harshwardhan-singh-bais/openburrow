"""Connection registry and event fan-out.

The one thing this module must never do is let a slow client slow down a fast
one. A `websocket.send_json` call awaits the socket, and a socket whose peer has
stopped reading eventually blocks. If the fan-out loop awaited it directly, one
suspended laptop would stall every other participant in the room — and the
symptom would look like "the relay is down", not "one client is slow".

So each connection owns a bounded outbound queue. Publishing is a
``put_nowait``, which cannot block. When a queue is full the *newest* frame is
dropped, the connection is marked lagged, and the client is told — with a count
and a resume point — that it missed events and must re-tail over HTTP.

Dropping the newest rather than the oldest is deliberate. Dropping the oldest
would let a client that is merely behind eventually catch up to the present,
which sounds better until you realise it means every frame it does receive is
stale, and there is no point at which it knows its view is current. Dropping the
newest and announcing the gap makes the client's recovery explicit and bounded:
stop, fetch the tail from a known sequence, resume.

The `lag` notice is not an error. It is the relay telling the truth about what
it did, which is the only alternative to silently serving a view that is missing
events.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from openburrow.core.logging import get_logger
from openburrow.core.models.ids import new_ulid
from openburrow.relay.config import RelaySettings
from openburrow.relay.errors import RoomFullError
from openburrow.relay.metrics import METRICS, RelayMetrics
from openburrow.relay.models import RoomRole

log = get_logger(__name__)

#: How long a writer waits for the client to accept a frame before giving up.
#: Generous, because a genuinely slow link is not a fault — but finite, because
#: a dead one must not hold a slot in the room forever.
SEND_TIMEOUT_S = 20.0

ConnectionId = str


@dataclass(slots=True)
class Connection:
    """One WebSocket, and everything the hub needs to know about it."""

    id: ConnectionId
    kind: str  # "events" | "doc"
    room: str
    member: str
    subject: str
    role: RoomRole
    queue: asyncio.Queue[dict[str, Any] | None]
    opened_at: float = field(default_factory=time.monotonic)
    sent: int = 0
    dropped: int = 0
    #: Set when a frame was dropped. Cleared when the notice is emitted.
    _lagged: bool = False
    _lag_notice_queued: bool = False
    closed: bool = False
    #: Highest ``origin_seq`` seen per origin repo, so a lag notice can tell the
    #: client exactly where to resume from.
    last_seq: dict[str, int] = field(default_factory=dict)

    @property
    def age_s(self) -> float:
        return time.monotonic() - self.opened_at

    def note_seq(self, origin_repo: str, origin_seq: int) -> None:
        current = self.last_seq.get(origin_repo, 0)
        if origin_seq > current:
            self.last_seq[origin_repo] = origin_seq

    def resume_point(self) -> dict[str, int]:
        """What the client should pass back as ``since`` after a gap."""
        return dict(self.last_seq)

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "room": self.room,
            "member": self.member,
            "role": str(self.role),
            "age_s": round(self.age_s, 1),
            "sent": self.sent,
            "dropped": self.dropped,
            "queue_depth": self.queue.qsize(),
        }


class Hub:
    """Every open connection, grouped by room."""

    def __init__(
        self,
        settings: RelaySettings,
        *,
        metrics: RelayMetrics | None = None,
    ) -> None:
        self.settings = settings
        self.metrics = metrics or METRICS
        self._by_room: dict[str, dict[ConnectionId, Connection]] = {}
        self._by_member: dict[tuple[str, str], set[ConnectionId]] = {}
        self._by_id: dict[ConnectionId, Connection] = {}

    # --- admission --------------------------------------------------------
    def admit(
        self,
        *,
        kind: str,
        room: str,
        member: str,
        subject: str,
        role: RoomRole,
    ) -> Connection:
        """Register a connection, or refuse it.

        Both caps are checked before anything is allocated, so a refused
        connection costs nothing to clean up. The per-member cap exists
        separately from the per-room cap because one client opening a hundred
        tabs should be refused without consuming the room's capacity and
        locking out everyone else.
        """
        room_conns = self._by_room.setdefault(room, {})
        if len(room_conns) >= self.settings.max_connections_per_room:
            self.metrics.connections_rejected.labels(reason="room_full").inc()
            raise RoomFullError(
                f"room {room!r} already has {len(room_conns)} open connections",
                hint="Close another tab or client, or raise OPENBURROW_RELAY_MAX_CONNECTIONS_PER_ROOM.",
                context={"room": room, "limit": self.settings.max_connections_per_room},
            )

        member_key = (room, member)
        member_conns = self._by_member.setdefault(member_key, set())
        if len(member_conns) >= self.settings.max_connections_per_member:
            self.metrics.connections_rejected.labels(reason="member_limit").inc()
            raise RoomFullError(
                f"{member!r} already has {len(member_conns)} open connections in this room",
                hint="Close a tab, or raise OPENBURROW_RELAY_MAX_CONNECTIONS_PER_MEMBER.",
                context={
                    "room": room,
                    "member": member,
                    "limit": self.settings.max_connections_per_member,
                },
            )

        conn = Connection(
            id=new_ulid(),
            kind=kind,
            room=room,
            member=member,
            subject=subject,
            role=role,
            queue=asyncio.Queue(maxsize=self.settings.outbound_queue_size),
        )
        room_conns[conn.id] = conn
        member_conns.add(conn.id)
        self._by_id[conn.id] = conn

        self.metrics.connections_open.labels(kind=kind).inc()
        self.metrics.connections_total.labels(kind=kind).inc()
        log.info(
            "relay.connection_admitted",
            connection=conn.id,
            kind=kind,
            room=room,
            member=member,
            room_connections=len(room_conns),
        )
        return conn

    def release(self, conn: Connection, *, reason: str = "closed") -> None:
        """Deregister a connection. Idempotent."""
        if conn.closed:
            return
        conn.closed = True

        room_conns = self._by_room.get(conn.room)
        if room_conns is not None:
            room_conns.pop(conn.id, None)
            if not room_conns:
                # Drop the room entry so an idle room costs nothing and a
                # long-lived process does not accumulate empty dicts.
                self._by_room.pop(conn.room, None)

        member_conns = self._by_member.get((conn.room, conn.member))
        if member_conns is not None:
            member_conns.discard(conn.id)
            if not member_conns:
                self._by_member.pop((conn.room, conn.member), None)

        self._by_id.pop(conn.id, None)
        self.metrics.connections_open.labels(kind=conn.kind).dec()
        self.metrics.connection_duration.labels(kind=conn.kind).observe(conn.age_s)

        # Unblock the writer if it is waiting on the queue. A full queue means the
        # pump is already about to notice `closed`, so losing the sentinel is
        # harmless.
        with contextlib.suppress(asyncio.QueueFull):
            conn.queue.put_nowait(None)

        log.info(
            "relay.connection_released",
            connection=conn.id,
            kind=conn.kind,
            room=conn.room,
            reason=reason,
            age_s=round(conn.age_s, 1),
            sent=conn.sent,
            dropped=conn.dropped,
        )

    # --- publishing -------------------------------------------------------
    def publish(
        self,
        room: str,
        frame: dict[str, Any],
        *,
        kind: str = "events",
        exclude: ConnectionId | None = None,
        origin_repo: str | None = None,
        origin_seq: int | None = None,
    ) -> int:
        """Fan a frame out to a room. Returns how many connections received it.

        Synchronous and non-blocking by contract. It returns a count rather than
        raising, because one full queue is not a failure of the publish — it is a
        fact about one client, and the caller has no useful recovery.
        """
        delivered = 0
        for conn in list(self._by_room.get(room, {}).values()):
            if conn.kind != kind or conn.closed or conn.id == exclude:
                continue
            if origin_repo is not None and origin_seq is not None:
                conn.note_seq(origin_repo, origin_seq)
            if self._enqueue(conn, frame):
                delivered += 1
                self.metrics.events_fanned_out.inc()
        return delivered

    def _enqueue(self, conn: Connection, frame: dict[str, Any]) -> bool:
        try:
            conn.queue.put_nowait(frame)
            return True
        except asyncio.QueueFull:
            conn.dropped += 1
            conn._lagged = True
            self.metrics.events_dropped.inc()
            self._make_room_for_notice(conn)
            return False

    def _make_room_for_notice(self, conn: Connection) -> None:
        """Guarantee the lag notice reaches the client.

        Leaving it to the pump ("emit the notice before the next frame") is not
        enough: if the client has stopped reading entirely, the next frame never
        comes and the notice never arrives — which is exactly the client that
        most needs to be told.

        So one queued frame is evicted to make room. That frame is genuinely
        lost, and it is counted, because a drop count that under-reports is worse
        than no drop count: the client uses it to decide how much to re-tail.
        """
        if conn._lag_notice_queued:
            return
        try:
            evicted = conn.queue.get_nowait()
        except asyncio.QueueEmpty:  # pragma: no cover - cannot happen from a full queue
            return
        if evicted is None:
            # The close sentinel. Put it back; the connection is on its way out
            # and there is no client left to notify.
            conn.queue.put_nowait(None)
            return

        conn.dropped += 1
        self.metrics.events_dropped.inc()
        conn.queue.put_nowait(self._lag_frame(conn))
        conn._lag_notice_queued = True
        conn._lagged = False

    def _lag_frame(self, conn: Connection) -> dict[str, Any]:
        self.metrics.lagged_clients.inc()
        return {
            "type": "lag",
            "dropped": conn.dropped,
            "resume_from": conn.resume_point(),
            "hint": (
                "This connection fell behind and events were dropped. Re-tail over HTTP "
                "with GET /rooms/{room}/events?since=<resume_from> before continuing."
            ),
        }

    # --- the writer loop --------------------------------------------------
    async def pump(
        self,
        conn: Connection,
        send: Callable[[dict[str, Any]], Awaitable[None]],
        *,
        on_close: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Drain a connection's queue into ``send`` until it closes.

        The route owns reading from the socket; the hub owns writing to it. That
        split is what keeps backpressure in one place — a route that also wrote
        would have to reimplement the lag logic or, more likely, forget to.
        """
        try:
            while not conn.closed:
                frame = await conn.queue.get()
                if frame is None:
                    break

                # A notice queued behind the drop, if the immediate attempt lost
                # the race with a full queue.
                if conn._lagged and not conn._lag_notice_queued:
                    conn._lagged = False
                    conn._lag_notice_queued = True
                    frame = self._lag_frame(conn)

                try:
                    await asyncio.wait_for(send(frame), timeout=SEND_TIMEOUT_S)
                except TimeoutError:
                    log.warning(
                        "relay.send_timeout",
                        connection=conn.id,
                        room=conn.room,
                        timeout_s=SEND_TIMEOUT_S,
                    )
                    break
                except Exception as exc:
                    log.info("relay.send_failed", connection=conn.id, error=str(exc))
                    break

                conn.sent += 1
                conn._lag_notice_queued = False
        finally:
            if on_close is not None:
                # A cleanup hook that raises must not mask whatever ended the
                # pump in the first place, so the exception is swallowed on
                # purpose rather than logged over the real cause.
                with contextlib.suppress(Exception):
                    await on_close()

    # --- introspection ----------------------------------------------------
    def room_connections(self, room: str) -> list[Connection]:
        return list(self._by_room.get(room, {}).values())

    def counts(self) -> dict[str, int]:
        """Connection counts by kind, for /readyz and the dashboard."""
        totals: dict[str, int] = {}
        for conn in self._by_id.values():
            totals[conn.kind] = totals.get(conn.kind, 0) + 1
        return totals

    def rooms(self) -> dict[str, int]:
        return {room: len(conns) for room, conns in self._by_room.items() if conns}

    def snapshot(self, *, room: str | None = None) -> list[dict[str, Any]]:
        if room is not None:
            return [c.describe() for c in self.room_connections(room)]
        return [c.describe() for c in self._by_id.values()]

    def __len__(self) -> int:
        return len(self._by_id)


__all__ = ["SEND_TIMEOUT_S", "Connection", "Hub"]
