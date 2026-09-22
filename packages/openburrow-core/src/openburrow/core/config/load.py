"""Merge the two configuration layers into one resolved object.

Precedence, highest first::

    process environment  >  .env  >  openburrow.yaml  >  built-in defaults

That ordering is deliberate. The environment is machine-and-session specific and
is what a user tweaks at 2am; the YAML file is team policy that went through
review. A shell override must always win, or you cannot debug anything.

:class:`ResolvedConfig` exposes both halves plus the merged view, so a caller can
ask "what does the *team* think?" and "what is actually in force right now?"
separately — which is exactly the question ``burrow doctor`` answers.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any

from openburrow.core.config.repo_config import (
    AdapterConfig,
    BrainConfig,
    BudgetConfig,
    BusConfig,
    GovernanceConfig,
    PolicyConfig,
    RepoConfig,
    load_repo_config,
)
from openburrow.core.config.settings import Settings, get_settings
from openburrow.core.errors import ConfigError
from openburrow.core.paths import BurrowPaths, find_repo_root_or_none


@dataclass(frozen=True)
class ResolvedConfig:
    """The single object the rest of OpenBurrow receives.

    Everything downstream takes a :class:`ResolvedConfig` rather than reaching
    for ``os.environ`` or re-reading YAML. That keeps configuration observable:
    if you want to know what a run used, you print one of these.

    ``slots=True`` was here, and it was silently fatal. Seven of this class's
    properties are ``functools.cached_property``, which stores its result in
    ``instance.__dict__`` — and a slots dataclass has no ``__dict__``. Every one
    of them raised::

        TypeError: No '__dict__' attribute on 'ResolvedConfig' instance to
        cache 'adapters' property.

    That is ``adapters``, ``governance``, ``policy``, ``budget``, ``brain``,
    ``bus`` and ``db_url`` — the entire merged-config layer, which is what every
    other component reads. Nothing caught it because the callers that failed
    were inside ``except Exception`` handlers that reported success anyway;
    ``burrow init`` printed "OpenBurrow initialised" while its own first line
    said the database could not be created.

    ``frozen=True`` stays: ``cached_property`` writes to ``__dict__`` directly,
    bypassing the frozen ``__setattr__``, so caching works on a frozen dataclass
    as long as it has a ``__dict__``. Slots bought four fields of memory on an
    object constructed once per process. The trade was never worth it.
    """

    settings: Settings
    repo: RepoConfig
    paths: BurrowPaths
    #: Where each layer came from, for `burrow doctor` provenance output.
    sources: dict[str, str]

    # --- merged views ------------------------------------------------------
    @cached_property
    def adapters(self) -> AdapterConfig:
        """Repo adapter config, with env-provided enabled list winning if set.

        ``openburrow.yaml`` describes the team's intent; ``OPENBURROW_ADAPTERS_ENABLED``
        is what this machine actually has installed. The machine wins.
        """
        base = self.repo.adapters.model_copy(deep=True)
        env_enabled = self.settings.adapters_enabled
        if env_enabled and env_enabled != AdapterConfig().enabled:
            base.enabled = env_enabled
        if self.settings.adapter_default:
            base.default = self.settings.adapter_default
        base.structured_output = self.settings.adapter_structured_output
        base.parse_fallback = self.settings.adapter_parse_fallback
        base.crash_restart = self.settings.adapter_crash_restart
        base.max_restarts = self.settings.adapter_max_restarts
        return base

    @cached_property
    def governance(self) -> GovernanceConfig:
        """Repo governance policy, tightened (never loosened) by env.

        Note the asymmetry: env can make governance *stricter* but not weaker.
        A developer must not be able to silently disable the audit layer for a
        repo whose policy file demands it.
        """
        base = self.repo.governance.model_copy(deep=True)
        if not self.settings.governance_enabled:
            base.enabled = False
        if self.settings.governance_require_delegation_authority:
            base.require_delegation_authority = True
        base.max_delegation_depth = min(
            base.max_delegation_depth, self.settings.governance_max_delegation_depth
        )
        if not self.settings.governance_allow_redelegation:
            base.allow_redelegation = False
        if self.settings.governance_cross_boundary_strict:
            base.cross_boundary_strict = True
        # "enforce" in env escalates; "off" in env only warns, never disables.
        if self.settings.governance_verify_capability_cards == "enforce":
            base.verify_capability_cards = "enforce"
        return base

    @cached_property
    def policy(self) -> PolicyConfig:
        base = self.repo.policy.model_copy(deep=True)
        base.enforce = self.settings.policy_enforce
        # ``policy_default_action`` and ``policy_allowed_paths`` were declared in
        # Settings and read by nothing, so OPENBURROW_POLICY_DEFAULT_ACTION had no
        # effect on the gate while looking exactly like the knob that sets it.
        # Every other policy field was merged here; these two were the gap.
        #
        # Only when set: the env layer is merged last, so an unconditional
        # assignment would override the committed policy on every run.
        if self.settings.policy_default_action is not None:
            base.default_action = self.settings.policy_default_action
        if self.settings.policy_allowed_commands:
            base.allowed_commands = list(self.settings.policy_allowed_commands)
        if self.settings.policy_denied_commands:
            base.denied_commands = list(self.settings.policy_denied_commands)
        if self.settings.policy_allowed_paths:
            base.allowed_paths = list(self.settings.policy_allowed_paths)
        if self.settings.policy_denied_paths:
            base.denied_paths = sorted({*base.denied_paths, *self.settings.policy_denied_paths})
        return base

    @cached_property
    def budget(self) -> BudgetConfig:
        base = self.repo.budget.model_copy(deep=True)
        if self.settings.policy_budget_usd_per_session:
            base.usd_per_session = min(
                base.usd_per_session or float("inf"),
                self.settings.policy_budget_usd_per_session,
            )
        if self.settings.policy_budget_tokens_per_session:
            base.tokens_per_session = min(
                base.tokens_per_session or 2**63,
                self.settings.policy_budget_tokens_per_session,
            )
        # The per-step file ceiling was the one budget field with no env path at
        # all, so it could only be set by editing the committed YAML.
        base.max_files_changed_per_step = min(
            base.max_files_changed_per_step or 2**63,
            self.settings.policy_max_files_changed_per_step,
        )
        return base

    @cached_property
    def brain(self) -> BrainConfig:
        base = self.repo.brain.model_copy(deep=True)
        base.enabled = base.enabled and self.settings.brain_enabled
        base.embeddings = self.settings.brain_embeddings
        base.embedding_model = self.settings.brain_embedding_model
        base.scope = self.settings.brain_scope
        return base

    @cached_property
    def bus(self) -> BusConfig:
        base = self.repo.bus.model_copy(deep=True)
        base.enabled = base.enabled and self.settings.a2a_enabled
        base.transport = self.settings.a2a_transport
        base.protocol_version = self.settings.a2a_protocol_version
        base.rate_limit_per_min = self.settings.a2a_rate_limit_per_min
        base.max_exchanges_before_escalation = self.settings.acp_max_exchanges
        if self.settings.a2a_external_peers:
            base.external_peers = sorted({*base.external_peers, *self.settings.a2a_external_peers})
        return base

    @cached_property
    def db_url(self) -> str:
        """Explicit env URL wins; otherwise the repo's SQLite file."""
        return self.settings.db_url or self.paths.db_url

    # --- diagnostics -------------------------------------------------------
    def provenance(self) -> dict[str, str]:
        """Human-readable "where did each value come from" map."""
        return dict(self.sources)

    def as_public_dict(self) -> dict[str, Any]:
        """Secrets-free view used by `burrow doctor --json` and the web dashboard."""
        return {
            "env": self.settings.env,
            "repo_root": str(self.paths.repo_root),
            "runtime_dir": str(self.paths.runtime_dir),
            "db_url": self.db_url,
            "sources": self.sources,
            "adapters": {
                "enabled": self.adapters.enabled,
                "default": self.adapters.default,
                "structured_output": self.adapters.structured_output,
            },
            "bus": {
                "enabled": self.bus.enabled,
                "transport": self.bus.transport,
                "protocol_version": self.bus.protocol_version,
                "external_peers": self.bus.external_peers,
            },
            "governance": self.governance.model_dump(mode="json"),
            "policy": {
                "enforce": self.policy.enforce,
                "default_action": self.policy.default_action,
            },
            "budget": self.budget.model_dump(mode="json"),
            "brain": {"enabled": self.brain.enabled, "scope": self.brain.scope},
            "providers_configured": self.settings.approved_provider_keys,
            "lanes_declared": [lane.name for lane in self.repo.lanes],
        }

    def require_lanes(self) -> list[Any]:
        """Lanes declared in YAML. Raises a helpful error when there are none."""
        if not self.repo.lanes:
            raise ConfigError(
                "no lanes declared in openburrow.yaml",
                hint=(
                    "Add a `lanes:` list, or run `burrow session start --harness <name>` "
                    "to create one ad hoc."
                ),
                context={"path": str(self.paths.config_file)},
            )
        return list(self.repo.lanes)


