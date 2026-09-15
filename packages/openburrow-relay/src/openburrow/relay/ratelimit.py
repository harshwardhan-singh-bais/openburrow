"""One rate limiter for the whole service.

The obvious approach is a library: `slowapi` on the HTTP routes and something
else on the WebSocket. That was rejected because it produces two limiters with
two configurations and two failure modes, and because a limiter that only
guards HTTP is a limiter you can walk around by staying on the socket — which is
precisely the surface that carries the most traffic here.

So there is one implementation, `TokenBucket`, and both surfaces use it.

A token bucket rather than a fixed window because the traffic this protects
against is bursty and legitimate: a lane finishing a task emits a dozen events
in a second, then nothing for a minute. A fixed window either rejects that burst
or has to be set so loose it protects nothing. A bucket with a burst of 200 and
a refill of 50/s admits the burst and still bounds the sustained rate.

The clock is `time.monotonic`, never wall time. A bucket keyed on wall time
misbehaves in exactly the situation you need it — an NTP step or a suspended VM
either grants a huge burst or freezes the bucket.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

#: How often an opportunistic sweep runs. Once a minute is plenty for a 15-minute
#: idle TTL: the cost of sweeping on every lookup would be O(n) per request.
SWEEP_INTERVAL_S = 60.0


@dataclass(slots=True)
class TokenBucket:
    """A leaky-bucket rate limiter.

    ``rate <= 0`` disables limiting entirely (every call to :meth:`allow`
    succeeds). That is deliberate rather than an error: an operator running the
    relay behind their own gateway may genuinely want the relay to stay out of
    the way, and the startup warnings call it out.
    """

    rate: float
    burst: int
    _tokens: float = field(init=False)
    _updated: float = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._tokens = float(max(self.burst, 0))
        self._updated = time.monotonic()

    @property
    def enabled(self) -> bool:
        return self.rate > 0 and self.burst > 0

    def _refill(self, now: float | None = None) -> None:
        if not self.enabled:
            return
        now = time.monotonic() if now is None else now
        elapsed = now - self._updated
        if elapsed <= 0:
            return
        self._tokens = min(float(self.burst), self._tokens + elapsed * self.rate)
        self._updated = now

    def allow(self, cost: float = 1.0) -> bool:
        """Consume ``cost`` tokens if available. Returns whether it succeeded."""
        if not self.enabled:
            return True
        self._refill()
        if self._tokens >= cost:
            self._tokens -= cost
            return True
        return False

    def retry_after(self, cost: float = 1.0) -> float:
        """Seconds until ``cost`` tokens would be available.

        Returns ``0.0`` when the bucket is disabled or already has room, so a
        caller can put the value straight into a ``Retry-After`` header without
        a special case.
        """
        if not self.enabled:
            return 0.0
        self._refill()
        deficit = cost - self._tokens
        if deficit <= 0:
            return 0.0
        return deficit / self.rate

    @property
    def tokens(self) -> float:
        """Current fill level. Refills first so the number is not stale."""
        self._refill()
        return self._tokens

    def reset(self) -> None:
        """Refill to full. Used when a connection is re-authenticated."""
        self._tokens = float(max(self.burst, 0))
        self._updated = time.monotonic()


class BucketRegistry:
    """Named buckets that expire when nobody has touched them.

    Without eviction this is a memory leak keyed on attacker-controlled strings
    (room ids, client addresses), which is a slow way to take the service down.
    ``sweep()`` is called opportunistically rather than on a timer so there is no
    background task to supervise.
    """

    def __init__(self, rate: float, burst: int, *, idle_ttl_s: float = 900.0) -> None:
        self.rate = rate
        self.burst = burst
        self.idle_ttl_s = idle_ttl_s
        self._buckets: dict[str, TokenBucket] = {}
        self._touched: dict[str, float] = {}
        self._last_sweep = time.monotonic()

    def get(self, key: str) -> TokenBucket:
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = TokenBucket(rate=self.rate, burst=self.burst)
            self._buckets[key] = bucket
        self._touched[key] = time.monotonic()
        self._maybe_sweep()
        return bucket

    def drop(self, key: str) -> None:
        self._buckets.pop(key, None)
        self._touched.pop(key, None)

    def sweep(self) -> int:
        """Evict idle buckets. Returns how many were removed."""
        now = time.monotonic()
        stale = [k for k, seen in self._touched.items() if now - seen > self.idle_ttl_s]
        for key in stale:
            self._buckets.pop(key, None)
            self._touched.pop(key, None)
        self._last_sweep = now
        return len(stale)

    def _maybe_sweep(self) -> None:
        if time.monotonic() - self._last_sweep > SWEEP_INTERVAL_S:
            self.sweep()

    def __len__(self) -> int:
        return len(self._buckets)


__all__ = ["SWEEP_INTERVAL_S", "BucketRegistry", "TokenBucket"]
