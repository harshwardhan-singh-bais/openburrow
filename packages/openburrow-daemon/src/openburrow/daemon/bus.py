"""In-process event bus.

A fan-out on top of the append-only log. Every event is *first* persisted, then
delivered — never the other way round. That ordering is what makes the log
authoritative: if a subscriber misses an event because it was slow or crashed, it
can recover by reading the log from its last seen ``seq``. An in-memory bus with
no durable backing would make replay impossible.

Subscribers get bounded queues and are dropped when they overflow. A slow TUI
must not be able to stall a lane, and the log means nothing is actually lost —
the subscriber just has to catch up.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from openburrow.core.db.repository import BusEventLog
from openburrow.core.logging import get_logger

log = get_logger(__name__)

#: Per-subscriber queue depth before the subscriber is considered too slow.
DEFAULT_QUEUE_SIZE = 1024


@dataclass(slots=True)
class Subscription:
    """One subscriber's registration."""

    id: str
    queue: asyncio.Queue[dict[str, Any]]
    #: Optional filter: only deliver events matching these types.
    event_types: set[str] = field(default_factory=set)
    session_id: str = ""
    dropped: int = 0

    def matches(self, event: dict[str, Any]) -> bool:
        """Whether this subscriber wants this event.

        Both filters are conjunctive: a subscription with a session and a type
        filter receives only events matching both.

        The type filter used to read ``return True and event.get("event_type")
        in self.event_types``. That is just ``return False`` — the branch is only
        entered when the type is absent — so the result was right, but the
        expression was a leftover from a longer conjunction and read like a bug.
        In a filter that decides what a lane is allowed to see, an expression
        that looks wrong but happens to be right is its own kind of defect.
        """
        if self.session_id and event.get("session_id") != self.session_id:
            return False
        if not self.event_types:
            # No type filter: anything on this session is wanted.
            return True
        return event.get("event_type") in self.event_types


class EventBus:
    """Persist-then-publish fan-out."""

    def __init__(self, log_writer: Callable[[], BusEventLog]) -> None:
        # The log is resolved lazily because it needs a live DB session, and the
        # bus outlives any individual session.
        self._log_writer = log_writer
        self._subscribers: dict[str, Subscription] = {}
        self._counter = 0

    # --- subscription ------------------------------------------------------
    def subscribe(
        self,
        *,
        session_id: str = "",
        event_types: set[str] | None = None,
        queue_size: int = DEFAULT_QUEUE_SIZE,
    ) -> Subscription:
        self._counter += 1
        subscription = Subscription(
            id=f"sub-{self._counter}",
            queue=asyncio.Queue(maxsize=queue_size),
            event_types=event_types or set(),
            session_id=session_id,
        )
        self._subscribers[subscription.id] = subscription
        log.debug(
            "bus.subscribed",
            subscription=subscription.id,
            session_id=session_id,
            types=sorted(subscription.event_types),
        )
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        self._subscribers.pop(subscription.id, None)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    # --- publishing --------------------------------------------------------
    async def publish(self, event: dict[str, Any]) -> int:
        """Deliver an already-persisted event to every matching subscriber.

        Returns the number of subscribers that received it. Callers that also
        need the event written should use :meth:`emit` instead.
        """
        delivered = 0
        for subscription in list(self._subscribers.values()):
            if not subscription.matches(event):
                continue
            try:
                subscription.queue.put_nowait(event)
                delivered += 1
            except asyncio.QueueFull:
                subscription.dropped += 1
                log.warning(
                    "bus.subscriber_slow",
                    subscription=subscription.id,
                    dropped=subscription.dropped,
                    hint="Subscriber will need to catch up from the log by seq.",
                )
        return delivered

    async def emit(
        self,
        *,
        event_type: str,
        session_id: str = "",
        thread_id: str = "",
        lane_id: str = "",
        task_id: str = "",
        task_state: str = "",
        priority: str = "informative",
        trust_boundary: str = "intra_repo",
        summary: str = "",
        payload: dict[str, Any] | None = None,
        correlation_id: str = "",
        content_hash: str = "",
    ) -> dict[str, Any]:
        """Persist an event, then fan it out. The canonical write path."""
        async with self._log_writer() as log:
            seq = await log.append(
                event_type=event_type,
                session_id=session_id,
                thread_id=thread_id,
                lane_id=lane_id,
                task_id=task_id,
                task_state=task_state,
                priority=priority,
                trust_boundary=trust_boundary,
                summary=summary,
                payload=payload,
                correlation_id=correlation_id,
                content_hash=content_hash,
            )
            await log.session.commit()

        event = {
            "seq": seq,
            "event_type": event_type,
            "session_id": session_id,
            "thread_id": thread_id,
            "lane_id": lane_id,
            "task_id": task_id,
            "task_state": task_state,
            "priority": priority,
            "trust_boundary": trust_boundary,
            "summary": summary,
            "payload": payload or {},
            "correlation_id": correlation_id,
        }
        await self.publish(event)
        return event

    # --- consumption -------------------------------------------------------
    async def listen(
        self,
        subscription: Subscription,
        *,
        idle_timeout: float | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield events delivered to a subscription.

        ``idle_timeout`` ends the iterator after a quiet period, which is what
        lets ``burrow logs -f`` exit when nothing is happening instead of
        hanging forever.
        """
        try:
            while True:
                try:
                    if idle_timeout is None:
                        event = await subscription.queue.get()
                    else:
                        event = await asyncio.wait_for(
                            subscription.queue.get(), timeout=idle_timeout
                        )
                except TimeoutError:
                    return
                yield event
        finally:
            self.unsubscribe(subscription)

    def listen_forever(self, subscription: Subscription) -> AsyncIterator[dict[str, Any]]:
        """Convenience wrapper with no idle timeout."""
        return self.listen(subscription)


class BusHealth:
    """Rolling bus-health counters (item 218).

    Tracks whether lanes are actually talking to each other in a useful way,
    versus generating volume. ``effectiveness`` is the number that matters: a
    bus with high traffic and low effectiveness is expensive noise, and this
    class exists so that is visible rather than assumed away.
    """

    def __init__(self) -> None:
        self.sent = 0
        self.delivered = 0
        self.replied = 0
        self.dropped = 0
        self.blocking_sent = 0
        self.by_type: dict[str, int] = {}

    def record_sent(self, *, blocking: bool = False) -> None:
        self.sent += 1
        if blocking:
            self.blocking_sent += 1

    def record_delivered(self, count: int) -> None:
        self.delivered += count

    def record_reply(self) -> None:
        self.replied += 1

    def record_dropped(self, count: int = 1) -> None:
        self.dropped += count

    def record_type(self, event_type: str) -> None:
        self.by_type[event_type] = self.by_type.get(event_type, 0) + 1

    @property
    def reply_rate(self) -> float:
        return round(self.replied / self.sent, 4) if self.sent else 0.0

    def snapshot(self) -> dict[str, Any]:
        return {
            "sent": self.sent,
            "delivered": self.delivered,
            "replied": self.replied,
            "dropped": self.dropped,
            "blocking_sent": self.blocking_sent,
            "reply_rate": self.reply_rate,
            "by_type": dict(sorted(self.by_type.items(), key=lambda kv: -kv[1])),
        }


__all__ = ["DEFAULT_QUEUE_SIZE", "BusHealth", "EventBus", "Subscription"]
