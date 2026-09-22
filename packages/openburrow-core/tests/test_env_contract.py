"""The documented environment interface, checked against the code that reads it.

``.env.example`` is the contract between this project and anyone deploying it:
305 variables, each with a comment saying what it does. Until this file existed,
nothing verified that any of them reached the code. The result was that **all 205
``OPENBURROW_*`` variables were silently ignored** — ``Settings`` had no
``env_prefix``, so pydantic-settings matched bare field names, ``OPENBURROW_ENV``
was dropped by ``extra="ignore"``, and a shell's unrelated ``ENV=production``
became ``settings.env`` in its place.

Three separate defects lived in that gap, and each one is a test below:

1. **No prefix.** ``OPENBURROW_*`` matched nothing. ``OPENBURROW_POLICY_ENFORCE``,
   ``OPENBURROW_DAEMON_SOCKET``, ``OPENBURROW_A2A_ENABLED`` — inert.
2. **No ``NoDecode`` on list settings.** pydantic-settings JSON-decodes any
   non-scalar field before validation, so ``OPENBURROW_POLICY_ALLOWED_COMMANDS=
   git,npm`` raised ``SettingsError`` from ``prepare_field_value`` and the
   ``mode="before"`` CSV validator never ran. All eleven list settings were
   affected, and the validator that looks like it handles them was unreachable.
3. **Two knobs nothing merged.** ``policy_default_action`` and
   ``policy_allowed_paths`` were declared, documented, and read by no one.

The tests here are deliberately about the *interface* rather than about any one
setting, because the failure mode was that the interface looked fine. A setting
that is documented and unreadable is worse than one that is absent: the second is
a missing feature, the first is a lie the user has no way to check.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from openburrow.core.config.settings import Settings, clear_settings_cache, get_settings

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[3]
ENV_EXAMPLE = REPO_ROOT / ".env.example"

#: ``OPENBURROW_*`` names in ``.env.example`` that no ``Settings`` field reads.
#: Each needs a reason, because the whole point of this file is that "documented
#: and unreadable" must not be an accident. An entry here is a promise that
#: something else reads the variable, or that the feature is not built yet.
DIRECT_READ_ELSEWHERE = {
    # Read by `BurrowPaths` before `Settings` exists — it decides where the
    # runtime directory is, so it cannot itself be stored in one.
    "OPENBURROW_HOME",
    # Same category: resolved while locating the repository.
    "OPENBURROW_REPO_ROOT",
    # Read by the relay's test conftest to opt into integration tests.
    "OPENBURROW_TEST_DB_URL",
    # Documented ahead of the stages that implement them. Kept listed so that
    # adding the variable without the reader is a visible choice rather than an
    # oversight — see FEATURE_STATUS.md for the stage each belongs to.
    "OPENBURROW_TEST_TIMEOUT_S",
    "OPENBURROW_EMAIL_DIGEST",
    "OPENBURROW_GITHUB_DEVICE_FLOW",
    "OPENBURROW_SSH_KNOWN_HOSTS",
    "OPENBURROW_RELAY_ALLOWED_ORIGINS",
    "OPENBURROW_WEB_RELAY_URL",
}


def parse_env_example() -> dict[str, str]:
    """Every ``KEY=value`` in ``.env.example``, comments and quotes stripped.

    Trailing ``# comment`` is removed the way a shell would treat it, because the
    file is full of them and a naive parse would feed ``"development   # development
    | staging | production"`` to a ``Literal``.
    """
    values: dict[str, str] = {}
    for raw_line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        key = key.strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            continue
        value = re.sub(r"\s+#.*$", "", raw).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def readable_names() -> set[str]:
    """Every environment variable name ``Settings`` will actually look up."""
    names: set[str] = set()
    for field_name, field in Settings.model_fields.items():
        names.add(f"OPENBURROW_{field_name.upper()}")
        alias = field.validation_alias
        for choice in getattr(alias, "choices", []) or []:
            if isinstance(choice, str):
                names.add(choice.upper())
    return names


@pytest.fixture
def clean_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Give each test an un-cached ``Settings`` and a clean slate afterwards.

    ``get_settings`` is ``lru_cache``d, so without this a settings object built by
    an earlier test keeps answering and the environment under test is not the
    environment in force.
    """
    clear_settings_cache()
    yield
    clear_settings_cache()


