"""Application state.

A single object hung off ``app.state`` rather than a module-level singleton. The
difference matters in tests: a module-level store cannot be pointed at a
different database between cases, so the second test either reuses the first
test's connection or monkeypatches a global — both of which produce tests that
pass in isolation and fail in a suite.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from openburrow.relay.config import RelaySettings
from openburrow.relay.hub import Hub
from openburrow.relay.metrics import METRICS, RelayMetrics
from openburrow.relay.ratelimit import BucketRegistry
from openburrow.relay.store import RelayStore


@dataclass(slots=True)
class AppState:
    """Everything the request handlers need, in one place."""

    settings: RelaySettings
    store: RelayStore
    hub: Hub
    metrics: RelayMetrics = field(default_factory=lambda: METRICS)
    #: Rate limiter for the HTTP surface. The WebSocket has one bucket per
    #: connection instead, since a single bucket per address would be shared by
    #: every tab a user has open.
    http_buckets: BucketRegistry = field(default_factory=lambda: BucketRegistry(rate=1.0, burst=10))
    started_at: float = field(default_factory=time.monotonic)
    ready: bool = False

    @classmethod
    def build(cls, settings: RelaySettings, *, metrics: RelayMetrics | None = None) -> AppState:
        resolved_metrics = metrics or METRICS
        return cls(
            settings=settings,
            store=RelayStore(settings, metrics=resolved_metrics),
            hub=Hub(settings, metrics=resolved_metrics),
            metrics=resolved_metrics,
            http_buckets=BucketRegistry(rate=settings.auth_rate_per_s, burst=settings.auth_burst),
        )

    @property
    def uptime_s(self) -> float:
        return time.monotonic() - self.started_at


__all__ = ["AppState"]
