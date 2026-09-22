"""``openburrow.yaml`` schema migration.

:class:`~openburrow.core.errors.ConfigSchemaError` tells the user to run
``burrow config migrate``, and :func:`~openburrow.core.config.repo_config.dump_repo_config`
names this command as one of its two callers. This module is what those promises
have to cash out to.

It is deliberately small. A migration framework written before there is a second
schema version is a framework designed around a guess, so the only rules here are
the ones that are true today:

* A file declaring a version **newer** than this build is refused, never
  rewritten. The newer version may have changed what an existing key *means*;
  writing the number down to what this build understands would discard exactly
  that information while leaving a file that still parses — the worst of both,
  because nothing downstream can tell it happened.
* A file declaring an **older** version is upgraded by running each step in
  :data:`MIGRATIONS` in order. A missing step is a hard error rather than a quiet
  success, so bumping the supported version without writing the migration fails
  the first time anyone runs the command instead of shipping a half-upgraded
  file.
* A file declaring **no** version predates the key. It is treated as version 1 —
  the first version — and the key is written in, which is the whole of the
  upgrade today.

Unknown keys survive the round trip. That is not incidental: a config written for
a newer build may hold a key this one cannot interpret, and a migration that
deleted it would make the file unusable on the build that wrote it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from openburrow.core.config.repo_config import (
    dump_repo_config,
    parse_repo_config,
)
from openburrow.core.errors import ConfigError, ConfigSchemaError
from openburrow.core.version import REPO_CONFIG_SCHEMA_VERSION

#: Migrations, keyed by the version they upgrade **from**. ``MIGRATIONS[1]``
#: turns a v1 document into a v2 document.
#:
#: Empty because v1 is the first version, and that is the honest state rather
#: than a gap: there is nothing to upgrade *from* yet. The registry exists so
#: that adding v2 is one function and one entry, and so that
#: :func:`plan_migration` can refuse when a step is missing instead of
#: silently claiming the file is current.
MIGRATIONS: dict[int, Callable[[dict[str, Any]], dict[str, Any]]] = {}


@dataclass(frozen=True, slots=True)
class MigrationPlan:
    """What migrating one file would do.

    Pure data. Nothing here has touched the disk, which is what lets ``--check``
    and ``--write`` be the same code path with one branch at the end rather than
    two implementations that can disagree.
    """

    path: Path
    declared: int | None
    supported: int
    steps: tuple[int, ...] = ()
    unknown_keys: tuple[str, ...] = ()
    document: str = field(default="", repr=False)

    @property
    def needed(self) -> bool:
        """Whether the file on disk differs from what this build would write.

        A missing ``schema_version`` counts even when no step runs, because
        recording it is the one change that is always meaningful: it is what lets
        the *next* build tell a v1 file from a pre-versioning one.
        """
        return self.declared is None or bool(self.steps)

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe view, without the document body.

        ``document`` is left out on purpose — it is the whole file, and a
        ``--json`` consumer asking whether migration is needed should not have to
        parse it to find that out.
        """
        return {
            "path": str(self.path),
            "declared": self.declared,
            "supported": self.supported,
            "steps": list(self.steps),
            "needed": self.needed,
            "unknown_keys": list(self.unknown_keys),
        }


def _read_mapping(target: Path) -> dict[str, Any]:
    """The file as a raw mapping, before any schema is applied to it."""
    try:
        parsed = yaml.safe_load(target.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(
            f"{target.name} is not valid YAML: {exc}",
            context={"path": str(target)},
            cause=exc,
        ) from exc

    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise ConfigError(
            f"{target.name} must contain a mapping at the top level, got {type(parsed).__name__}",
            context={"path": str(target)},
        )
    return parsed


def _declared_version(raw: dict[str, Any], target: Path) -> int | None:
    """The version the file declares, or ``None`` when it predates the key.

    A non-integer is rejected here rather than left to pydantic. ``bool`` is
    excluded explicitly because it is an ``int`` subclass in Python, so
    ``schema_version: true`` would otherwise sail through as version 1.
    """
    if "schema_version" not in raw:
        return None
    value = raw["schema_version"]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(
            f"{target.name} declares schema_version {value!r}, which is not an integer",
            hint=f"Set `schema_version: {REPO_CONFIG_SCHEMA_VERSION}`.",
            context={"path": str(target)},
        )
    return value


def plan_migration(path: Path | str) -> MigrationPlan:
    """Work out what migrating ``path`` would do, without touching it.

    Raises :class:`ConfigSchemaError` when the file is ahead of this build. That
    is the one case the command refuses rather than reports, and it refuses
    before reading anything else, because there is no safe partial answer.
    """
    target = Path(path)
    if not target.is_file():
        raise ConfigError(
            f"no configuration file at {target}",
            hint="Run `burrow init` to create openburrow.yaml.",
            context={"path": str(target)},
        )

    raw = _read_mapping(target)
    declared = _declared_version(raw, target)

    if declared is not None and declared > REPO_CONFIG_SCHEMA_VERSION:
        raise ConfigSchemaError(
            f"{target.name} declares schema_version {declared}, but this build "
            f"understands at most {REPO_CONFIG_SCHEMA_VERSION}",
            hint=(
                "Upgrade OpenBurrow rather than downgrading the file. A newer schema "
                "may have changed what an existing key means, and this build cannot "
                "tell which keys those are."
            ),
            context={
                "path": str(target),
                "found": declared,
                "supported": REPO_CONFIG_SCHEMA_VERSION,
            },
        )

    # A file with no version is at the first version, not at zero: it was written
    # before the key existed, and nothing between then and v1 needs undoing.
    start = REPO_CONFIG_SCHEMA_VERSION if declared is None else declared
    steps = tuple(range(start, REPO_CONFIG_SCHEMA_VERSION))

    data = dict(raw)
    for version in steps:
        migrate = MIGRATIONS.get(version)
        if migrate is None:
            raise ConfigError(
                f"no migration registered from schema_version {version}",
                hint=(
                    f"This build supports v{REPO_CONFIG_SCHEMA_VERSION}, so the step "
                    f"from v{version} is required. Add it to MIGRATIONS in "
                    "openburrow/core/config/migrate.py."
                ),
                context={"path": str(target), "from_version": version},
            )
        data = migrate(data)

    data["schema_version"] = REPO_CONFIG_SCHEMA_VERSION

    # Permissive on purpose: a migration's job is to carry unknown keys forward,
    # so refusing them here would make the command unable to do the one thing it
    # is for. They are reported instead, in the plan.
    config = parse_repo_config(data, source=str(target))

    return MigrationPlan(
        path=target,
        declared=declared,
        supported=REPO_CONFIG_SCHEMA_VERSION,
        steps=steps,
        unknown_keys=tuple(item.path for item in config.unknown_keys()),
        document=dump_repo_config(config),
    )


def apply_migration(plan: MigrationPlan) -> Path:
    """Write ``plan`` to disk, keeping the previous file beside it as ``.bak``.

    The backup is unconditional rather than opt-in. The write is a full
    re-serialisation, so YAML comments do not survive it — ``yaml.safe_load``
    discards them and the dependency set has no round-tripping loader. A comment
    nobody noticed is not recoverable; a ``.bak`` file is.
    """
    if not plan.needed:
        return plan.path

    backup = plan.path.with_suffix(plan.path.suffix + ".bak")
    backup.write_bytes(plan.path.read_bytes())
    plan.path.write_bytes(plan.document.encode("utf-8"))
    return plan.path


__all__ = [
    "MIGRATIONS",
    "MigrationPlan",
    "apply_migration",
    "plan_migration",
]