class TestTheDocumentedInterfaceIsReal:
    def test_env_example_exists_and_is_substantial(self) -> None:
        assert ENV_EXAMPLE.is_file(), "`.env.example` is the environment contract"
        assert len(parse_env_example()) > 250

    def test_the_whole_env_example_loads(
        self, monkeypatch: pytest.MonkeyPatch, clean_settings: None
    ) -> None:
        """The end-to-end check: every documented variable, applied at once.

        This is the test that would have caught the list settings. Setting
        ``OPENBURROW_POLICY_ALLOWED_COMMANDS=git,npm,pnpm`` raised ``SettingsError``
        from pydantic-settings' decoder, so a deployment that copied
        ``.env.example`` could not start at all — and nothing noticed, because
        nothing ever loaded the file.
        """
        for key, value in parse_env_example().items():
            monkeypatch.setenv(key, value)
        settings = Settings()  # must not raise
        assert settings.env == "development"
        assert settings.policy_allowed_commands[:3] == ["git", "npm", "pnpm"]

    def test_no_documented_openburrow_variable_is_orphaned(self) -> None:
        """A variable nobody reads is a promise the project does not keep."""
        readable = readable_names()
        orphans = sorted(
            name
            for name in parse_env_example()
            if name.startswith("OPENBURROW_")
            and name not in readable
            and name not in DIRECT_READ_ELSEWHERE
        )
        assert orphans == [], (
            "documented but unread: "
            + ", ".join(orphans)
            + " — add a Settings field, or list it in DIRECT_READ_ELSEWHERE with a reason"
        )

    def test_every_listed_exception_still_exists_in_the_file(self) -> None:
        """Keeps the exception list honest: no stale entries for removed vars."""
        declared = set(parse_env_example())
        stale = sorted(name for name in DIRECT_READ_ELSEWHERE if name not in declared)
        assert stale == [], (
            f"DIRECT_READ_ELSEWHERE names variables .env.example no longer has: {stale}"
        )


class TestTheNamespace:
    def test_a_prefixed_string_setting_reaches_its_field(
        self, monkeypatch: pytest.MonkeyPatch, clean_settings: None
    ) -> None:
        """Every ``str`` field, checked one at a time.

        The previous implementation passed this for provider keys and failed for
        everything else, because the provider keys happened to have the right bare
        names. Checking all of them is what distinguishes "the namespace works"
        from "the namespace works for the two variables anyone tried".
        """
        sentinel = "openburrow-sentinel-value"
        checked = 0
        for field_name, field in Settings.model_fields.items():
            if field.annotation is not str or field_name == "log_level":
                continue
            monkeypatch.setenv(f"OPENBURROW_{field_name.upper()}", sentinel)
            settings = Settings()
            assert getattr(settings, field_name) == sentinel, (
                f"OPENBURROW_{field_name.upper()} did not reach settings.{field_name}"
            )
            checked += 1
        assert checked > 80, "the sweep should cover most of the surface"

    def test_provider_native_names_still_work(
        self, monkeypatch: pytest.MonkeyPatch, clean_settings: None
    ) -> None:
        """The exception, and it is load-bearing.

        ``ANTHROPIC_API_KEY`` is read by the SDK as well as by us; renaming it
        would mean maintaining two copies of one credential.
        """
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-native")
        monkeypatch.setenv("GITHUB_TOKEN", "gh-native")
        monkeypatch.setenv("AIDER_BIN", "/opt/aider")
        settings = Settings()
        assert settings.anthropic_api_key == "sk-native"
        assert settings.github_token == "gh-native"
        assert settings.aider_bin == "/opt/aider"

    def test_native_fields_also_accept_the_prefixed_form(
        self, monkeypatch: pytest.MonkeyPatch, clean_settings: None
    ) -> None:
        monkeypatch.setenv("OPENBURROW_ANTHROPIC_API_KEY", "sk-prefixed")
        assert Settings().anthropic_api_key == "sk-prefixed"

    @pytest.mark.parametrize("name", ["ENV", "LOG_LEVEL", "POLICY_ENFORCE", "DAEMON_PORT"])
    def test_a_bare_name_is_not_read(
        self, name: str, monkeypatch: pytest.MonkeyPatch, clean_settings: None
    ) -> None:
        """The other half of the defect, and the more dangerous half.

        With no prefix, ``ENV=production`` in a shell silently became
        ``settings.env``. A variable that belongs to somebody else's tool
        changing OpenBurrow's behaviour is worse than one that does nothing.
        """
        monkeypatch.setenv(name, "1")
        settings = Settings()
        field_name = name.lower()
        assert str(getattr(settings, field_name)) != "1"


