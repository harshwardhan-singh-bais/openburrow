"""OpenBurrow core.

The substrate every other OpenBurrow package stands on:

* :mod:`openburrow.core.config`   — layered configuration (env > .env > yaml > defaults)
* :mod:`openburrow.core.models`   — the domain vocabulary (sessions, lanes, tasks, delegations)
* :mod:`openburrow.core.db`       — the local SQLite schema and the append-only bus log
* :mod:`openburrow.core.logging`  — structured logging that survives both console and JSON sinks

Nothing in this package imports another OpenBurrow package. That one-way rule is
what keeps the dependency graph acyclic and lets ``openburrow-core`` be installed
on its own by tools that only want the schemas.
"""

from openburrow.core.config import RepoConfig, ResolvedConfig, Settings, get_settings, load_config
from openburrow.core.errors import (
    AdapterError,
    BusError,
    ConfigError,
    GovernanceError,
    OpenBurrowError,
    PolicyViolation,
    ProtocolError,
)
from openburrow.core.logging import bind_context, configure_logging, get_logger
from openburrow.core.paths import BurrowPaths, find_repo_root
from openburrow.core.version import __version__, version_info

__all__ = [
    "AdapterError",
    "BurrowPaths",
    "BusError",
    "ConfigError",
    "GovernanceError",
    "OpenBurrowError",
    "PolicyViolation",
    "ProtocolError",
    "RepoConfig",
    "ResolvedConfig",
    "Settings",
    "__version__",
    "bind_context",
    "configure_logging",
    "find_repo_root",
    "get_logger",
    "get_settings",
    "load_config",
    "version_info",
]