def load_config(
    repo_root: Path | str | None = None,
    *,
    settings: Settings | None = None,
    require_repo: bool = True,
) -> ResolvedConfig:
    """Build the merged configuration.

    ``require_repo=False`` is used by commands that must work outside a repo —
    ``burrow --version``, ``burrow daemon status``, ``burrow relay serve``.
    """
    resolved_settings = settings or get_settings()

    root: Path | None
    if repo_root is not None:
        root = Path(repo_root).expanduser().resolve()
    else:
        root = find_repo_root_or_none()

    if root is None:
        if require_repo:
            from openburrow.core.errors import RepoNotInitializedError

            raise RepoNotInitializedError(
                "OpenBurrow could not find a repository from the current directory",
                hint="cd into your repo and run `burrow init`.",
                context={"cwd": str(Path.cwd())},
            )
        root = Path.cwd()

    paths = BurrowPaths.for_repo(root)
    repo = load_repo_config(root, strict=resolved_settings.strict_config)

    sources: dict[str, str] = {
        "environment": "process env"
        if not resolved_settings.model_config.get("env_file")
        else ".env",
        "repo_config": (
            str(paths.config_file) if paths.config_file.is_file() else "<defaults, file absent>"
        ),
        "runtime_dir": str(paths.runtime_dir),
        "global_dir": str(paths.global_dir),
    }

    return ResolvedConfig(settings=resolved_settings, repo=repo, paths=paths, sources=sources)


__all__ = ["ResolvedConfig", "load_config"]
