"""Single source of truth for the OpenBurrow version.

Every package reports the same version because they ship as one product even
though they install as many distributions. ``openburrow-core`` owns the number;
the others re-export it.
"""

from __future__ import annotations

from dataclasses import dataclass

__version__ = "0.1.0"

#: Bumped whenever the on-disk SQLite schema changes in a way that needs a migration.
SCHEMA_VERSION = 1

#: Bumped whenever the daemon control-plane API changes shape.
CONTROL_PLANE_API_VERSION = "v1"

#: The A2A protocol revision OpenBurrow targets.
A2A_PROTOCOL_VERSION = "1.0"

#: The ``openburrow.yaml`` schema revision this build understands.
REPO_CONFIG_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class VersionInfo:
    """Parsed semantic version, plus the protocol/schema revisions it targets."""

    major: int
    minor: int
    patch: int
    prerelease: str | None = None
    schema_version: int = SCHEMA_VERSION
    a2a_protocol_version: str = A2A_PROTOCOL_VERSION
    control_plane_api_version: str = CONTROL_PLANE_API_VERSION
    repo_config_schema_version: int = REPO_CONFIG_SCHEMA_VERSION

    @property
    def is_prerelease(self) -> bool:
        return self.prerelease is not None

    def __str__(self) -> str:
        base = f"{self.major}.{self.minor}.{self.patch}"
        return f"{base}-{self.prerelease}" if self.prerelease else base


def _parse(raw: str) -> VersionInfo:
    core, _, prerelease = raw.partition("-")
    parts = core.split(".")
    if len(parts) != 3:  # pragma: no cover - guarded by the constant above
        raise ValueError(f"malformed version string: {raw!r}")
    major, minor, patch = (int(p) for p in parts)
    return VersionInfo(major=major, minor=minor, patch=patch, prerelease=prerelease or None)


version_info: VersionInfo = _parse(__version__)
