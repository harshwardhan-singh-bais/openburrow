"""The relay application.

`create_app` is a factory rather than a module-level ``app`` so that tests can
build an application against injected settings and a throwaway database. A
module-level app would make the second test in a file reuse the first test's
connections, which is the classic shape of a suite that passes alone and fails
together.

The lifespan does four things in a fixed order: connect the store, ensure the
schema, mark the instance ready, and start the retention sweeper. Readiness is a
flag rather than a check because ``/readyz`` already re-checks the database —
``state.ready`` records the weaker but distinct fact that *startup completed*,
and a relay that never finished starting should not receive traffic even if
Postgres happens to be up.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from openburrow.core.logging import configure_logging, get_logger
from openburrow.core.version import __version__
from openburrow.relay.config import RelaySettings
from openburrow.relay.errors import RateLimitedError, RelayError
from openburrow.relay.metrics import METRICS, RelayMetrics
from openburrow.relay.routes import router
from openburrow.relay.state import AppState

log = get_logger(__name__)

#: How often retention pruning runs. Hourly: the work is proportional to the
#: number of rooms, and a room's retention is measured in days, so anything
#: more frequent is wasted queries.
PRUNE_INTERVAL_S = 3600.0


def create_app(
    settings: RelaySettings | None = None,
    *,
    metrics: RelayMetrics | None = None,
    auto_schema: bool | None = None,
) -> FastAPI:
    """Build the relay application.

    ``auto_schema`` defaults to ``OPENBURROW_RELAY_AUTO_SCHEMA``, which defaults
    to on. A production deployment should turn it off and run migrations; the
    startup log says so rather than leaving it to be discovered.
    """
    resolved = settings or RelaySettings.from_env()
    resolved_metrics = metrics or METRICS

    if auto_schema is None:
        auto_schema = (resolved.extra.get("AUTO_SCHEMA") or "1").strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
        }

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        state: AppState = app.state.relay
        configure_logging(level=resolved.log_level, fmt=resolved.log_format)

        for warning in resolved.warnings():
            log.warning("relay.config_warning", detail=warning)
        if resolved.extra:
            # Unknown OPENBURROW_RELAY_* variables are surfaced, not swallowed: a
            # typo'd name is otherwise a silent no-op.
            log.info("relay.unrecognised_settings", keys=sorted(resolved.extra))

        await state.store.connect()
        if auto_schema:
            await state.store.ensure_schema()
            log.warning(
                "relay.auto_schema_enabled",
                detail=(
                    "schema auto-creation is on. A production deployment should run migrations "
                    "and set OPENBURROW_RELAY_AUTO_SCHEMA=0."
                ),
            )

        state.ready = True
        pruner = asyncio.create_task(_prune_loop(state))
        log.info(
            "relay.ready",
            version=__version__,
            host=resolved.host,
            port=resolved.port,
            auto_schema=auto_schema,
        )
        try:
            yield
        finally:
            state.ready = False
            pruner.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pruner
            await state.store.close()
            log.info("relay.stopped")

    app = FastAPI(
        title="OpenBurrow relay",
        version=__version__,
        summary="Relays bus events and brain-doc updates between OpenBurrow daemons.",
        lifespan=lifespan,
    )
    app.state.relay = AppState.build(resolved, metrics=resolved_metrics)

    if resolved.allowed_origins:
        # Explicit origins only. `allow_origins=["*"]` with credentials is the
        # combination browsers reject and operators keep trying anyway.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(resolved.allowed_origins),
            allow_credentials=True,
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-Admin-Key"],
            max_age=600,
        )

    _install_middleware(app)
    _install_error_handlers(app)
    app.include_router(router)
    return app


def _install_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def request_context(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Attach a request id and emit one log line per request.

        The id is echoed in the ``X-Request-Id`` response header, which is the
        difference between "a user says it failed" and a searchable log line.
        """
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        started = time.monotonic()
        try:
            response = await call_next(request)
        except Exception:
            elapsed_ms = (time.monotonic() - started) * 1000
            log.exception(
                "relay.request_failed",
                request_id=request_id,
                method=request.method,
                path=request.url.path,
                elapsed_ms=round(elapsed_ms, 1),
            )
            raise
        elapsed_ms = (time.monotonic() - started) * 1000
        response.headers["X-Request-Id"] = request_id
        # Health and metrics are polled constantly; logging them at INFO buries
        # everything else.
        if request.url.path not in {"/healthz", "/readyz", "/metrics"}:
            log.info(
                "relay.request",
                request_id=request_id,
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                elapsed_ms=round(elapsed_ms, 1),
            )
        return response


def _install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(RelayError)
    async def relay_error_handler(request: Request, exc: RelayError) -> JSONResponse:
        """Render a relay error as its declared status.

        The status comes off the exception rather than a mapping here, so a new
        error cannot be added without one. The body keeps ``code``/``hint``/
        ``context`` intact — the frontend renders those directly.
        """
        headers: dict[str, str] = {}
        if isinstance(exc, RateLimitedError):
            headers["Retry-After"] = str(max(1, int(exc.retry_after + 0.999)))
        log.info(
            "relay.error",
            code=exc.code,
            status=exc.status_code,
            path=request.url.path,
            context=exc.context,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.to_dict()},
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
        """422 with the same envelope as every other error.

        FastAPI's default body has a different shape from ours, which would make
        the frontend carry two error parsers for no benefit.
        """
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "openburrow.relay.bad_request",
                    "message": "the request body is not valid",
                    "context": {"errors": exc.errors()[:20]},
                }
            },
        )


async def _prune_loop(state: AppState) -> None:
    """Delete expired events on a timer.

    Failures are logged and swallowed: pruning is maintenance, and a database
    blip should not take the relay down. The next tick retries.
    """
    while True:
        await asyncio.sleep(PRUNE_INTERVAL_S)
        try:
            removed = await state.store.prune()
            if removed:
                log.info("relay.pruned_events", removed=removed)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("relay.prune_failed", error=str(exc))


__all__ = ["PRUNE_INTERVAL_S", "create_app"]