class TestCsvLists:
    @pytest.mark.parametrize(
        "field_name",
        [
            "a2a_external_peers",
            "mcp_config_paths",
            "mcp_deny_servers",
            "adapters_enabled",
            "policy_allowed_commands",
            "policy_denied_commands",
            "policy_allowed_paths",
            "policy_denied_paths",
            "sandbox_network_allowlist",
            "notify_on",
            "relay_cors_origins",
        ],
    )
    def test_a_comma_separated_value_parses(
        self, field_name: str, monkeypatch: pytest.MonkeyPatch, clean_settings: None
    ) -> None:
        """``NoDecode`` is what makes this reachable.

        Without it pydantic-settings JSON-decodes the raw string first and raises
        ``SettingsError``, so the ``mode="before"`` CSV validator never runs. The
        validator existing is not the same as the validator being used.
        """
        monkeypatch.setenv(f"OPENBURROW_{field_name.upper()}", "alpha, beta ,gamma")
        assert getattr(Settings(), field_name) == ["alpha", "beta", "gamma"]

    def test_an_empty_value_is_an_empty_list(
        self, monkeypatch: pytest.MonkeyPatch, clean_settings: None
    ) -> None:
        monkeypatch.setenv("OPENBURROW_POLICY_ALLOWED_COMMANDS", "")
        assert Settings().policy_allowed_commands == []

    def test_a_single_value_is_a_one_element_list(
        self, monkeypatch: pytest.MonkeyPatch, clean_settings: None
    ) -> None:
        monkeypatch.setenv("OPENBURROW_POLICY_ALLOWED_PATHS", ".")
        assert Settings().policy_allowed_paths == ["."]

    def test_a_pattern_containing_a_space_survives(
        self, monkeypatch: pytest.MonkeyPatch, clean_settings: None
    ) -> None:
        """``rm -rf /`` is a command pattern with a space in it.

        Splitting on whitespace as well as commas would silently turn one rule
        into three, none of which matches anything.
        """
        monkeypatch.setenv("OPENBURROW_POLICY_DENIED_COMMANDS", "rm -rf /,curl|sh")
        assert Settings().policy_denied_commands == ["rm -rf /", "curl|sh"]


class TestCaching:
    def test_settings_are_cached(
        self, monkeypatch: pytest.MonkeyPatch, clean_settings: None
    ) -> None:
        monkeypatch.setenv("OPENBURROW_LOG_LEVEL", "WARNING")
        assert get_settings() is get_settings()
        assert get_settings().log_level == "WARNING"

    def test_the_cache_must_be_cleared_for_a_change_to_take_effect(
        self, monkeypatch: pytest.MonkeyPatch, clean_settings: None
    ) -> None:
        """Why the fixture in ``conftest.py`` calls ``clear_settings_cache``.

        A test that sets an environment variable and does not clear the cache
        keeps the previous settings object, so the isolation is real only for
        whichever test happened to run first.
        """
        monkeypatch.setenv("OPENBURROW_LOG_LEVEL", "WARNING")
        assert get_settings().log_level == "WARNING"
        monkeypatch.setenv("OPENBURROW_LOG_LEVEL", "ERROR")
        assert get_settings().log_level == "WARNING"
        clear_settings_cache()
        assert get_settings().log_level == "ERROR"


