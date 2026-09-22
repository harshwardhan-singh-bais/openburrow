"""Stage 13 tests: the custom-adapter trust gate.

``registry._load_custom`` documents three trust modes — ``prompt`` | ``allow`` |
``deny`` — as "an explicit trust decision ... rather than silently". The
``prompt`` branch logged a warning and ``continue``d: it imported nothing, asked
nothing, and was indistinguishable from ``deny``. Because ``prompt`` is the
default, the shipped behaviour was that **custom adapters never load**, and the
hint printed alongside told the operator to set ``=allow`` — that is, the way to
make the feature work was to turn the gate off rather than answer a question.

Two things are worth pinning here beyond "it asks now":

* **A gate that cannot ask must not answer yes.** ``prompt`` under a daemon, a
  pipe or CI has no terminal; the safe reading of silence is refusal. A trust
  gate that defaults to yes when nobody is watching is a delay, not a gate.
* **A failure to ask is not consent.** A prompter that raises must resolve to
  *deny*, never to *allow* — the exception path is the one place a security
  control most often degrades into an unconditional ``import``.

The tests avoid asserting on log events; structlog is not wired to ``caplog``
in this repo. Where the distinction matters (asked vs. not asked, imported vs.
not imported) they assert on the *effect* — a marker file the adapter writes at
import time — because that is the thing an attacker would exploit.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

from openburrow.adapters import registry as registry_module
from openburrow.adapters.registry import AdapterRegistry, build_registry
from openburrow.core.config.settings import Settings

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
class _FakeStream(io.StringIO):
    """A stream whose ``isatty`` we control, for the unattended-run tests."""

    def __init__(self, *, tty: bool) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


_ADAPTER_TEMPLATE = """
from pathlib import Path

Path({marker!r}).write_text("imported")

from openburrow.adapters.harnesses.mock import MockAdapter


class {cls}(MockAdapter):
    name = {name!r}
    description = "Custom adapter used to test the trust gate."
