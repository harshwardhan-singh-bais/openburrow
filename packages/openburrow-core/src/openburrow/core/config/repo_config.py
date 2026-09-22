"""``openburrow.yaml`` — the committed, per-repo configuration.

This file is checked into the repository because it encodes *team intent*:
which harnesses participate, what the risk tiers are, which paths are off
limits. It never contains secrets — those live in the environment (see
:mod:`openburrow.core.config.settings`).

Shape of the file::

    schema_version: 1
    project:
      name: acme-api
      default_branch: main

    lanes:
      - name: alice
        harness: claude-code
        role: implementer
        claims: ["src/api/**"]

    governance:
      require_delegation_authority: true
      max_delegation_depth: 2

    policy:
      # Denial is expressed through `denied_*`. Risk tiers do not block; they
      # route the dangerous commands to a human for approval. `default_action`
      # applies only when nothing else matched, so it defaults to `allow` — an
      # empty allowlist is not a prohibition.
      default_action: allow
      denied_commands: ["docker push"]
      denied_paths: [".env", "secrets/"]

    budget:
      usd_per_session: 5.00
      tokens_per_session: 2000000

Every section is optional; an empty file is a valid, if uninteresting, config.

``schema_version`` is the compatibility contract, and it is asymmetric on purpose.
A file that declares a version *newer* than this build is refused rather than
parsed: the newer version may have changed what an existing key means, and
guessing is how a governance setting quietly becomes advisory. A file that
declares an *older* version is upgraded by ``burrow config migrate``.

Unknown keys are the other half of that contract. With
``OPENBURROW_STRICT_CONFIG=false`` (the default) they are preserved and warned
about, so a config written for a newer build degrades on an older CLI instead of
exploding; with ``true`` they are a hard error naming each key. **Preserved, not
dropped** — a key this build cannot interpret may still be load-bearing for the
build that wrote it, and a round trip through this version must not delete it.
That is also why :meth:`RepoConfig.unknown_keys` carries a suggestion: relaxing
this to a warning trades away the typo protection `extra="forbid"` gave for
free, so the report has to name the likely intended field instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import get_close_matches
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from openburrow.core.errors import ConfigError, ConfigSchemaError
from openburrow.core.logging import get_logger
from openburrow.core.paths import find_config_file
from openburrow.core.version import REPO_CONFIG_SCHEMA_VERSION

log = get_logger(__name__)

HarnessName = str
LaneRole = Literal["implementer", "reviewer", "observer", "coordinator", "custom"]
RiskTier = Literal["low", "medium", "high", "critical"]


@dataclass(frozen=True, slots=True)
class UnknownKey:
    """One key in ``openburrow.yaml`` that this build does not model."""

    path: str
    #: The closest known field at the same level, when one is close enough to be
    #: worth naming. This is the mitigation for allowing unknown keys at all: a
    #: typo such as ``govrnance:`` used to be a hard error and is now merely
    #: preserved, so the report has to be good enough to catch it.
    suggestion: str | None = None


class _Base(BaseModel):
    """Shared model config: alias-friendly, immutable after load.

    ``extra="allow"`` rather than ``"forbid"``, because the model has to *see*
    the keys it does not know about before anything can decide what to do with
    them. Forbidding here made that decision unconditionally, which is why
    ``OPENBURROW_STRICT_CONFIG=false`` — documented as "unknown keys are
    preserved" — could not be honoured: an unknown key was a hard
    ``ConfigError`` at every setting, and the flag reached this module and
    stopped. Allowing them means :func:`parse_repo_config` is the single place
    that decides, and it can reject by name, report, or round-trip them.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
        str_strip_whitespace=True,
        validate_assignment=True,
    )


# ---------------------------------------------------------------------------
# project
# ---------------------------------------------------------------------------
class ProjectConfig(_Base):
    """Identity of the repository OpenBurrow is coordinating."""

    name: str = ""
    default_branch: str = "main"
    description: str = ""
    #: Tags flow into session metadata and make `burrow session list` searchable.
    tags: list[str] = Field(default_factory=list)
    #: Repo-local override for the branch prefix worktree branches use.
    branch_prefix: str = "burrow/"
    #: Paths excluded from overlap/conflict detection (vendored code, lockfiles).
    ignore_paths: list[str] = Field(
        default_factory=lambda: [
            "**/node_modules/**",
            "**/.venv/**",
            "**/dist/**",
            "**/build/**",
            "**/*.lock",
            "**/uv.lock",
            "**/package-lock.json",
        ]
    )


