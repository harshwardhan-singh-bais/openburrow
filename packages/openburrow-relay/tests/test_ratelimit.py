"""The rate limiter.

Both surfaces share this implementation, so a bug here is a bug in the HTTP
limiter *and* the WebSocket limiter. That is the cost of having one limiter, and
these tests are how the cost is paid down.
"""

from __future__ import annotations

import time

import pytest

from openburrow.relay.ratelimit import BucketRegistry, TokenBucket

pytestmark = pytest.mark.unit


class TestTokenBucket:
    def test_burst_is_admitted_then_refused(self) -> None:
        bucket = TokenBucket(rate=1.0, burst=3)
        assert [bucket.allow() for _ in range(3)] == [True, True, True]
        assert bucket.allow() is False

    def test_refills_over_time(self) -> None:
        bucket = TokenBucket(rate=1000.0, burst=1)
        assert bucket.allow() is True
        assert bucket.allow() is False
        time.sleep(0.01)  # 1000/s => 10 tokens in 10ms, capped at burst
        assert bucket.allow() is True

    def test_never_exceeds_burst(self) -> None:
        bucket = TokenBucket(rate=1000.0, burst=5)
        time.sleep(0.02)
        # Refill is clamped to `burst`, not unbounded.
        assert bucket.tokens == pytest.approx(5.0, abs=0.1)

    def test_cost_is_charged_per_unit(self) -> None:
        """One frame carrying ten events must cost ten tokens.

        Charging per frame would let a client send one frame with a thousand
        events and pay for one.
        """
        bucket = TokenBucket(rate=0.001, burst=10)
        assert bucket.allow(cost=10) is True
        assert bucket.allow(cost=1) is False

    def test_cost_larger_than_burst_is_never_allowed(self) -> None:
        bucket = TokenBucket(rate=1.0, burst=5)
        assert bucket.allow(cost=6) is False

    def test_retry_after_is_zero_when_room_exists(self) -> None:
        assert TokenBucket(rate=1.0, burst=5).retry_after() == 0.0

    def test_retry_after_is_proportional_to_the_deficit(self) -> None:
        bucket = TokenBucket(rate=2.0, burst=1)
        bucket.allow()  # empty
        # One token at 2/s is 0.5s.
        assert bucket.retry_after() == pytest.approx(0.5, abs=0.05)

    def test_zero_rate_disables_limiting(self) -> None:
        bucket = TokenBucket(rate=0, burst=0)
        assert bucket.enabled is False
        assert all(bucket.allow() for _ in range(100))
        assert bucket.retry_after() == 0.0

    def test_reset_refills(self) -> None:
        bucket = TokenBucket(rate=0.001, burst=4)
        for _ in range(4):
            bucket.allow()
        assert bucket.allow() is False
        bucket.reset()
        assert bucket.allow() is True

    def test_zero_elapsed_does_not_add_tokens(self) -> None:
        # A monotonic clock can return the same value twice in fast succession;
        # a negative elapsed would otherwise *remove* tokens.
        bucket = TokenBucket(rate=100.0, burst=2)
        bucket._refill(now=1_000.0)
        bucket._refill(now=1_000.0)
        assert bucket.tokens == pytest.approx(2.0)


class TestBucketRegistry:
    def test_buckets_are_distinct_per_key(self) -> None:
        registry = BucketRegistry(rate=1.0, burst=1)
        assert registry.get("a").allow() is True
        # "b" has its own bucket and must not be affected by "a" being empty.
        assert registry.get("b").allow() is True
        assert registry.get("a").allow() is False

    def test_same_key_returns_the_same_bucket(self) -> None:
        registry = BucketRegistry(rate=1.0, burst=1)
        assert registry.get("a") is registry.get("a")

    def test_idle_buckets_are_evicted(self) -> None:
        """Without eviction this leaks memory keyed on attacker-controlled input.

        Room ids and client addresses are both supplied by callers, so a registry
        that never forgets is a slow denial of service.
        """
        registry = BucketRegistry(rate=1.0, burst=1, idle_ttl_s=0.0)
        registry.get("a")
        registry.get("b")
        assert len(registry) == 2
        time.sleep(0.001)
        assert registry.sweep() == 2
        assert len(registry) == 0

    def test_recently_used_buckets_survive_a_sweep(self) -> None:
        registry = BucketRegistry(rate=1.0, burst=1, idle_ttl_s=60.0)
        registry.get("a")
        assert registry.sweep() == 0
        assert len(registry) == 1

    def test_drop_removes_one_bucket(self) -> None:
        registry = BucketRegistry(rate=1.0, burst=1)
        registry.get("a")
        registry.get("b")
        registry.drop("a")
        assert len(registry) == 1

    def test_drop_of_an_unknown_key_is_harmless(self) -> None:
        BucketRegistry(rate=1.0, burst=1).drop("never-seen")