"""


def write_adapter(directory: Path, stem: str, *, adapter_name: str | None = None) -> Path:
    """Write a custom adapter module and return the marker path it creates.

    The module writes a marker file **at import time**. That is deliberate: it
    lets a test distinguish "the gate refused" from "the gate was never
    consulted", which a check on ``registry.names()`` alone cannot do.
    """
    directory.mkdir(parents=True, exist_ok=True)
    marker = directory / f"{stem}.imported"
    cls = "".join(part.capitalize() for part in stem.split("_")) + "Adapter"
    source = _ADAPTER_TEMPLATE.format(
        marker=str(marker),
        cls=cls,
        name=adapter_name or stem.replace("_", "-"),
    )
    (directory / f"{stem}.py").write_text(source, encoding="utf-8")
    return marker


def settings_for(directory: Path, trust: str) -> Settings:
    return Settings(custom_adapter_dir=str(directory), custom_adapter_trust=trust)


def always(answer: bool) -> tuple[list[Path], registry_module.ConfirmFn]:
    """A confirm callback that records the paths it was asked about."""
    asked: list[Path] = []

    def confirm(path: Path) -> bool:
        asked.append(path)
        return answer

    return asked, confirm


# --------------------------------------------------------------------------
# allow / deny
# --------------------------------------------------------------------------
class TestAllowLoadsWithoutAsking:
    def test_allow_imports_the_adapter(self, tmp_path: Path) -> None:
        marker = write_adapter(tmp_path, "allowed_probe")
        _asked, confirm = always(answer=False)
        registry = AdapterRegistry(settings_for(tmp_path, "allow"), confirm=confirm)

        assert "allowed-probe" in registry.names()
        assert marker.exists(), "the adapter module was never imported"

    def test_allow_never_consults_the_gate(self, tmp_path: Path) -> None:
        """``allow`` is a standing answer; asking anyway would be noise."""
        write_adapter(tmp_path, "allowed_probe")
        asked, confirm = always(answer=False)
        registry = AdapterRegistry(settings_for(tmp_path, "allow"), confirm=confirm)

        registry.names()

        assert asked == []


class TestDenyNeverLoads:
    def test_deny_imports_nothing(self, tmp_path: Path) -> None:
        marker = write_adapter(tmp_path, "denied_probe")
        registry = AdapterRegistry(settings_for(tmp_path, "deny"))

        assert "denied-probe" not in registry.names()
        assert not marker.exists(), "deny still imported the module"

    def test_deny_does_not_ask(self, tmp_path: Path) -> None:
        """A policy of "never" should not produce a question about each file."""
        write_adapter(tmp_path, "denied_probe")
        asked, confirm = always(answer=True)
        registry = AdapterRegistry(settings_for(tmp_path, "deny"), confirm=confirm)

        registry.names()

        assert asked == []


# --------------------------------------------------------------------------
# prompt — the mode that used to be a no-op
# --------------------------------------------------------------------------
class TestPromptActuallyPrompts:
    def test_approval_loads_the_adapter(self, tmp_path: Path) -> None:
        """The regression: under the old code this could never succeed."""
        marker = write_adapter(tmp_path, "approved_probe")
        _asked, confirm = always(answer=True)
        registry = AdapterRegistry(settings_for(tmp_path, "prompt"), confirm=confirm)

        assert "approved-probe" in registry.names()
        assert marker.exists()

    def test_the_gate_is_asked_about_the_module_path(self, tmp_path: Path) -> None:
        write_adapter(tmp_path, "asked_probe")
        asked, confirm = always(answer=True)
        registry = AdapterRegistry(settings_for(tmp_path, "prompt"), confirm=confirm)

        registry.names()

        assert asked == [tmp_path / "asked_probe.py"]

    def test_refusal_does_not_import(self, tmp_path: Path) -> None:
        marker = write_adapter(tmp_path, "refused_probe")
        asked, confirm = always(answer=False)
        registry = AdapterRegistry(settings_for(tmp_path, "prompt"), confirm=confirm)

        assert "refused-probe" not in registry.names()
        assert not marker.exists(), "a refused adapter was imported anyway"
        assert asked == [tmp_path / "refused_probe.py"]

    def test_the_question_is_asked_once_per_registry(self, tmp_path: Path) -> None:
        """``names()``, ``resolve()`` and ``describe_all()`` all load custom
        adapters. Asking on each of them would prompt the same question three
        times for one ``burrow adapters list``."""
        write_adapter(tmp_path, "once_probe")
        asked, confirm = always(answer=True)
        registry = AdapterRegistry(settings_for(tmp_path, "prompt"), confirm=confirm)

        registry.names()
        registry.names()
        registry.describe_all()

        assert len(asked) == 1

    def test_each_module_is_asked_about_separately(self, tmp_path: Path) -> None:
        """Refusing one adapter must not silently decide the others.

        Per-module granularity is the point of the gate: an operator who trusts
        one custom adapter and not its neighbour should be able to say so.
        """
        good = write_adapter(tmp_path, "aa_wanted")
        bad = write_adapter(tmp_path, "bb_unwanted")
        asked: list[Path] = []

        def confirm(path: Path) -> bool:
            asked.append(path)
            return path.stem == "aa_wanted"

        registry = AdapterRegistry(settings_for(tmp_path, "prompt"), confirm=confirm)

        assert "aa-wanted" in registry.names()
        assert good.exists()
        assert not bad.exists()
        assert [p.stem for p in asked] == ["aa_wanted", "bb_unwanted"]

    def test_a_refused_adapter_does_not_abort_the_others(self, tmp_path: Path) -> None:
        write_adapter(tmp_path, "aa_refused")
        write_adapter(tmp_path, "bb_accepted")

        registry = AdapterRegistry(
            settings_for(tmp_path, "prompt"),
            confirm=lambda path: path.stem == "bb_accepted",
        )

        names = registry.names()

        assert "bb-accepted" in names
        assert "aa-refused" not in names


# --------------------------------------------------------------------------
# fail closed
# --------------------------------------------------------------------------
class TestTheGateIsFailClosed:
    def test_a_prompter_that_raises_denies(self, tmp_path: Path) -> None:
        """The exception path must not become an implicit ``import``."""
        marker = write_adapter(tmp_path, "boom_probe")

        def explode(path: Path) -> bool:
            raise RuntimeError("the approval UI is not available")

        registry = AdapterRegistry(settings_for(tmp_path, "prompt"), confirm=explode)

        assert "boom-probe" not in registry.names()
        assert not marker.exists()

    def test_a_non_boolean_answer_is_coerced_not_trusted(self, tmp_path: Path) -> None:
        """A falsy return is a refusal, whatever type it arrives as."""
        marker = write_adapter(tmp_path, "falsy_probe")
        registry = AdapterRegistry(
            settings_for(tmp_path, "prompt"),
            confirm=lambda _path: None,  # type: ignore[return-value,arg-type]
        )

        assert "falsy-probe" not in registry.names()
        assert not marker.exists()


# --------------------------------------------------------------------------
# the default prompter
# --------------------------------------------------------------------------
class TestDefaultPrompter:
    def test_unattended_runs_deny(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No terminal to ask on means the answer is no, not a guess."""
        monkeypatch.setattr(sys, "stdin", _FakeStream(tty=False))
        monkeypatch.setattr(sys, "stderr", _FakeStream(tty=True))

        assert registry_module._ask_terminal(Path("probe.py")) is False

    def test_a_piped_stdout_alone_still_denies(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Both ends are required: reading a question nobody can see is not
        asking, and a half-attached terminal is the CI case."""
        monkeypatch.setattr(sys, "stdin", _FakeStream(tty=True))
        monkeypatch.setattr(sys, "stderr", _FakeStream(tty=False))

        assert registry_module._ask_terminal(Path("probe.py")) is False

    @pytest.mark.parametrize(
        ("answer", "expected"),
        [("y", True), ("Y", True), ("yes", True), ("yes\n", True), ("n", False), ("", False)],
    )
    def test_answers_are_read_conservatively(
        self, monkeypatch: pytest.MonkeyPatch, answer: str, expected: bool
    ) -> None:
        monkeypatch.setattr(sys, "stdin", _FakeStream(tty=True))
        monkeypatch.setattr(sys, "stderr", _FakeStream(tty=True))
        monkeypatch.setattr("builtins.input", lambda: answer)

        assert registry_module._ask_terminal(Path("probe.py")) is expected

    def test_eof_denies(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "stdin", _FakeStream(tty=True))
        monkeypatch.setattr(sys, "stderr", _FakeStream(tty=True))

        def eof() -> str:
            raise EOFError

        monkeypatch.setattr("builtins.input", eof)

        assert registry_module._ask_terminal(Path("probe.py")) is False

    def test_ctrl_c_denies(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Interrupting the question is not an answer to it."""
        monkeypatch.setattr(sys, "stdin", _FakeStream(tty=True))
        monkeypatch.setattr(sys, "stderr", _FakeStream(tty=True))

        def interrupt() -> str:
            raise KeyboardInterrupt

        monkeypatch.setattr("builtins.input", interrupt)

        assert registry_module._ask_terminal(Path("probe.py")) is False

    def test_the_prompt_is_written_to_stderr(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``burrow adapters list --json`` writes machine-readable output to
        stdout. A prompt spliced into a JSON document is a bug that only shows
        up in a pipeline — so the question goes to stderr."""
        stderr = _FakeStream(tty=True)
        monkeypatch.setattr(sys, "stdin", _FakeStream(tty=True))
        monkeypatch.setattr(sys, "stderr", stderr)
        monkeypatch.setattr("builtins.input", lambda: "n")

        registry_module._ask_terminal(Path("probe.py"))

        assert "Load it?" in stderr.getvalue()

    def test_the_prompt_names_the_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An approval prompt that does not say what is being approved is a
        rubber stamp."""
        stderr = _FakeStream(tty=True)
        monkeypatch.setattr(sys, "stdin", _FakeStream(tty=True))
        monkeypatch.setattr(sys, "stderr", stderr)
        monkeypatch.setattr("builtins.input", lambda: "n")

        registry_module._ask_terminal(Path("/somewhere/suspicious_adapter.py"))

        assert "suspicious_adapter.py" in stderr.getvalue()

    def test_the_default_registry_uses_it(self, tmp_path: Path) -> None:
        """The wired-up default must be the terminal prompter, not a stub."""
        registry = AdapterRegistry(settings_for(tmp_path, "prompt"))

        assert registry._confirm is registry_module._ask_terminal


# --------------------------------------------------------------------------
# robustness of the loop itself
# --------------------------------------------------------------------------
class TestTheLoopSurvivesBadModules:
    def test_one_unimportable_module_does_not_stop_the_rest(self, tmp_path: Path) -> None:
        write_adapter(tmp_path, "zz_good")
        (tmp_path / "mm_broken.py").write_text("raise RuntimeError('boom')\n", encoding="utf-8")

        registry = AdapterRegistry(settings_for(tmp_path, "allow"))

        assert "zz-good" in registry.names()

    def test_underscore_prefixed_files_are_ignored(self, tmp_path: Path) -> None:
        """``_helpers.py`` is a support module, not an adapter to trust."""
        write_adapter(tmp_path, "_private")
        asked, confirm = always(answer=True)
        registry = AdapterRegistry(settings_for(tmp_path, "prompt"), confirm=confirm)

        registry.names()

        assert asked == []

    def test_a_missing_directory_is_not_an_error(self, tmp_path: Path) -> None:
        """No custom-adapter directory is the common case, not a failure."""
        asked, confirm = always(answer=True)
        registry = AdapterRegistry(settings_for(tmp_path / "nope", "prompt"), confirm=confirm)

        names = registry.names()

        assert "mock" in names
        assert asked == [], "a directory that does not exist produced a prompt"

    def test_build_registry_forwards_the_confirm_callback(self, tmp_path: Path) -> None:
        marker = write_adapter(tmp_path, "forwarded_probe")
        asked, confirm = always(answer=True)

        registry = build_registry(settings_for(tmp_path, "prompt"), confirm=confirm)

        assert "forwarded-probe" in registry.names()
        assert marker.exists()
        assert asked == [tmp_path / "forwarded_probe.py"]
