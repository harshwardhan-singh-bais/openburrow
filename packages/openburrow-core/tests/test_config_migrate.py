"""``burrow config migrate`` — the command two other modules already promised.

:class:`~openburrow.core.errors.ConfigSchemaError`'s hint tells the user to run
it, and ``dump_repo_config``'s docstring names it as one of its two callers. It
did not exist. That is the same defect shape as the 23 daemon methods that had
engines and no handlers: a promise made in a string nobody could execute.

These tests pin the three rules that make the command worth having, and each one
is a rule rather than an implementation detail:

* a file **ahead** of this build is refused and left alone;
* a file **behind** it is upgraded, with the previous one kept;
* a missing migration step is a hard error, so bumping the supported version
  without writing the migration fails loudly instead of shipping a half-upgraded
  file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from openburrow.core.config import migrate, repo_config
from openburrow.core.config.migrate import apply_migration, plan_migration
from openburrow.core.errors import ConfigError, ConfigSchemaError

pytestmark = pytest.mark.integration

PRE_VERSIONING = """# A config written before schema_version existed.
project:
  name: acme-api
govrnance:
  require_delegation_authority: true
"""


def write(tmp_path: Path, text: str, name: str = "openburrow.yaml") -> Path:
    target = tmp_path / name
    target.write_text(text, encoding="utf-8")
    return target


def _bump_supported(monkeypatch: pytest.MonkeyPatch, version: int) -> None:
    """Move the supported schema version to ``version`` in *both* modules that read it.

    Two patches rather than one, and that is a real property of the code worth
    knowing: :data:`~openburrow.core.config.migrate.REPO_CONFIG_SCHEMA_VERSION`
    decides which steps run, and
    :data:`~openburrow.core.config.repo_config.REPO_CONFIG_SCHEMA_VERSION` decides
    what :class:`RepoConfig` accepts. In production both names are bound from
    ``openburrow.core.version`` at import, so they cannot disagree. In a test they
    can, and moving only the migrator's copy produces a plan that the validator
    then rejects — a failure that reads like a bug in the command rather than in
    the test.
    """
    monkeypatch.setattr(migrate, "REPO_CONFIG_SCHEMA_VERSION", version)
    monkeypatch.setattr(repo_config, "REPO_CONFIG_SCHEMA_VERSION", version)


# ---------------------------------------------------------------------------
# the plan
# ---------------------------------------------------------------------------
def test_a_file_without_a_version_needs_migrating(tmp_path: Path) -> None:
    plan = plan_migration(write(tmp_path, PRE_VERSIONING))

    assert plan.declared is None
    assert plan.needed is True
    assert plan.supported == 1


def test_a_current_file_does_not(tmp_path: Path) -> None:
    plan = plan_migration(write(tmp_path, "schema_version: 1\nproject:\n  name: acme\n"))

    assert plan.declared == 1
    assert plan.needed is False


def test_the_plan_reports_the_declared_version(tmp_path: Path) -> None:
    plan = plan_migration(write(tmp_path, "schema_version: 1\n"))

    assert plan.as_dict()["declared"] == 1
    assert plan.as_dict()["needed"] is False


def test_the_plan_does_not_touch_the_file(tmp_path: Path) -> None:
    """``--check`` and ``--write`` must not be able to disagree.

    Both read the same pure plan and branch once at the end. A plan with a side
    effect would make ``--check`` mutate the thing it was asked to inspect.
    """
    target = write(tmp_path, PRE_VERSIONING)

    plan_migration(target)

    assert target.read_text(encoding="utf-8") == PRE_VERSIONING


def test_the_plan_carries_the_document_body_but_not_into_json(tmp_path: Path) -> None:
    """``as_dict`` drops the body: a caller asking *whether* need not parse it."""
    plan = plan_migration(write(tmp_path, PRE_VERSIONING))

    assert "schema_version: 1" in plan.document
    assert "document" not in plan.as_dict()


def test_unknown_keys_are_reported_and_kept(tmp_path: Path) -> None:
    plan = plan_migration(write(tmp_path, PRE_VERSIONING))

    assert plan.unknown_keys == ("govrnance",)
    assert "govrnance" in plan.document


# ---------------------------------------------------------------------------
# refusal
# ---------------------------------------------------------------------------
def test_a_file_ahead_of_this_build_is_refused(tmp_path: Path) -> None:
    target = write(tmp_path, "schema_version: 99\nproject:\n  name: future\n")

    with pytest.raises(ConfigSchemaError) as caught:
        plan_migration(target)

    assert "99" in caught.value.message


def test_a_refused_file_is_left_exactly_as_it_was(tmp_path: Path) -> None:
    """The refusal has to happen before anything is written, not after.

    Downgrading the number would discard whatever the newer schema meant while
    leaving a file that still parses — the failure nobody can see afterwards.
    """
    original = "schema_version: 99\nproject:\n  name: future\n"
    target = write(tmp_path, original)

    with pytest.raises(ConfigSchemaError):
        plan_migration(target)

    assert target.read_text(encoding="utf-8") == original
    assert not (tmp_path / "openburrow.yaml.bak").exists()


def test_a_missing_file_is_a_config_error_not_a_traceback(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as caught:
        plan_migration(tmp_path / "absent.yaml")

    assert "burrow init" in (caught.value.hint or "")


def test_invalid_yaml_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as caught:
        plan_migration(write(tmp_path, "project: [unclosed\n"))

    assert "not valid YAML" in caught.value.message


def test_a_non_mapping_top_level_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as caught:
        plan_migration(write(tmp_path, "- just\n- a\n- list\n"))

    assert "mapping" in caught.value.message


def test_a_non_integer_version_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as caught:
        plan_migration(write(tmp_path, "schema_version: 'one'\n"))

    assert "not an integer" in caught.value.message


def test_a_boolean_is_not_accepted_as_a_version(tmp_path: Path) -> None:
    """``bool`` is an ``int`` subclass, so this needs saying out loud.

    Without the explicit check, ``schema_version: true`` coerces to 1 and the
    file is silently treated as a current v1 config.
    """
    with pytest.raises(ConfigError):
        plan_migration(write(tmp_path, "schema_version: true\n"))


# ---------------------------------------------------------------------------
# applying
# ---------------------------------------------------------------------------
def test_applying_writes_the_version_and_keeps_the_previous_file(tmp_path: Path) -> None:
    target = write(tmp_path, PRE_VERSIONING)
    plan = plan_migration(target)

    apply_migration(plan)

    assert "schema_version: 1" in target.read_text(encoding="utf-8")
    backup = tmp_path / "openburrow.yaml.bak"
    assert backup.read_text(encoding="utf-8") == PRE_VERSIONING


def test_the_backup_keeps_the_comments_the_rewrite_cannot(tmp_path: Path) -> None:
    """The write is a re-serialisation and YAML comments do not survive it.

    ``yaml.safe_load`` discards them and the dependency set has no round-tripping
    loader, so the backup is unconditional rather than opt-in: losing a comment
    is recoverable from a ``.bak``, and losing one nobody noticed is not.
    """
    target = write(tmp_path, PRE_VERSIONING)

    apply_migration(plan_migration(target))

    assert "# A config written before schema_version existed." not in target.read_text(
        encoding="utf-8"
    )
    assert "# A config written before schema_version existed." in (
        tmp_path / "openburrow.yaml.bak"
    ).read_text(encoding="utf-8")


def test_applying_a_current_file_changes_nothing(tmp_path: Path) -> None:
    text = "schema_version: 1\nproject:\n  name: acme\n"
    target = write(tmp_path, text)

    apply_migration(plan_migration(target))

    assert target.read_text(encoding="utf-8") == text
    assert not (tmp_path / "openburrow.yaml.bak").exists()


def test_the_migrated_file_parses_under_this_build(tmp_path: Path) -> None:
    """The end the command exists for: the file loads afterwards."""
    from openburrow.core.config.repo_config import load_repo_config

    target = write(tmp_path, PRE_VERSIONING)
    apply_migration(plan_migration(target))

    config = load_repo_config(tmp_path)
    assert config.schema_version == 1
    assert config.project.name == "acme-api"
    assert [item.path for item in config.unknown_keys()] == ["govrnance"]


# ---------------------------------------------------------------------------
# the version bump that has no migration
# ---------------------------------------------------------------------------
def test_a_missing_migration_step_is_a_hard_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bumped ``REPO_CONFIG_SCHEMA_VERSION`` with no migration must not pass.

    This is the failure mode the registry exists to prevent: the version number
    moves, every v1 file on disk is now "behind", and a command that quietly
    reported "already current" would leave them un-upgraded while telling the
    operator otherwise.
    """
    _bump_supported(monkeypatch, 2)
    target = write(tmp_path, "schema_version: 1\nproject:\n  name: acme\n")

    with pytest.raises(ConfigError) as caught:
        plan_migration(target)

    assert "no migration registered" in caught.value.message
    assert caught.value.context["from_version"] == 1


def test_a_registered_migration_is_actually_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control for the test above: the loop really does call the function.

    Without this, ``steps`` could be computed and then ignored and both tests
    would still pass.
    """

    def v1_to_v2(data: dict[str, Any]) -> dict[str, Any]:
        data.setdefault("project", {})["description"] = "upgraded by the step"
        return data

    _bump_supported(monkeypatch, 2)
    monkeypatch.setitem(migrate.MIGRATIONS, 1, v1_to_v2)
    target = write(tmp_path, "schema_version: 1\nproject:\n  name: acme\n")

    plan = plan_migration(target)

    assert plan.steps == (1,)
    assert plan.needed is True
    assert "upgraded by the step" in plan.document
    assert plan.as_dict()["supported"] == 2


def test_the_registry_is_empty_and_that_is_the_whole_truth() -> None:
    """v1 is the first version, so there is nothing to upgrade *from* yet.

    Pinned so the empty registry reads as a fact about the schema rather than as
    a stub someone forgot to fill in.
    """
    assert migrate.MIGRATIONS == {}
