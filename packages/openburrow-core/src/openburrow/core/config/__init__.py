"""Configuration.

Two layers, deliberately kept separate because they have different owners:

* :mod:`~openburrow.core.config.settings` — **machine** configuration. Secrets,
  paths, ports, provider keys. Comes from the environment (``.env``). Never
  committed, never relayed.
* :mod:`~openburrow.core.config.repo_config` — **repo** configuration. Lives in
  ``openburrow.yaml`` at the repo root and *is* committed, because it describes
  how the team wants this repository coordinated: which lanes, which policies,
  which risk tiers.

:func:`~openburrow.core.config.load.load_config` merges them with the documented
precedence (process env > ``.env`` > ``openburrow.yaml`` > defaults) and hands
back a single :class:`~openburrow.core.config.load.ResolvedConfig`.
"""

from openburrow.core.config.load import ResolvedConfig, load_config
from openburrow.core.config.repo_config import (
    AdapterConfig,
    BrainConfig,
    BudgetConfig,
    GovernanceConfig,
    LaneTemplate,
    PolicyConfig,
    RepoConfig,
    load_repo_config,
)
from openburrow.core.config.settings import Settings, get_settings

__all__ = [
    "AdapterConfig",
    "BrainConfig",
    "BudgetConfig",
    "GovernanceConfig",
    "LaneTemplate",
    "PolicyConfig",
    "RepoConfig",
    "ResolvedConfig",
    "Settings",
    "get_settings",
    "load_config",
    "load_repo_config",
]
