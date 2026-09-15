"""Console entry point for the relay.

Deliberately thin. It parses arguments, validates configuration, and hands off to
uvicorn. Anything cleverer belongs in `create_app`, where it is reachable from
tests.

``--check`` is the one non-obvious flag: it validates configuration and exits
without binding a port. That turns "the relay will not start and the logs are
buried in a container orchestrator" into a one-line command an operator can run
locally against the same environment.
"""

from __future__ import annotations

import argparse
import sys

from openburrow.core.errors import OpenBurrowError
from openburrow.core.logging import configure_logging, get_logger
from openburrow.core.version import __version__
from openburrow.relay.config import ENV_PREFIX, RelaySettings

log = get_logger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openburrow-relay",
        description="Relay OpenBurrow bus events between daemons on different machines.",
        epilog=(
            "Configuration is read from the environment only. "
            f"The required variables are {ENV_PREFIX}JWT_SECRET and {ENV_PREFIX}DB_URL; "
            "see .env.example for the rest."
        ),
    )
    parser.add_argument("--host", help="Bind address. Overrides the environment.")
    parser.add_argument("--port", type=int, help="Bind port. Overrides the environment.")
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Reload on source changes. Development only.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate configuration and exit. Does not bind a port.",
    )
    parser.add_argument(
        "--print-config",
        action="store_true",
        help="Print the resolved configuration with secrets redacted, then exit.",
    )
    parser.add_argument("--version", action="version", version=f"openburrow-relay {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        settings = RelaySettings.from_env()
    except OpenBurrowError as exc:
        # Printed rather than logged: this runs before logging is configured, and
        # a config error the operator cannot see is worse than an ugly one.
        print(f"configuration error: {exc}", file=sys.stderr)
        return 3

    if args.host:
        settings = _replace(settings, host=args.host)
    if args.port:
        settings = _replace(settings, port=args.port)

    if args.print_config:
        _print_config(settings)
        return 0

    if args.check:
        print(f"configuration is valid (relay {__version__})")
        for warning in settings.warnings():
            print(f"  warning: {warning}")
        return 0

    configure_logging(level=settings.log_level, fmt=settings.log_format)

    # Imported here so that `--check` and `--version` do not pay for building an
    # app, and so a missing optional dependency surfaces as a clear error rather
    # than an import failure at module load.
    import uvicorn

    from openburrow.relay.app import create_app

    app = create_app(settings)
    log.info("relay.starting", host=settings.host, port=settings.port, reload=args.reload)

    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        reload=args.reload,
        # uvicorn's own access log duplicates the middleware's structured line,
        # and the middleware one carries the request id.
        access_log=False,
        log_config=None,
        # WebSocket ping settings: a client that vanishes without a FIN should be
        # reaped in about 40s rather than holding a room slot indefinitely.
        ws_ping_interval=20.0,
        ws_ping_timeout=20.0,
    )
    return 0


def _replace(settings: RelaySettings, **changes: object) -> RelaySettings:
    """Return a copy of ``settings`` with fields replaced.

    ``dataclasses.replace`` would be the obvious call, but ``RelaySettings`` is a
    frozen slotted dataclass and ``replace`` re-runs ``__init__``, which is fine
    here — it is used for two CLI flags and nothing else.
    """
    from dataclasses import replace as _dc_replace

    return _dc_replace(settings, **changes)  # type: ignore[arg-type]


def _print_config(settings: RelaySettings) -> None:
    """Print the resolved configuration with the secrets redacted.

    The JWT secret is never printed, not even truncated. A debug flag that
    prints part of a secret is a debug flag that ends up in a pastebin.
    """
    redacted = {
        "JWT_SECRET": "***set***" if settings.jwt_secret else "***missing***",
        "DB_URL": settings.db_url.replace(settings.db_url.split("://")[-1].split("@")[0], "***")
        if "@" in settings.db_url
        else settings.db_url,
    }
    print(f"openburrow-relay {__version__}")
    print(f"  host             {settings.host}")
    print(f"  port             {settings.port}")
    print(f"  database         {redacted['DB_URL']}")
    print(f"  jwt secret       {redacted['JWT_SECRET']}")
    print(f"  jwt issuer       {settings.jwt_issuer}")
    print(f"  token ttl        {settings.token_ttl_s}s")
    print(f"  retention        {settings.retention_days}d")
    print(f"  event rate       {settings.event_rate_per_s}/s (burst {settings.event_burst})")
    print(f"  max conns/room   {settings.max_connections_per_room}")
    print(f"  max conns/member {settings.max_connections_per_member}")
    print(f"  max frame        {settings.max_frame_bytes} bytes")
    print(f"  origins          {', '.join(settings.allowed_origins) or '(none)'}")
    print(f"  tls terminated   {settings.tls_terminated}")
    print(f"  metrics          {'on' if settings.metrics_enabled else 'off'}")
    if settings.extra:
        print(f"  extra vars       {', '.join(sorted(settings.extra))}")
    warnings = settings.warnings()
    if warnings:
        print("  warnings:")
        for warning in warnings:
            print(f"    - {warning}")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