# ---------------------------------------------------------------------------
# lanes
# ---------------------------------------------------------------------------
class LaneTemplate(_Base):
    """A named lane a session can instantiate.

    A lane is one teammate's harness instance, exposed on the bus as exactly one
    A2A peer. Templates let ``burrow session start`` come up with the right
    lanes already declared instead of asking every time.
    """

    name: str
    harness: HarnessName
    role: LaneRole = "implementer"
    #: Glob patterns this lane is expected to touch. Used for advisory locks and
    #: for the Merge Radar's first-pass heuristic before the LLM judge runs.
    claims: list[str] = Field(default_factory=list)
    #: Environment variable names to pass through to this lane's harness.
    #: Values are read from the ambient environment at spawn time — never stored here.
    env_passthrough: list[str] = Field(default_factory=list)
    #: Optional model override, interpreted by the harness adapter.
    model: str = ""
    #: Hard ceiling on how long this lane may run in one session.
    max_runtime_s: int = 0  # 0 = unlimited
    #: Wall-clock idle time before the lane is considered abandoned.
    idle_timeout_s: int = 900
    #: Whether this lane may delegate work to other lanes.
    can_delegate: bool = True
    #: Whether this lane's tasks may be re-delegated onward (governance item 183).
    transferable: bool = True
    #: Extra CLI arguments appended verbatim to the harness spawn command.
    extra_args: list[str] = Field(default_factory=list)
    #: Free-form labels for filtering (`burrow session start --lane-tag reviewer`).
    tags: list[str] = Field(default_factory=list)
    #: For `harness: custom` lanes — the command to run, as an argv list
    #: (preferred) or a shell string. The custom adapter's error hint has told
    #: users to set this since it was written; the field actually existing is
    #: what makes that hint true. Ignored by every other harness, which has its
    #: own binary.
    command: list[str] | str = ""
    #: For `custom` lanes: put the command on a PTY (interactive TUI harnesses)
    #: or a plain pipe (build scripts, one-shot filters). Default False — most
    #: custom commands are not terminal programs.
    use_pty: bool = False
    #: Extra environment variables for this lane, merged over the base env.
    #: Non-secret values only: secrets belong in the ambient environment and
    #: reach the lane through `env_passthrough`, never through this file.
    env: dict[str, str] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# governance
# ---------------------------------------------------------------------------
class GovernanceConfig(_Base):
    """Repo-level overrides for the delegation-accountability layer.

    Defaults here are deliberately the strict ones. A repo that wants looser
    behaviour has to say so explicitly, in a file that gets code review.
    """

    enabled: bool = True
    require_delegation_authority: bool = True
    authority_inheritance: Literal["bounded", "full", "none"] = "bounded"
    max_delegation_depth: int = 3
    allow_redelegation: bool = False
    cross_boundary_strict: bool = True
    verify_capability_cards: Literal["off", "warn", "enforce"] = "warn"
    #: Lanes considered "trusted" — intra-boundary for audit purposes.
    trusted_lanes: list[str] = Field(default_factory=list)
    #: Human identities authorized to approve high-risk actions.
    approvers: list[str] = Field(default_factory=list)
    #: Extra actions that always require human sign-off in this repo.
    always_require_approval: list[str] = Field(default_factory=list)
    ledger_retention_days: int = 365

    @field_validator("max_delegation_depth")
    @classmethod
    def _depth_is_sane(cls, value: int) -> int:
        if value < 0:
            raise ValueError("max_delegation_depth cannot be negative")
        if value > 10:
            raise ValueError(
                "max_delegation_depth above 10 is not meaningful and almost certainly a typo"
            )
        return value


# ---------------------------------------------------------------------------
# policy
# ---------------------------------------------------------------------------
def _default_risk_tiers() -> dict[RiskTier, list[str]]:
    """Default action-pattern -> risk tier map.

    A named function rather than a lambda, because mypy infers a lambda's dict
    keys as plain ``str`` while the field is keyed by the ``RiskTier`` literal.
    Every key below is a valid member, so this is a widening the annotation
    fixes rather than a bug — but it is worth fixing properly: the annotation is
    what turns an invalid tier name into a type error instead of a policy rule
    that silently never matches.

    Defined above its use, not below it. The field default is evaluated eagerly
    when the class body runs, so a definition placed after the class is a
    ``NameError`` at import time rather than a forward reference — and
    ``from __future__ import annotations`` does not save it, because the
    annotation is not what is being evaluated.

    ``rm -rf`` sits in ``high`` as well as ``rm -rf /`` sitting in ``critical``.
    That is deliberate, and it is the clearest illustration of what "highest
    matching tier wins" is for: ``rm -rf /tmp/build`` matches only the high
    pattern and pauses for approval, while ``rm -rf /`` matches both and is
    critical. With the tier map as the only ordering there is no way to say
    "this family of commands is dangerous, and this member of it is fatal", and
    the previous implementation could not have honoured it anyway.
    """
    return {
        "critical": ["git push --force", "rm -rf /", "DROP TABLE", "kubectl delete"],
        "high": ["git push", "rm -rf", "npm publish", "docker push", "terraform apply"],
        "medium": ["git commit", "pip install", "npm install", "uv add"],
        "low": ["git status", "ls", "cat", "rg", "pytest"],
    }


