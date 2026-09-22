"""The HTTP surface.

Uses `TestClient` against an application built with a deliberately unreachable
database. That is not a limitation — it is the point. The relay's most important
property at this layer is that it *degrades honestly*: `/readyz` must say the
database is down rather than returning 200, and a request that cannot be served
must say why in a machine-readable way rather than producing a bare 500.

The endpoints that need real rows are marked ``integration`` and live in
``test_store.py``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from openburrow.core.version import __version__
from openburrow.relay.app import create_app
from openburrow.relay.config import RelaySettings
from openburrow.relay.metrics import build_metrics

pytestmark = pytest.mark.unit

#: A port nothing is listening on, so `healthcheck` fails fast with a connection
#: error rather than hanging until a timeout.
UNREACHABLE_DB = "postgresql://burrow:burrow@127.0.0.1:1/burrow_test"


@pytest.fixture
def app_settings(settings_factory: Callable[..., RelaySettings]) -> RelaySettings:
    return settings_factory(DB_URL=UNREACHABLE_DB, LOG_FORMAT="console", LOG_LEVEL="CRITICAL")


@pytest.fixture
def client(app_settings: RelaySettings) -> Iterator[TestClient]:
    """A client that runs the lifespan.

    Entering the context manager is what runs startup, and startup is what sets
    ``state.ready``. A test that skipped it would be testing a relay that never
    booted.
    """
    app = create_app(app_settings, metrics=build_metrics(), auto_schema=False)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def cold_client(app_settings: RelaySettings) -> TestClient:
    """A client that never ran startup. Used to test the not-ready path."""
    app = create_app(app_settings, metrics=build_metrics(), auto_schema=False)
    return TestClient(app)


class TestHealth:
    def test_healthz_is_unconditional(self, client: TestClient) -> None:
        """Liveness must not depend on the database.

        A liveness probe that checks Postgres turns a database blip into a
        container restart loop, which is strictly worse than the blip.
        """
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"ok": True}

    def test_healthz_works_before_startup_completes(self, cold_client: TestClient) -> None:
        assert cold_client.get("/healthz").status_code == 200


class TestReadiness:
    def test_readyz_reports_503_when_the_database_is_unreachable(self, client: TestClient) -> None:
        response = client.get("/readyz")
        assert response.status_code == 503
        body = response.json()
        assert body["ok"] is False
        # The error is passed through, not swallowed into a bare status code.
        assert body["database"]["ok"] is False
        assert body["database"]["error"]

    def test_readyz_is_503_before_startup(self, cold_client: TestClient) -> None:
        response = cold_client.get("/readyz")
        assert response.status_code == 503
        assert response.json()["ok"] is False

    def test_readyz_redacts_the_database_password(self, client: TestClient) -> None:
        """A readiness probe is often the easiest endpoint to reach.

        It must never be a way to read the database password out of the process.
        """
        body = client.get("/readyz").text
        assert "burrow:burrow" not in body
        assert "***" in body or "127.0.0.1:1" in body

    def test_readyz_reports_uptime_and_connection_counts(self, client: TestClient) -> None:
        body = client.get("/readyz").json()
        assert body["uptime_s"] >= 0
        assert body["connections"] == {}
        assert body["rooms"] == {}


class TestMetrics:
    def test_metrics_are_served(self, client: TestClient) -> None:
        response = client.get("/metrics")
        assert response.status_code == 200
        assert "openburrow_relay_events_received_total" in response.text

    def test_disabled_metrics_explain_themselves_rather_than_404(
        self, settings_factory: Callable[..., RelaySettings]
    ) -> None:
        """A scrape that starts 404ing looks like a broken relay.

        Saying "disabled, here is the variable" is a five-second diagnosis
        instead of an incident.
        """
        settings = settings_factory(
            DB_URL=UNREACHABLE_DB, METRICS_ENABLED="0", LOG_LEVEL="CRITICAL"
        )
        app = create_app(settings, metrics=build_metrics(), auto_schema=False)
        with TestClient(app) as test_client:
            response = test_client.get("/metrics")
        assert response.status_code == 200
        assert "disabled" in response.text
        assert "METRICS_ENABLED" in response.text


class TestAuthRequired:
    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("get", "/rooms"),
            ("get", "/rooms/room_x"),
            ("get", "/rooms/room_x/events"),
            ("post", "/rooms/room_x/invites"),
            ("get", "/rooms/room_x/invites"),
        ],
    )
    def test_endpoints_require_a_token(self, client: TestClient, method: str, path: str) -> None:
        # A body is supplied for POSTs so that auth is reached before body
        # validation. Otherwise a missing token would report as a 422, which is
        # technically true and completely useless to the caller.
        kwargs: dict[str, Any] = {"json": {}} if method == "post" else {}
        response = getattr(client, method)(path, **kwargs)
        assert response.status_code == 401
        error = response.json()["error"]
        assert error["code"] == "openburrow.relay_auth_failed"
        assert "Bearer" in error["hint"]

    def test_a_malformed_token_is_401_not_500(self, client: TestClient) -> None:
        response = client.get("/rooms", headers={"Authorization": "Bearer not.a.token"})
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "openburrow.relay_auth_failed"

    def test_a_non_bearer_header_is_401(self, client: TestClient) -> None:
        response = client.get("/rooms", headers={"Authorization": "Basic abc123"})
        assert response.status_code == 401


class TestAdminSurface:
    def test_creating_a_room_without_a_configured_key_is_503(self, client: TestClient) -> None:
        """Disabled, not open.

        A missing environment variable defaulting to "allow everyone" is how
        admin endpoints end up on the internet. 503 says "not configured here",
        which points an operator at their environment rather than their
        credentials.
        """
        response = client.post("/rooms", json={"repo_slug": "github.com/acme/burrow"})
        assert response.status_code == 503
        error = response.json()["error"]
        assert error["code"] == "openburrow.relay.admin_disabled"
        assert "ADMIN_KEY" in error["hint"]

    def test_creating_a_room_with_a_configured_key_and_no_header_is_401(
        self, settings_factory: Callable[..., RelaySettings]
    ) -> None:
        settings = settings_factory(
            DB_URL=UNREACHABLE_DB, ADMIN_KEY="operator-secret", LOG_LEVEL="CRITICAL"
        )
        app = create_app(settings, metrics=build_metrics(), auto_schema=False)
        with TestClient(app) as test_client:
            response = test_client.post("/rooms", json={"repo_slug": "github.com/acme/burrow"})
        assert response.status_code == 401

    def test_creating_a_room_with_the_wrong_key_is_401(
        self, settings_factory: Callable[..., RelaySettings]
    ) -> None:
        settings = settings_factory(
            DB_URL=UNREACHABLE_DB, ADMIN_KEY="operator-secret", LOG_LEVEL="CRITICAL"
        )
        app = create_app(settings, metrics=build_metrics(), auto_schema=False)
        with TestClient(app) as test_client:
            response = test_client.post(
                "/rooms",
                json={"repo_slug": "github.com/acme/burrow"},
                headers={"X-Admin-Key": "wrong"},
            )
        assert response.status_code == 401


class TestValidation:
    def test_a_short_invite_is_a_422_in_our_envelope(self, client: TestClient) -> None:
        """FastAPI's default 422 body has a different shape from ours.

        Making the frontend carry two error parsers for no benefit is the kind of
        paper cut that never gets fixed.
        """
        response = client.post(
            "/auth/token", json={"invite": "short", "subject": "ada@example.com"}
        )
        assert response.status_code == 422
        error = response.json()["error"]
        assert error["code"] == "openburrow.relay.bad_request"
        assert "errors" in error["context"]

    def test_a_missing_field_is_a_422(self, client: TestClient) -> None:
        response = client.post("/auth/token", json={"invite": "a" * 40})
        assert response.status_code == 422

    def test_a_bad_role_is_a_400(self, client: TestClient) -> None:
        # Reaches the role check rather than body validation, so it is a 400 and
        # not a 422 — the body is well-formed, the value is not usable.
        response = client.post(
            "/rooms/room_x/invites",
            json={"role": "superuser"},
            headers={"Authorization": "Bearer not.a.token"},
        )
        # Auth is checked first, so this is a 401. The 400 path is covered in
        # test_store.py where a real token can be minted.
        assert response.status_code == 401


class TestRequestId:
    def test_every_response_carries_a_request_id(self, client: TestClient) -> None:
        response = client.get("/healthz")
        assert response.headers.get("X-Request-Id")

    def test_a_supplied_request_id_is_echoed(self, client: TestClient) -> None:
        """The difference between "a user says it failed" and a log line."""
        response = client.get("/healthz", headers={"X-Request-Id": "trace-me-123"})
        assert response.headers["X-Request-Id"] == "trace-me-123"


class TestOpenApi:
    def test_the_documented_surface_is_the_real_surface(self, client: TestClient) -> None:
        schema = client.get("/openapi.json").json()
        paths = set(schema["paths"])
        for expected in [
            "/readyz",
            "/auth/token",
            "/rooms",
            "/rooms/{room}",
            "/rooms/{room}/events",
            "/rooms/{room}/invites",
            "/invites/{invite_id}",
        ]:
            assert expected in paths, f"{expected} is missing from the OpenAPI schema"

    def test_health_and_metrics_are_excluded_from_the_schema(self, client: TestClient) -> None:
        paths = set(client.get("/openapi.json").json()["paths"])
        assert "/healthz" not in paths
        assert "/metrics" not in paths

    def test_the_version_is_reported(self, client: TestClient) -> None:
        assert client.get("/openapi.json").json()["info"]["version"] == __version__


class TestWebSocketAuth:
    def test_a_garbage_query_token_closes_with_4401(self, client: TestClient) -> None:
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises(WebSocketDisconnect) as excinfo:
            with client.websocket_connect("/rooms/room_x/stream?token=garbage") as ws:
                ws.receive_json()
        assert excinfo.value.code == 4401

    def test_a_wrong_first_frame_closes_with_4401(self, client: TestClient) -> None:
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises(WebSocketDisconnect) as excinfo:
            with client.websocket_connect("/rooms/room_x/stream") as ws:
                ws.send_json({"type": "publish", "events": []})
                ws.receive_json()
        assert excinfo.value.code == 4401

    def test_a_malformed_first_frame_closes_with_4401(self, client: TestClient) -> None:
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises(WebSocketDisconnect) as excinfo:
            with client.websocket_connect("/rooms/room_x/stream") as ws:
                ws.send_text("this is not json")
                ws.receive_json()
        assert excinfo.value.code == 4401

    def test_the_doc_socket_authenticates_the_same_way(self, client: TestClient) -> None:
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises(WebSocketDisconnect) as excinfo:
            with client.websocket_connect("/rooms/room_x/doc?token=garbage") as ws:
                ws.receive_json()
        assert excinfo.value.code == 4401
