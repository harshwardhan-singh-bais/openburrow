"""Configuration validation.

Every test here asserts on a *refusal*. The settings object has no interesting
happy path — a valid configuration is just a dataclass — so the value is entirely
in the cases it rejects and the cases where it declines to guess.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from openburrow.core.errors import ConfigError
from openburrow.relay.config import MIN_SECRET_BYTES, RelaySettings

pytestmark = pytest.mark.unit


class TestRequiredSettings:
    def test_missing_secret_is_refused(self, test_db_url: str) -> None:
        with pytest.raises(ConfigError) as excinfo:
            RelaySettings.from_env({"OPENBURROW_RELAY_DB_URL": test_db_url})
        assert "JWT_SECRET" in excinfo.value.message
        # The hint must say *why* it is not auto-generated, or the next person
        # "fixes" it by generating one at boot — which silently logs everyone out
        # on every restart.
        assert "auto-generated" in (excinfo.value.hint or "")

    def test_short_secret_is_refused(self, test_db_url: str) -> None:
        with pytest.raises(ConfigError) as excinfo:
            RelaySettings.from_env(
                {
                    "OPENBURROW_RELAY_JWT_SECRET": "short",
                    "OPENBURROW_RELAY_DB_URL": test_db_url,
                }
            )
        assert "shorter" in excinfo.value.message
        assert MIN_SECRET_BYTES == 32

    def test_secret_exactly_at_the_floor_is_accepted(self, test_db_url: str) -> None:
        # Boundary case, because an off-by-one here rejects a secret that is fine.
        secret = "x" * MIN_SECRET_BYTES
        settings = RelaySettings.from_env(
            {"OPENBURROW_RELAY_JWT_SECRET": secret, "OPENBURROW_RELAY_DB_URL": test_db_url}
        )
        assert settings.jwt_secret == secret

    def test_missing_db_url_is_refused(self, test_secret: str) -> None:
        with pytest.raises(ConfigError) as excinfo:
            RelaySettings.from_env({"OPENBURROW_RELAY_JWT_SECRET": test_secret})
        assert "DB_URL" in excinfo.value.message

    def test_error_carries_a_stable_code(self, test_db_url: str) -> None:
        # The CLI and the frontend branch on `code`, not on the message.
        with pytest.raises(ConfigError) as excinfo:
            RelaySettings.from_env({"OPENBURROW_RELAY_DB_URL": test_db_url})
        assert excinfo.value.to_dict()["code"] == "openburrow.config_error"


class TestPostgresOnly:
    @pytest.mark.parametrize(
        "url",
        [
            "sqlite:///burrow.db",
            "sqlite+aiosqlite:///burrow.db",
            "mysql://user:pass@localhost/burrow",
            "file:burrow.db",
        ],
    )
    def test_non_postgres_urls_are_refused(self, url: str, test_secret: str) -> None:
        with pytest.raises(ConfigError) as excinfo:
            RelaySettings.from_env(
                {"OPENBURROW_RELAY_JWT_SECRET": test_secret, "OPENBURROW_RELAY_DB_URL": url}
            )
        assert "Postgres" in excinfo.value.message
        # The hint has to explain the asymmetry with the daemon, which *does* use
        # SQLite. Without that, the refusal reads as an inconsistency.
        assert "restart" in (excinfo.value.hint or "")

    @pytest.mark.parametrize(
        "url",
        [
            "postgresql://burrow:burrow@localhost/burrow",
            "postgresql+asyncpg://burrow:burrow@localhost/burrow",
            "postgres://burrow:burrow@localhost/burrow",
        ],
    )
    def test_postgres_spellings_are_normalised(self, url: str, test_secret: str) -> None:
        settings = RelaySettings.from_env(
            {"OPENBURROW_RELAY_JWT_SECRET": test_secret, "OPENBURROW_RELAY_DB_URL": url}
        )
        # Every accepted spelling must reach asyncpg in the form it understands,
        # and must not end up double-prefixed.
        assert settings.async_db_url.startswith("postgresql+asyncpg://")
        assert settings.async_db_url.count("+asyncpg") == 1


class TestNumericParsing:
    def test_bad_integer_names_the_variable(
        self, settings_factory: Callable[..., RelaySettings]
    ) -> None:
        with pytest.raises(ConfigError) as excinfo:
            settings_factory(PORT="not-a-port")
        assert "OPENBURROW_RELAY_PORT" in excinfo.value.message

    def test_bad_float_names_the_variable(
        self, settings_factory: Callable[..., RelaySettings]
    ) -> None:
        with pytest.raises(ConfigError) as excinfo:
            settings_factory(EVENT_RATE_PER_S="fast")
        assert "OPENBURROW_RELAY_EVENT_RATE_PER_S" in excinfo.value.message

    def test_out_of_range_port_is_refused(
        self, settings_factory: Callable[..., RelaySettings]
    ) -> None:
        with pytest.raises(ConfigError):
            settings_factory(PORT="70000")

    def test_booleans_accept_the_usual_spellings(
        self, settings_factory: Callable[..., RelaySettings]
    ) -> None:
        for raw in ("1", "true", "TRUE", "yes", "on"):
            assert settings_factory(TLS_TERMINATED=raw).tls_terminated is True
        for raw in ("0", "false", "no", "off", ""):
            assert settings_factory(TLS_TERMINATED=raw).tls_terminated is False


class TestUnknownVariables:
    def test_unknown_variables_are_collected_not_dropped(
        self, settings_factory: Callable[..., RelaySettings]
    ) -> None:
        # A typo'd variable name is otherwise a silent no-op, which is the class
        # of bug that costs an afternoon of staring at a config that "looks right".
        settings = settings_factory(EVENT_RATE_PER_SECOND="50")
        assert "EVENT_RATE_PER_SECOND" in settings.extra
        assert settings.event_rate_per_s == 50.0  # the real setting kept its default

    def test_admin_key_lands_in_extra(self, settings_factory: Callable[..., RelaySettings]) -> None:
        # The admin surface reads its key from `extra`, so this is the contract
        # that makes `_require_admin` work without a dedicated settings field.
        settings = settings_factory(ADMIN_KEY="operator-secret")
        assert settings.extra["ADMIN_KEY"] == "operator-secret"


class TestWarnings:
    def test_loopback_with_no_origins_is_quiet(self, settings: RelaySettings) -> None:
        assert settings.is_loopback
        assert settings.warnings() == []

    def test_public_bind_without_tls_warns(
        self, settings_factory: Callable[..., RelaySettings]
    ) -> None:
        settings = settings_factory(HOST="0.0.0.0", ALLOWED_ORIGINS="https://app.example.com")
        assert any("TLS_TERMINATED" in w for w in settings.warnings())

    def test_public_bind_without_origins_warns_separately(
        self, settings_factory: Callable[..., RelaySettings]
    ) -> None:
        settings = settings_factory(HOST="0.0.0.0", TLS_TERMINATED="1")
        assert any("ALLOWED_ORIGINS" in w for w in settings.warnings())

    def test_unlimited_rate_warns(self, settings_factory: Callable[..., RelaySettings]) -> None:
        settings = settings_factory(EVENT_RATE_PER_S="0")
        assert any("rate limiting is disabled" in w for w in settings.warnings())

    def test_zero_retention_warns(self, settings_factory: Callable[..., RelaySettings]) -> None:
        settings = settings_factory(RETENTION_DAYS="0")
        assert any("without bound" in w for w in settings.warnings())

    def test_metrics_off_warns_rather_than_hiding(
        self, settings_factory: Callable[..., RelaySettings]
    ) -> None:
        settings = settings_factory(METRICS_ENABLED="0")
        assert any("/metrics will report as such" in w for w in settings.warnings())