class PolicyConfig(_Base):
    """The pre-execution gate (Stage 13) and the risk tiers approvals read.

    ``default_action`` used to default to ``deny`` while ``allowed_commands``
    defaulted to empty, and the gate consulted the former only when the latter
    was non-empty. So the shipped configuration read as "deny everything" and
    behaved as "deny nothing". One of the two had to move, and the half of this
    model that already worked — ``allowed_paths: ["."]`` with a ``denied_paths``
    blocklist — is allow-by-default. The commands half now matches it: denial is
    expressed by naming what is denied, and the risk tiers route the dangerous
    commands to a human rather than blocking them. ``default_action: deny`` is
    still supported and now actually applies; ``PolicyGate.diagnose`` reports it
    when it is set beside an empty allowlist, because that combination denies
    every command and is almost never what was meant.
    """

    file: str = ".openburrow/policy.yaml"
    enforce: bool = True
    default_action: Literal["deny", "allow"] = "allow"
    allowed_commands: list[str] = Field(default_factory=list)
    denied_commands: list[str] = Field(default_factory=list)
    allowed_paths: list[str] = Field(default_factory=lambda: ["."])
    denied_paths: list[str] = Field(
        default_factory=lambda: [".env", "secrets/", "**/*.pem", "**/*.key", "~/.ssh", "~/.aws"]
    )
    #: Per-role narrowing, so a reviewer lane is stricter than an implementer.
    #: Keys: ``allowed_commands`` (replaces), ``denied_commands`` (adds),
    #: ``default_action`` (may tighten to ``deny``, never loosen).
    role_overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)
    #: Risk tier per action pattern. Highest matching tier wins.
    risk_tiers: dict[RiskTier, list[str]] = Field(default_factory=_default_risk_tiers)


class BudgetConfig(_Base):
    """Ceilings enforced by the policy gate. Zero means "no ceiling"."""

    usd_per_session: float = 5.0
    tokens_per_session: int = 2_000_000
    max_files_changed_per_step: int = 200
    max_concurrent_lanes: int = 8
    max_session_duration_s: int = 0


# ---------------------------------------------------------------------------
# adapters
# ---------------------------------------------------------------------------
class AdapterConfig(_Base):
    """Which harnesses participate and how strictly they are driven."""

    enabled: list[str] = Field(
        default_factory=lambda: ["opencode", "claude-code", "codex", "crush"]
    )
    default: str = "opencode"
    structured_output: Literal["prefer", "require", "never"] = "prefer"
    parse_fallback: bool = True
    healthcheck_on_start: bool = True
    crash_restart: Literal["never", "once", "backoff", "always"] = "backoff"
    max_restarts: int = 3
    #: Per-harness overrides keyed by adapter name.
    overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# brain
# ---------------------------------------------------------------------------
class BrainConfig(_Base):
    """Shared-knowledge settings, including the AGENTS.md bridge."""

    enabled: bool = True
    scope: Literal["repo", "org"] = "repo"
    ingest_agents_md: bool = True
    export_agents_md: bool = True
    staleness_check: bool = True
    embeddings: bool = False
    embedding_model: str = "text-embedding-3-small"
    #: Paths whose contents seed Brain entries on `burrow init`.
    seed_paths: list[str] = Field(default_factory=lambda: ["AGENTS.md", "docs/**/*.md"])


# ---------------------------------------------------------------------------
# bus / A2A
# ---------------------------------------------------------------------------
class BusConfig(_Base):
    """How this repo's lanes talk to each other."""

    enabled: bool = True
    transport: Literal["sse", "streamable-http", "json-rpc-only"] = "sse"
    protocol_version: str = "1.0"
    rate_limit_per_min: int = 60
    max_exchanges_before_escalation: int = 6
    #: Third-party A2A peers this repo is willing to talk to (item 55).
    external_peers: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# relay
