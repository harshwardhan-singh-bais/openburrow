"""``burrow config migrate`` at the CLI layer: exit codes and output modes.

The core tests pin what a migration *means*. These pin the contract a script
depends on, which is a different question and the one the command exists to
serve: ``--check`` has to be usable as a CI gate, so its exit code must
distinguish "the committed configuration is current" from "it is not", and
``--write`` must not be able to run when someone meant ``--check``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from openburrow.cli.main import app

pytestmark = pytest.mark.integration

runner = CliRunner()

STALE = """# pre-versioning
project:
  name: acme-api
govrnance:
  require_delegation_authority: true
"""
CURRENT = "schema_version: 1\nproject:\n  name: acme-api\n"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway repo whose config the command is pointed at explicitly.

    ``--path`` rather than relying on discovery, so a failure here is about the
    command and not about ``find_repo_root`` walking somewhere unexpected.
    """
    (tmp_path / "openburrow.yaml").write_text(STALE, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def migrate(*args: str) -> Result:
    """Invoke the subcommand and hand back the click result."""
    return runner.invoke(app, ["config", "migrate", *args])


def test_check_fails_on_a_stale_file_and_writes_nothing(repo: Path) -> None:
    result = migrate("--check")

    assert result.exit_code == 1
    assert (repo / "openburrow.yaml").read_text(encoding="utf-8") == STALE
    assert not (repo / "openburrow.yaml.bak").exists()


def test_write_migrates_and_keeps_the_previous_file(repo: Path) -> None:
    result = migrate("--write")

    assert result.exit_code == 0
    assert "schema_version: 1" in (repo / "openburrow.yaml").read_text(encoding="utf-8")
    assert (repo / "openburrow.yaml.bak").read_text(encoding="utf-8") == STALE


def test_check_passes_once_the_file_is_current(repo: Path) -> None:
    migrate("--write")

    assert migrate("--check").exit_code == 0


def test_write_and_check_together_are_refused(repo: Path) -> None:
    """Opposites, so the command stops rather than picking one silently.

    ``--check`` is what a CI job runs and ``--write`` is what a developer runs;
    a script that somehow passed both has a bug, and exiting 2 says so instead of
    leaving the operator to work out which flag won.
    """
    result = migrate("--write", "--check")

    assert result.exit_code == 2
    assert (repo / "openburrow.yaml").read_text(encoding="utf-8") == STALE


def test_a_file_from_a_newer_build_is_refused(repo: Path) -> None:
    (repo / "openburrow.yaml").write_text("schema_version: 99\n", encoding="utf-8")

    result = migrate("--write")

    assert result.exit_code == 1
    assert (repo / "openburrow.yaml").read_text(encoding="utf-8") == "schema_version: 99\n"


def test_json_carries_the_plan_and_not_the_document_body(repo: Path) -> None:
    """``--json`` is for a caller deciding, so it gets the decision, not the file."""
    result = runner.invoke(app, ["--json", "config", "migrate", "--check"])

    payload = json.loads(result.output)
    assert payload["needed"] is True
    assert payload["declared"] is None
    assert payload["written"] is False
    assert payload["unknown_keys"] == ["govrnance"]
    assert "document" not in payload


def test_json_reports_a_refusal_without_a_traceback(repo: Path) -> None:
    (repo / "openburrow.yaml").write_text("schema_version: 99\n", encoding="utf-8")

    result = runner.invoke(app, ["--json", "config", "migrate", "--check"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["needed"] is None
    assert "99" in payload["error"]


def test_a_current_file_reports_already_current(repo: Path) -> None:
    (repo / "openburrow.yaml").write_text(CURRENT, encoding="utf-8")

    result = migrate()

    assert result.exit_code == 0
    assert "already current" in result.output


def test_it_works_when_the_repo_config_cannot_be_loaded(repo: Path) -> None:
    """The case the command exists for, and the one it could not handle.

    Resolving the target path through ``context.config()`` meant the lookup
    itself raised for a repo whose ``openburrow.yaml`` declares a version this
    build rejects — so the command named by that error's own hint was unreachable
    for the only input that produces the hint. Discovery now derives the path
    without parsing the file, which is the difference between a fix-it command
    and a command that fails the same way as everything else.
    """
    (repo / "openburrow.yaml").write_text("schema_version: 99\n", encoding="utf-8")

    result = migrate()  # deliberately no --path

    assert result.exit_code == 1
    assert "99" in result.output


def test_it_works_in_a_directory_with_no_repo_at_all(repo: Path) -> None:
    """No repo, so the honest answer is "there is no file to migrate"."""
    (repo / "openburrow.yaml").unlink()

    result = migrate()

    assert result.exit_code == 1
    assert "burrow init" in result.output
