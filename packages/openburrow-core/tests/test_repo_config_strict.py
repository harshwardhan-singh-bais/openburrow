"""``OPENBURROW_STRICT_CONFIG`` decides what an unknown key means — and nothing else does.

The flag was documented in three places and implemented in none. ``.env.example``
said "unknown YAML keys are a hard error" only when it was ``true``; the module
docstring promised the permissive branch was "ignored (otherwise) with a
warning, so a newer config on an older CLI degrades rather than explodes". What
actually happened was unconditional: ``_Base`` set ``extra="forbid"``, so an
unknown key raised at every setting, and ``strict`` reached the module and was
used for exactly one thing — a ``setdefault`` on ``schema_version`` that was
itself a no-op, because the field already had a default.

Both halves are pinned here. The permissive half matters most, because it is the
one a user meets: a team that upgrades one person's CLI must not be forced to
downgrade the shared config, and the key a newer build wrote must still be there
when that person runs it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from openburrow.core.config.repo_config import (
    dump_repo_config,
    load_repo_config,
    parse_repo_config,
)
from openburrow.core.errors import ConfigError, ConfigSchemaError

pytestmark = pytest.mark.unit

TYPO = "govrnance"


def _with_typo() -> dict[str, object]:
    return {
        "schema_version": 1,
        "project": {"name": "acme"},
        TYPO: {"require_delegation_authority": True},
    }


def test_an_unknown_key_is_preserved_when_not_strict() -> None:
    config = parse_repo_config(_with_typo(), source="openburrow.yaml")

    assert [item.path for item in config.unknown_keys()] == [TYPO]


def test_an_unknown_key_survives_a_round_trip() -> None:
    """Preserved, not dropped — the whole point of the permissive branch.

    A key this build cannot interpret may still be load-bearing for the build
    that wrote it. Dropping it would make the file unusable there, and would do
    it silently: the file still parses here, so nothing would notice until
    someone on the other version ran the project.
    """
    document = dump_repo_config(parse_repo_config(_with_typo()))

    assert TYPO in document
    reparsed = parse_repo_config(yaml.safe_load(document))
    assert [item.path for item in reparsed.unknown_keys()] == [TYPO]


def test_an_unknown_key_is_a_hard_error_when_strict() -> None:
    with pytest.raises(ConfigError) as caught:
        parse_repo_config(_with_typo(), source="openburrow.yaml", strict=True)

    assert TYPO in caught.value.message
    assert caught.value.context["unknown_keys"] == [TYPO]


def test_the_error_suggests_the_field_that_was_probably_meant() -> None:
    """The mitigation for allowing unknown keys at all.

    ``extra="forbid"`` caught typos for free. Relaxing it to a warning trades
    that away, so the report has to carry the thing the hard error used to
    provide: which field was likely intended.
    """
    config = parse_repo_config(_with_typo())

    assert config.unknown_keys()[0].suggestion == "governance"


def test_a_key_with_no_close_match_gets_no_suggestion() -> None:
    config = parse_repo_config({"schema_version": 1, "zzzzzzzzzz": 1})

    assert config.unknown_keys()[0].suggestion is None


def test_a_nested_unknown_key_is_found_and_located() -> None:
    """Recursive, because ``extra="allow"`` applies at every level."""
    config = parse_repo_config({"schema_version": 1, "policy": {"denied_command": ["typo"]}})

    found = config.unknown_keys()
    assert [item.path for item in found] == ["policy.denied_command"]
    assert found[0].suggestion == "policy.denied_commands"


def test_an_unknown_key_inside_a_list_item_is_found_and_indexed() -> None:
    """A lane template is a list element, and its keys are as unknown as any other."""
    config = parse_repo_config(
        {
            "schema_version": 1,
            "lanes": [{"name": "alice", "harness": "mock", "claimz": ["src/**"]}],
        }
    )

    found = config.unknown_keys()
    assert [item.path for item in found] == ["lanes[0].claimz"]
    assert found[0].suggestion == "lanes[0].claims"


def test_a_clean_config_reports_nothing() -> None:
    config = parse_repo_config({"schema_version": 1, "project": {"name": "acme"}})

    assert config.unknown_keys() == []


def test_the_default_extra_keys_are_not_reported_as_unknown() -> None:
    """The guard against the detector firing on its own defaults.

    Every section has defaults, and a config that mentions only one of them must
    not look like it contains keys this build does not model — otherwise the
    warning is noise on every ordinary file and gets tuned out.
    """
    config = parse_repo_config({"schema_version": 1})

    assert config.unknown_keys() == []


def test_a_strict_load_requires_the_version_to_be_written_down(tmp_path: Path) -> None:
    """The flag's second meaning, which was equally inert.

    ``parsed.setdefault("schema_version", ...)`` looked like it made ``strict``
    matter here, but the field has a default of its own, so the assignment
    changed nothing either way. Strict now means what it reads as: say which
    schema you target rather than inheriting this build's by omission.
    """
    (tmp_path / "openburrow.yaml").write_text("project:\n  name: acme\n", encoding="utf-8")

    with pytest.raises(ConfigSchemaError) as caught:
        load_repo_config(tmp_path, strict=True)

    assert "schema_version" in caught.value.message


def test_a_strict_load_accepts_a_file_that_declares_its_version(tmp_path: Path) -> None:
    (tmp_path / "openburrow.yaml").write_text(
        "schema_version: 1\nproject:\n  name: acme\n", encoding="utf-8"
    )

    assert load_repo_config(tmp_path, strict=True).project.name == "acme"


def test_a_permissive_load_fills_the_version_in(tmp_path: Path) -> None:
    (tmp_path / "openburrow.yaml").write_text("project:\n  name: acme\n", encoding="utf-8")

    assert load_repo_config(tmp_path).schema_version == 1


def test_a_version_from_the_future_is_refused_at_any_strictness() -> None:
    """Not a strictness question, and deliberately not covered by the flag.

    A newer schema may have changed what an existing key *means*, so parsing the
    file under the old rules produces a configuration that is confidently wrong —
    a governance setting that quietly stopped applying. Refusing is the only
    answer that does not guess.
    """
    for strict in (False, True):
        with pytest.raises(ConfigSchemaError):
            parse_repo_config({"schema_version": 99}, strict=strict)


def test_a_version_below_one_is_refused() -> None:
    with pytest.raises(ConfigSchemaError):
        parse_repo_config({"schema_version": 0})