# ---------------------------------------------------------------------------
class RelayConfig(_Base):
    """Remote teammate settings, overridable per repo."""

    enabled: bool = False
    url: str = ""
    workspace: str = "default"
    share_brain: bool = False
    private_by_default: bool = True


# ---------------------------------------------------------------------------
# root
# ---------------------------------------------------------------------------
class RepoConfig(_Base):
    """The whole of ``openburrow.yaml``."""

    schema_version: int = REPO_CONFIG_SCHEMA_VERSION
    project: ProjectConfig = Field(default_factory=ProjectConfig)
    lanes: list[LaneTemplate] = Field(default_factory=list)
    adapters: AdapterConfig = Field(default_factory=AdapterConfig)
    governance: GovernanceConfig = Field(default_factory=GovernanceConfig)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    brain: BrainConfig = Field(default_factory=BrainConfig)
    bus: BusConfig = Field(default_factory=BusConfig)
    relay: RelayConfig = Field(default_factory=RelayConfig)

    @field_validator("schema_version")
    @classmethod
    def _schema_supported(cls, value: int) -> int:
        if value > REPO_CONFIG_SCHEMA_VERSION:
            raise ConfigSchemaError(
                f"openburrow.yaml declares schema_version {value}, but this build "
                f"understands at most {REPO_CONFIG_SCHEMA_VERSION}",
                context={"found": value, "supported": REPO_CONFIG_SCHEMA_VERSION},
            )
        if value < 1:
            raise ConfigSchemaError(f"schema_version must be >= 1, got {value}")
        return value

    @field_validator("lanes")
    @classmethod
    def _lane_names_unique(cls, value: list[LaneTemplate]) -> list[LaneTemplate]:
        seen: set[str] = set()
        for lane in value:
            if lane.name in seen:
                raise ValueError(f"duplicate lane name in openburrow.yaml: {lane.name!r}")
            seen.add(lane.name)
        return value

    # --- convenience -------------------------------------------------------
    def lane(self, name: str) -> LaneTemplate | None:
        return next((lane for lane in self.lanes if lane.name == name), None)

    def enabled_harnesses(self) -> list[str]:
        declared = {lane.harness for lane in self.lanes}
        return [h for h in self.adapters.enabled if h in declared] or list(self.adapters.enabled)

    def unknown_keys(self) -> list[UnknownKey]:
        """Every key this build does not model, with a suggestion where one fits.

        Recursive, because ``extra="allow"`` applies at every level: a key inside
        ``policy:`` is as much a compatibility question as one at the top level,
        and reporting only the root would send someone looking in the wrong
        place. Sorted by path so the message is stable across runs and two
        reports can be diffed.

        Read off the *validated* model rather than the raw mapping, so the paths
        are the ones pydantic actually resolved — aliases and nested list items
        included, not a second parser's opinion of the same file.
        """
        found: list[UnknownKey] = []

        def walk(model: BaseModel, prefix: str) -> None:
            known = list(type(model).model_fields)
            for key in model.model_extra or {}:
                near = get_close_matches(key, known, n=1, cutoff=0.7)
                found.append(
                    UnknownKey(
                        path=f"{prefix}{key}",
                        suggestion=f"{prefix}{near[0]}" if near else None,
                    )
                )
            for name in known:
                value = getattr(model, name, None)
                if isinstance(value, BaseModel):
                    walk(value, f"{prefix}{name}.")
                elif isinstance(value, list):
                    for index, item in enumerate(value):
                        if isinstance(item, BaseModel):
                            walk(item, f"{prefix}{name}[{index}].")

        walk(self, "")
        return sorted(found, key=lambda item: item.path)


