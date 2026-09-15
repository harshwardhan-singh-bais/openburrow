"""Relay configuration.

Read from the environment and nowhere else. A service that also reads a config
file is a service whose behaviour depends on a file that is not in the container
image, which is the mechanism by which staging and production quietly diverge.

Two settings have no default and the process refuses to start without them:

* ``OPENBURROW_RELAY_JWT_SECRET`` — generating a random secret on boot would
  silently invalidate every token issued before a restart. That presents as
  "everyone gets logged out randomly", which is a miserable thing to debug, so
  the failure is moved to startup where it is obvious.
* ``OPENBURROW_RELAY_DB_URL`` — and it must be Postgres. A relay that cannot be
  restarted without losing its rooms is not a relay.

Everything else has a default that is safe for a single-machine deployment.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from openburrow.core.errors import ConfigError

ENV_PREFIX = "OPENBURROW_RELAY_"

#: Below this, HS256 is weaker than the invite tokens it is protecting.
MIN_SECRET_BYTES = 32

#: Prefixes accepted for the database URL. Postgres only — see the module docstring.
_POSTGRES_SCHEMES = ("postgresql://", "postgresql+asyncpg://", "postgres://")

#: The top of the valid TCP port range. Named rather than inlined because the
#: check reads as `1 <= port <= 65535`, which looks like a typo waiting to happen.
MAX_PORT = 65535


@dataclass(frozen=True, slots=True)
class RelaySettings:
    """Everything the relay needs to run."""

    # --- identity ---------------------------------------------------------
    jwt_secret: str
    jwt_issuer: str = "openburrow-relay"
    jwt_audience: str = "openburrow"
    #: 12 hours. Long enough for a working day, short enough that a leaked token
    #: stops mattering within a shift.
    token_ttl_s: int = 12 * 60 * 60

    # --- storage ----------------------------------------------------------
    db_url: str = ""
    db_pool_min: int = 1
    db_pool_max: int = 10
    #: Events older than this are eligible for pruning. 0 disables pruning.
    retention_days: int = 30

    # --- network ----------------------------------------------------------
    host: str = "127.0.0.1"
    port: int = 8787
    allowed_origins: tuple[str, ...] = ()
    #: Set when a reverse proxy terminates TLS in front of the relay. Only then
    #: are X-Forwarded-* headers believed.
    tls_terminated: bool = False

    # --- limits -----------------------------------------------------------
    event_rate_per_s: float = 50.0
    event_burst: int = 200
    auth_rate_per_s: float = 1.0
    auth_burst: int = 10
    #: 256 KiB. A bus event is a summary plus a payload, not a file transfer.
    max_frame_bytes: int = 256 * 1024
    max_connections_per_room: int = 64
    max_connections_per_member: int = 8
    #: Outbound queue depth per connection. Overflow drops the *newest* frame and
    #: marks the client lagged, rather than blocking the fan-out for everyone.
    outbound_queue_size: int = 512
    max_doc_frame_bytes: int = 1024 * 1024

    # --- observability ----------------------------------------------------
    log_level: str = "INFO"
    log_format: str = "json"
    #: Emit Prometheus metrics. Off means the endpoint still exists and reports
    #: that it is disabled, rather than 404ing.
    metrics_enabled: bool = True

    extra: dict[str, str] = field(default_factory=dict)

    # --- construction -----------------------------------------------------
    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> RelaySettings:
        """Build settings from the process environment.

        ``environ`` is injectable so tests do not have to mutate the real
        environment and leak state between cases.
        """
        return _from_mapping(dict(os.environ) if environ is None else environ)

    # --- derived ----------------------------------------------------------
    @property
    def async_db_url(self) -> str:
        """The URL as asyncpg needs to see it.

        ``postgres://`` is the Heroku-era spelling and SQLAlchemy rejects it; a
        plain ``postgresql://`` is ambiguous once an async driver is in play. Both
        are normalised rather than rejected, because refusing a URL that every
        other tool accepts is a bad first experience.
        """
        url = self.db_url
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://") :]
        if url.startswith("postgresql://"):
            url = "postgresql+asyncpg://" + url[len("postgresql://") :]
        return url

    @property
    def is_loopback(self) -> bool:
        return self.host in {"127.0.0.1", "::1", "localhost"}

    def warnings(self) -> list[str]:
        """Operator warnings, logged at startup.

        Not errors: every one of these is a legitimate configuration in some
        deployment. But each is also a plausible accident, and a line in the log
        is far cheaper than finding out from an incident.
        """
        found: list[str] = []
        if not self.is_loopback and not self.tls_terminated:
            found.append(
                f"binding to {self.host} but OPENBURROW_RELAY_TLS_TERMINATED is not set: "
                "tokens and event payloads will cross the network in the clear"
            )
        if not self.allowed_origins and not self.is_loopback:
            found.append(
                "no OPENBURROW_RELAY_ALLOWED_ORIGINS set on a non-loopback bind: "
                "browsers will refuse the WebSocket handshake"
            )
        if self.retention_days == 0:
            found.append("retention pruning is disabled; relay_events will grow without bound")
        if not self.metrics_enabled:
            found.append("metrics are disabled; /metrics will report as such rather than 404")
        if self.event_rate_per_s <= 0:
            found.append("event rate limiting is disabled (rate <= 0 means unlimited)")
        return found


def _from_mapping(environ: dict[str, str]) -> RelaySettings:
    """The single implementation. Reads from a plain mapping rather than
    ``os.environ`` directly so that tests, containers, and the CLI can all
    construct settings from an explicit source."""

    def get(name: str, default: str | None = None) -> str | None:
        return environ.get(ENV_PREFIX + name) or default

    def get_int(name: str, default: int) -> int:
        raw = get(name)
        if raw is None:
            return default
        try:
            return int(raw)
        except ValueError as exc:
            raise ConfigError(
                f"{ENV_PREFIX}{name} must be an integer, got {raw!r}",
                context={"variable": ENV_PREFIX + name},
                cause=exc,
            ) from exc

    def get_float(name: str, default: float) -> float:
        raw = get(name)
        if raw is None:
            return default
        try:
            return float(raw)
        except ValueError as exc:
            raise ConfigError(
                f"{ENV_PREFIX}{name} must be a number, got {raw!r}",
                context={"variable": ENV_PREFIX + name},
                cause=exc,
            ) from exc

    def get_bool(name: str, default: bool) -> bool:
        raw = get(name)
        if raw is None:
            return default
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}

    def get_list(name: str) -> tuple[str, ...]:
        raw = get(name)
        if not raw:
            return ()
        return tuple(part.strip() for part in str(raw).split(",") if part.strip())

    secret = get("JWT_SECRET")
    if not secret:
        raise ConfigError(
            "OPENBURROW_RELAY_JWT_SECRET is not set",
            hint=(
                'Generate one with `python -c "import secrets;print(secrets.token_urlsafe(48))"` '
                "and set it in the relay's environment. It is intentionally not auto-generated: "
                "a random secret per boot invalidates every issued token on restart."
            ),
            context={"variable": ENV_PREFIX + "JWT_SECRET"},
        )
    if len(secret.encode("utf-8")) < MIN_SECRET_BYTES:
        raise ConfigError(
            f"OPENBURROW_RELAY_JWT_SECRET is shorter than {MIN_SECRET_BYTES} bytes",
            hint="HS256 is only as strong as the secret. Use at least 32 bytes of entropy.",
            context={"variable": ENV_PREFIX + "JWT_SECRET", "length": len(secret)},
        )

    db_url = get("DB_URL")
    if not db_url:
        raise ConfigError(
            "OPENBURROW_RELAY_DB_URL is not set",
            hint="The relay requires Postgres. Example: postgresql://burrow:burrow@localhost:5432/burrow",
            context={"variable": ENV_PREFIX + "DB_URL"},
        )
    if not str(db_url).startswith(_POSTGRES_SCHEMES):
        raise ConfigError(
            f"OPENBURROW_RELAY_DB_URL must be a Postgres URL, got {str(db_url).split(':', 1)[0]!r}",
            hint=(
                "SQLite is not supported here. The relay is the one component that must survive "
                "a restart, and a file database on a single host cannot do that for more than one host."
            ),
            context={"variable": ENV_PREFIX + "DB_URL"},
        )

    port = get_int("PORT", 8787)
    if not 1 <= port <= MAX_PORT:
        raise ConfigError(
            f"OPENBURROW_RELAY_PORT must be between 1 and {MAX_PORT}, got {port}",
            context={"variable": ENV_PREFIX + "PORT"},
        )

    known = {
        "JWT_SECRET",
        "JWT_ISSUER",
        "JWT_AUDIENCE",
        "TOKEN_TTL_S",
        "DB_URL",
        "DB_POOL_MIN",
        "DB_POOL_MAX",
        "RETENTION_DAYS",
        "HOST",
        "PORT",
        "ALLOWED_ORIGINS",
        "TLS_TERMINATED",
        "EVENT_RATE_PER_S",
        "EVENT_BURST",
        "AUTH_RATE_PER_S",
        "AUTH_BURST",
        "MAX_FRAME_BYTES",
        "MAX_CONNECTIONS_PER_ROOM",
        "MAX_CONNECTIONS_PER_MEMBER",
        "OUTBOUND_QUEUE_SIZE",
        "MAX_DOC_FRAME_BYTES",
        "LOG_LEVEL",
        "LOG_FORMAT",
        "METRICS_ENABLED",
    }
    # The relay *client* lives under the same prefix — `OPENBURROW_RELAY_ENABLED`,
    # `_URL`, `_TOKEN`, `_ORG` and friends are what a CLI sets to *talk to* a
    # relay. A machine that both runs a relay and dials one has all of them set,
    # so treating them as unknown would be a false positive on every single
    # boot. A warning that always fires is a warning people learn to ignore,
    # which costs us the real typos this check exists to catch.
    client_variables = {
        "ENABLED",
        "URL",
        "TOKEN",
        "ORG",
        "WORKSPACE",
        "ROLE",
        "RECONNECT_MAX_S",
        "QUEUE_MAX",
        "PRIVATE_BY_DEFAULT",
    }

    # Unknown OPENBURROW_RELAY_* variables are collected rather than ignored. A
    # typo'd variable name is otherwise a silent no-op, which is exactly the
    # class of bug that costs an afternoon.
    extra = {
        key[len(ENV_PREFIX) :]: value
        for key, value in environ.items()
        if key.startswith(ENV_PREFIX)
        and key[len(ENV_PREFIX) :] not in known
        and key[len(ENV_PREFIX) :] not in client_variables
    }

    return RelaySettings(
        jwt_secret=str(secret),
        jwt_issuer=str(get("JWT_ISSUER", "openburrow-relay")),
        jwt_audience=str(get("JWT_AUDIENCE", "openburrow")),
        token_ttl_s=get_int("TOKEN_TTL_S", 12 * 60 * 60),
        db_url=str(db_url),
        db_pool_min=get_int("DB_POOL_MIN", 1),
        db_pool_max=get_int("DB_POOL_MAX", 10),
        retention_days=get_int("RETENTION_DAYS", 30),
        host=str(get("HOST", "127.0.0.1")),
        port=port,
        allowed_origins=get_list("ALLOWED_ORIGINS"),
        tls_terminated=get_bool("TLS_TERMINATED", False),
        event_rate_per_s=get_float("EVENT_RATE_PER_S", 50.0),
        event_burst=get_int("EVENT_BURST", 200),
        auth_rate_per_s=get_float("AUTH_RATE_PER_S", 1.0),
        auth_burst=get_int("AUTH_BURST", 10),
        max_frame_bytes=get_int("MAX_FRAME_BYTES", 256 * 1024),
        max_connections_per_room=get_int("MAX_CONNECTIONS_PER_ROOM", 64),
        max_connections_per_member=get_int("MAX_CONNECTIONS_PER_MEMBER", 8),
        outbound_queue_size=get_int("OUTBOUND_QUEUE_SIZE", 512),
        max_doc_frame_bytes=get_int("MAX_DOC_FRAME_BYTES", 1024 * 1024),
        log_level=str(get("LOG_LEVEL", "INFO")).upper(),
        log_format=str(get("LOG_FORMAT", "json")),
        metrics_enabled=get_bool("METRICS_ENABLED", True),
        extra=extra,
    )


__all__ = ["ENV_PREFIX", "MAX_PORT", "MIN_SECRET_BYTES", "RelaySettings"]
