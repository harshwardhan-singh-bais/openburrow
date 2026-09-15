"""Structured logging.

Two sinks, one call site:

* **console** — colourised, human-first, for a developer staring at a terminal.
* **json**     — one object per line, for the daemon's log file, the relay, and CI.

Every log record carries the same correlation fields so a session can be
reconstructed end to end from logs alone:

``session_id``  which session
``lane_id``     which lane (harness instance)
``task_id``     which A2A task
``trace_id``    OpenTelemetry trace, when tracing is on

Those fields are bound once via :func:`bind_context` and then appear on every
subsequent record in that async task. The daemon binds them per-lane, so a
multi-lane log file stays readable.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Iterator, MutableMapping
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import structlog

#: Correlation fields propagated into every log record.
CONTEXT_FIELDS: tuple[str, ...] = (
    "session_id",
    "lane_id",
    "task_id",
    "trace_id",
    "delegation_id",
    "harness",
)

#: The default is ``None`` rather than ``{}``, and that is load-bearing. A
#: mutable default is shared by every context that has not set the var, so one
#: caller doing ``ctx = _context.get(); ctx["lane_id"] = ...`` would write that
#: field into every other task's log records — lane attribution bleeding across
#: sessions, in the one subsystem whose output is the audit trail. Every reader
#: copies before mutating today, so the bug is latent rather than live; that is
#: an argument for removing the trap, not for keeping it. ``None`` makes the
#: unsafe version impossible to write by accident.
_context: ContextVar[dict[str, Any] | None] = ContextVar("openburrow_log_context", default=None)

_configured = False

_LEVELS: dict[str, int] = {
    "CRITICAL": logging.CRITICAL,
    "ERROR": logging.ERROR,
    "WARNING": logging.WARNING,
    "INFO": logging.INFO,
    "DEBUG": logging.DEBUG,
    "NOTSET": logging.NOTSET,
}


def resolve_level(level: str | int | None) -> int:
    if isinstance(level, int):
        return level
    name = (level or os.environ.get("OPENBURROW_LOG_LEVEL", "INFO")).upper()
    return _LEVELS.get(name, logging.INFO)


def _add_context(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog processor: merge the ambient context into the record."""
    for key, value in (_context.get() or {}).items():
        event_dict.setdefault(key, value)
    return event_dict


def _drop_none(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog processor: omit keys whose value is ``None`` to keep lines tight."""
    return {k: v for k, v in event_dict.items() if v is not None}


def configure_logging(
    *,
    level: str | int | None = None,
    fmt: str | None = None,
    log_file: Path | str | None = None,
    force: bool = False,
) -> None:
    """Idempotently configure structlog + the stdlib root logger.

    Called by the CLI entrypoint, the daemon bootstrap, and the relay app.
    Calling it twice is a no-op unless ``force=True``.
    """
    global _configured  # noqa: PLW0603 - idempotent one-time setup; a module
    # attribute is the correct scope for it, and a class wrapper would add a
    # layer purely to avoid the keyword.
    if _configured and not force:
        return

    resolved_level = resolve_level(level)
    resolved_fmt = (fmt or os.environ.get("OPENBURROW_LOG_FORMAT", "console")).lower()

    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        _add_context,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _drop_none,
    ]

    renderer: Any
    if resolved_fmt == "json":
        renderer = structlog.processors.JSONRenderer(sort_keys=True)
    else:
        renderer = structlog.dev.ConsoleRenderer(
            colors=sys.stderr.isatty(),
            # `pad_event_to`, not the older `pad_event`. The old name still works
            # but emits a DeprecationWarning on every `configure_logging` call,
            # which pollutes test output and hides real warnings.
            pad_event_to=34,
            exception_formatter=structlog.dev.plain_traceback,
        )

    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(resolved_level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(resolved_level)

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(logging.Formatter("%(message)s"))
    stream.setLevel(resolved_level)
    root.addHandler(stream)

    target = log_file or os.environ.get("OPENBURROW_LOG_FILE", "").strip()
    if target:
        path = Path(target).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter("%(message)s"))
        file_handler.setLevel(resolved_level)
        root.addHandler(file_handler)

    for noisy in ("asyncio", "aiosqlite", "httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(max(resolved_level, logging.WARNING))

    _configured = True


def get_logger(name: str | None = None, **initial: Any) -> structlog.stdlib.BoundLogger:
    """Return a bound logger. ``name`` should be the module ``__name__``."""
    configure_logging()
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name or "openburrow")
    return logger.bind(**initial) if initial else logger


@contextmanager
def bind_context(**fields: Any) -> Iterator[None]:
    """Temporarily bind correlation fields for the current async task.

    Example::

        with bind_context(session_id=sid, lane_id=lid):
            log.info("lane.started")     # carries session_id + lane_id
    """
    merged = {**(_context.get() or {}), **{k: v for k, v in fields.items() if v is not None}}
    token = _context.set(merged)
    try:
        yield
    finally:
        _context.reset(token)


def current_context() -> dict[str, Any]:
    """Snapshot of the ambient correlation fields — used when serialising events."""
    return dict(_context.get() or {})


class LogTimer:
    """Context manager that logs the duration of a block at DEBUG/INFO.

    Intended for anything that shells out to a harness, since those are the calls
    whose latency is worth knowing about. It is exported as public API, but note
    that nothing in the codebase uses it yet — the previous docstring claimed it
    was "used liberally", which was simply untrue and would have sent anyone
    debugging latency looking for call sites that do not exist.
    """

    # `_start` was missing from this tuple, and `__slots__` without a `__dict__`
    # means assigning an undeclared name raises rather than creating one — so
    # `__enter__` raised `AttributeError` on every single use, and the timer
    # could not time anything. A `__slots__` list is a hand-maintained mirror of
    # the attributes a class assigns, and nothing checks that the two agree;
    # this is what that costs.
    __slots__ = ("_event", "_fields", "_level", "_logger", "_start")

    def __init__(
        self,
        event: str,
        *,
        logger: structlog.stdlib.BoundLogger | None = None,
        level: str = "debug",
        **fields: Any,
    ) -> None:
        self._event = event
        self._logger = logger or get_logger("openburrow.timing")
        self._level = level
        self._fields = fields

    def __enter__(self) -> LogTimer:
        import time

        self._start = time.perf_counter()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        import time

        elapsed_ms = round((time.perf_counter() - self._start) * 1000, 2)
        payload = {**self._fields, "duration_ms": elapsed_ms}
        if exc_type is not None:
            payload["error"] = exc_type.__name__
        getattr(self._logger, self._level)(f"{self._event}.done", **payload)


__all__ = [
    "CONTEXT_FIELDS",
    "LogTimer",
    "bind_context",
    "configure_logging",
    "current_context",
    "get_logger",
    "resolve_level",
]