def parse_repo_config(
    data: dict[str, Any], *, source: str = "<dict>", strict: bool = False
) -> RepoConfig:
    """Validate a raw mapping into a :class:`RepoConfig`.

    Raises :class:`ConfigError` with the offending field path, because "invalid
    configuration" without a line number is not an error message, it is a puzzle.

    ``strict`` decides what an unknown key means, and it is the only thing that
    does — see the module docstring. The permissive branch *keeps* the key rather
    than dropping it, because the two callers want opposite things from the same
    file: someone running this build wants to get on with their work, and
    ``burrow config migrate`` wants to write the key back out unchanged.
    """
    try:
        config = RepoConfig.model_validate(data or {})
    except ConfigSchemaError:
        raise
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
        raise ConfigError(
            f"invalid OpenBurrow configuration in {source}: {problems}",
            hint="Run `burrow config validate` for the full report.",
            context={"source": source},
            cause=exc,
        ) from exc

    unknown = config.unknown_keys()
    if unknown and strict:
        listed = "; ".join(
            f"{item.path} (did you mean {item.suggestion!r}?)" if item.suggestion else item.path
            for item in unknown
        )
        raise ConfigError(
            f"{source} contains keys this build does not model: {listed}",
            hint=(
                "OPENBURROW_STRICT_CONFIG=true rejects unknown keys. Remove them, or "
                "set it to false to have them preserved with a warning."
            ),
            context={"source": source, "unknown_keys": [item.path for item in unknown]},
        )
    if unknown:
        log.warning(
            "config.unknown_keys",
            source=source,
            keys=[item.path for item in unknown],
            suggestions={
                item.path: item.suggestion for item in unknown if item.suggestion is not None
            },
            detail=(
                "preserved as written; this build does not interpret them. "
                "OPENBURROW_STRICT_CONFIG=true turns this into an error."
            ),
        )

    return config


def load_repo_config(
    repo_root: Path | str | None = None,
    *,
    path: Path | str | None = None,
    strict: bool = False,
) -> RepoConfig:
    """Load and validate ``openburrow.yaml``.

    Returns a default :class:`RepoConfig` when the file is absent, so callers
    never have to branch on existence — ``burrow init`` is what creates it.
    """
    # Declared explicitly rather than left to inference. Without the annotation
    # mypy binds `config_path` to `Path` from the first branch and then rejects
    # the `None` assignments below — the variable is genuinely optional here,
    # and saying so is the difference between a checked flow and a lucky one.
    config_path: Path | None

    if path is not None:
        config_path = Path(path).expanduser()
    elif repo_root is not None:
        config_path = find_config_file(Path(repo_root).expanduser().resolve())
    else:
        config_path = None

    if config_path is None:
        from openburrow.core.paths import find_repo_root_or_none

        root = find_repo_root_or_none()
        config_path = find_config_file(root) if root else None

    if config_path is None or not config_path.is_file():
        return RepoConfig()

    raw = config_path.read_text(encoding="utf-8")
    try:
        parsed = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(
            f"{config_path.name} is not valid YAML: {exc}",
            context={"path": str(config_path)},
            cause=exc,
        ) from exc

    if not isinstance(parsed, dict):
        raise ConfigError(
            f"{config_path.name} must contain a mapping at the top level, "
            f"got {type(parsed).__name__}",
            context={"path": str(config_path)},
        )

    # `strict` has two meanings and this is the first: a strict file must say
    # which schema it targets rather than inheriting this build's version by
    # omission. Without the explicit check the `setdefault` below is decorative,
    # because `RepoConfig.schema_version` already has a default — which is
    # precisely what it was. The flag was read from the environment, threaded
    # through two layers, and then did nothing on this path.
    if strict and "schema_version" not in parsed:
        raise ConfigSchemaError(
            f"{config_path.name} does not declare schema_version, and "
            f"OPENBURROW_STRICT_CONFIG=true requires it",
            hint=f"Add `schema_version: {REPO_CONFIG_SCHEMA_VERSION}` to the file.",
            context={"path": str(config_path)},
        )
    parsed.setdefault("schema_version", REPO_CONFIG_SCHEMA_VERSION)

    return parse_repo_config(parsed, source=str(config_path), strict=strict)


def dump_repo_config(config: RepoConfig) -> str:
    """Serialise a config back to YAML with a stable, diff-friendly key order.

    Used by ``burrow init`` and ``burrow config migrate``; stable ordering keeps
    the generated file reviewable in a pull request.
    """
    payload = config.model_dump(mode="json", exclude_defaults=False)
    return yaml.safe_dump(
        payload,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
        width=100,
    )


def write_repo_config(config: RepoConfig, path: Path | str) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(dump_repo_config(config), encoding="utf-8")
    return target


__all__ = [
    "AdapterConfig",
    "BrainConfig",
    "BudgetConfig",
    "BusConfig",
    "GovernanceConfig",
    "LaneTemplate",
    "PolicyConfig",
    "ProjectConfig",
    "RelayConfig",
    "RepoConfig",
    "UnknownKey",
    "dump_repo_config",
    "load_repo_config",
    "parse_repo_config",
    "write_repo_config",
]