# --- the frontend's half of the same contract -------------------------------

WEB_SRC = REPO_ROOT / "apps" / "web" / "src"

#: The frontend reads its own namespace, so `readable_names()` cannot see it.
#: These are the ``NEXT_PUBLIC_*`` names ``.env.example`` documents that no file
#: under ``apps/web/src`` reads. Kept as a mapping rather than a set so that each
#: entry has to carry a reason.
NEXT_PUBLIC_READ_ELSEWHERE: dict[str, str] = {}


def documented_next_public_names() -> set[str]:
    return {name for name in parse_env_example() if name.startswith("NEXT_PUBLIC_")}


def next_public_names_the_web_app_reads() -> set[str]:
    """Every ``process.env.NEXT_PUBLIC_*`` literal under ``apps/web/src``.

    Deliberately a literal-text scan rather than an import: the point is to
    compare the *documented* interface against the *written* one, and Next bakes
    the name it finds in the source, so a name that only ever appears in a
    comment is exactly as inert as one that appears nowhere.
    """
    pattern = re.compile(r"process\.env\.(NEXT_PUBLIC_[A-Z0-9_]+)")
    found: set[str] = set()
    for path in WEB_SRC.rglob("*"):
        if path.suffix not in {".ts", ".tsx"}:
            continue
        found.update(pattern.findall(path.read_text(encoding="utf-8")))
    return found


class TestTheFrontendInterfaceIsReal:
    """The same contract, for the variables ``Settings`` cannot see.

    ``test_no_documented_openburrow_variable_is_orphaned`` filters on the
    ``OPENBURROW_`` prefix, so the frontend's namespace was never covered — and
    that is precisely where the drift happened. ``.env.example`` declared seven
    ``NEXT_PUBLIC_*`` variables and ``apps/web/src`` read four entirely
    different ones, with **zero** overlap in either direction. Anyone who
    copied the template and filled it in configured nothing, and because the
    names look almost right (``..._API_URL`` beside ``..._API``) there was no
    error to notice: Next simply bakes the name it finds, and the fallback path
    is a working default.

    A prefix filter is the kind of check that passes because it examined the
    wrong half, so these two tests are named for the direction they cover.
    """

    def test_the_web_app_reads_something(self) -> None:
        """Guards the guard: a scan that finds nothing would pass both tests below."""
        assert WEB_SRC.is_dir(), "apps/web/src is where the frontend's env reads live"
        assert len(next_public_names_the_web_app_reads()) >= 4

    def test_every_documented_next_public_variable_is_read(self) -> None:
        """A documented variable nobody reads is a promise the project does not keep."""
        read = next_public_names_the_web_app_reads()
        orphans = sorted(
            name
            for name in documented_next_public_names()
            if name not in read and name not in NEXT_PUBLIC_READ_ELSEWHERE
        )
        assert orphans == [], (
            "documented but read by nothing under apps/web/src: "
            + ", ".join(orphans)
            + " — wire it, delete it, or list it in NEXT_PUBLIC_READ_ELSEWHERE with a reason"
        )

    def test_every_next_public_variable_the_web_app_reads_is_documented(self) -> None:
        """The other direction, which is the one that made the template useless.

        A variable the code reads and the template never mentions is a variable
        every deployer sets wrongly, because the template is where they look.
        """
        documented = documented_next_public_names()
        undocumented = sorted(next_public_names_the_web_app_reads() - documented)
        assert undocumented == [], (
            "read by apps/web/src but absent from .env.example: "
            + ", ".join(undocumented)
            + " — add it to the FRONTEND section with a comment"
        )

    def test_the_exception_map_has_no_stale_entries(self) -> None:
        declared = documented_next_public_names()
        stale = sorted(name for name in NEXT_PUBLIC_READ_ELSEWHERE if name not in declared)
        assert stale == [], (
            f"NEXT_PUBLIC_READ_ELSEWHERE names variables .env.example no longer has: {stale}"
        )
