"""Machine-level settings, sourced from the environment.

Mirrors ``.env.example`` section by section. Every field is optional with a
sensible default, because OpenBurrow must run on a laptop with an empty ``.env``.

Access the singleton with :func:`get_settings`; tests override with
:func:`override_settings` or by clearing the cache.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

EnvStr = Annotated[str, Field()]

_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def _split_csv(value: object) -> list[str]:
    """Accept ``"a,b"``, ``"a, b"``, ``["a","b"]`` or ``""`` and normalise to a list."""
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value)]


#: A list setting written as a comma-separated string in the environment.
#:
#: ``NoDecode`` is load-bearing, not decoration. pydantic-settings treats any
#: non-scalar annotation as "complex" and JSON-decodes the raw value before
#: validation ever runs — so ``OPENBURROW_POLICY_ALLOWED_COMMANDS=git,npm``
#: raised ``SettingsError`` from ``prepare_field_value`` and the ``mode="before"``
#: CSV validator below never got a chance to run. Every one of the eleven list
#: settings in ``.env.example`` was affected; the validator that looks like it
#: handles them was unreachable.
CsvList = Annotated[list[str], NoDecode]


def native_alias(name: str) -> AliasChoices:
    """A validation alias accepting a provider-native name *and* our own.

    ``env_prefix`` is ``OPENBURROW_``, so every OpenBurrow setting reads
    ``OPENBURROW_<FIELD>``. A few settings are not ours to rename: an API key the
    ``anthropic`` SDK also reads, ``AIDER_BIN`` which Aider's own tooling
    documents, ``GITHUB_TOKEN`` which every GitHub client in existence looks for.
    Those have to answer to their upstream name as well, or the user ends up
    maintaining two copies of one credential.

    Declaring the alias is what makes each exception visible. The alternative —
    an empty ``env_prefix`` so that bare names resolve — reads *every* bare name,
    which is how a shell's ``ENV=production`` silently became ``settings.env``
    while the documented ``OPENBURROW_ENV`` did nothing at all.

    Setting a ``validation_alias`` disables ``env_prefix`` for that field, which
    is why the prefixed form is named explicitly rather than assumed.
    """
    return AliasChoices(name, f"OPENBURROW_{name}")


class Settings(BaseSettings):
    """Environment-backed runtime settings.

    Two namespaces, and the split is deliberate. OpenBurrow's own settings read
    ``OPENBURROW_<FIELD>`` via ``env_prefix``. The handful of provider-native
    variables — ``ANTHROPIC_API_KEY``, ``GITHUB_TOKEN``, ``AIDER_BIN`` and their
    kin — keep their upstream names through :func:`native_alias`, because the
    tools that read them are not ours and renaming a credential is how a harness
    ends up unable to authenticate while ``.env`` looks correct.

    ``env_prefix`` used to be empty "on purpose", to read both namespaces. It
    read neither: pydantic-settings with no prefix matches the bare field name,
    so ``OPENBURROW_ENV`` was ignored, ``OPENBURROW_POLICY_DEFAULT_ACTION`` was
    ignored, and the provider keys worked only because their bare names happened
    to be the right ones. All 205 documented ``OPENBURROW_*`` variables in
    ``.env.example`` were inert, and a bare ``ENV`` or ``LOG_LEVEL`` from an
    unrelated tool was read in their place.
    """

    model_config = SettingsConfigDict(
        env_prefix="OPENBURROW_",
        env_file=(".env", ".env.local"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        validate_assignment=False,
    )

    # ---- 1. core / runtime -------------------------------------------------
    env: Literal["development", "staging", "production"] = "development"
    log_level: str = "INFO"
    log_format: Literal["console", "json"] = "console"
    log_file: str = ""
    global_home: str = ""
    state_dir: str = ""
    default_repo: str = ""
    timezone: str = "UTC"
    strict_config: bool = False
    telemetry: bool = False

    # ---- 2. daemon ---------------------------------------------------------
    daemon_host: str = "127.0.0.1"
    daemon_port: int = 7317
    daemon_socket: str = ""
    daemon_pidfile: str = ""
    daemon_autostart: bool = False
    daemon_max_cpu_pct: int = 50
    daemon_max_mem_mb: int = 1024
    daemon_shutdown_grace_s: int = 15
    daemon_multi_repo: bool = True
    api_version: str = "v1"

    # ---- 3. database -------------------------------------------------------
    db_url: str = ""
    db_echo: bool = False
    db_pool_size: int = 5
    db_busy_timeout_ms: int = 5000
    db_journal_mode: str = "WAL"
    db_migrate_on_start: bool = True
    db_backup_on_migrate: bool = True
    db_retention_days: int = 90

    # ---- 4. A2A ------------------------------------------------------------
    a2a_enabled: bool = True
    a2a_host: str = "127.0.0.1"
    a2a_port_base: int = 7400
    a2a_port_range: int = 200
    a2a_agent_card_path: str = "/.well-known/agent-card.json"
    a2a_transport: Literal["sse", "streamable-http", "json-rpc-only"] = "sse"
    a2a_protocol_version: str = "1.0"
    a2a_task_timeout_s: int = 900
    a2a_input_required_timeout_s: int = 1800
    a2a_auth_required_timeout_s: int = 3600
    a2a_max_concurrent_tasks: int = 8
    a2a_message_max_bytes: int = 262_144
    a2a_rate_limit_per_min: int = 60
    a2a_dedupe_window_s: int = 30
    a2a_sign_messages: bool = False
    a2a_verify_provenance: bool = False
    a2a_external_peers: CsvList = Field(default_factory=list)
    a2a_conformance_suite: bool = True

    # ---- 5. MCP ------------------------------------------------------------
    mcp_enabled: bool = True
    mcp_config_paths: CsvList = Field(default_factory=list)
    mcp_allow_passthrough: bool = True
    mcp_deny_servers: CsvList = Field(default_factory=list)
    mcp_healthcheck_interval_s: int = 60
    mcp_tool_timeout_s: int = 120

    # ---- 6. ACP ------------------------------------------------------------
    acp_enabled: bool = True
    acp_max_exchanges: int = 6
    acp_escalate_after: int = 4
    acp_auto_accept_trivial: bool = False
    acp_default_timeout_s: int = 600

    # ---- 7. LLM ------------------------------------------------------------
    llm_enabled: bool = True
    llm_default_model: str = "gpt-4o-mini"
    llm_judge_model: str = "gpt-4o-mini"
    llm_classifier_model: str = "gpt-4o-mini"
    llm_planner_model: str = "gpt-4o"
    llm_temperature: float = 0.0
    llm_max_tokens: int = 2048
    llm_timeout_s: int = 60
    llm_max_retries: int = 3
    llm_cache: bool = True
    llm_budget_usd_per_session: float = 5.0
    llm_local_only: bool = False

    # Provider keys — read but never logged. `repr=False` keeps them out of
    # tracebacks and `burrow doctor` dumps. Each carries a native alias so the
    # SDK's own variable name works as well as ours.
    openai_api_key: str = Field(
        default="", repr=False, validation_alias=native_alias("OPENAI_API_KEY")
    )
    openai_api_base: str = Field(
        default="https://api.openai.com/v1", validation_alias=native_alias("OPENAI_API_BASE")
    )
    anthropic_api_key: str = Field(
        default="", repr=False, validation_alias=native_alias("ANTHROPIC_API_KEY")
    )
    anthropic_api_base: str = Field(
        default="https://api.anthropic.com", validation_alias=native_alias("ANTHROPIC_API_BASE")
    )
    gemini_api_key: str = Field(
        default="", repr=False, validation_alias=native_alias("GEMINI_API_KEY")
    )
    azure_api_key: str = Field(
        default="", repr=False, validation_alias=native_alias("AZURE_API_KEY")
    )
    azure_api_base: str = Field(default="", validation_alias=native_alias("AZURE_API_BASE"))
    azure_deployment_name: str = Field(
        default="", validation_alias=native_alias("AZURE_DEPLOYMENT_NAME")
    )
    ollama_api_base: str = Field(
        default="http://127.0.0.1:11434", validation_alias=native_alias("OLLAMA_API_BASE")
    )
    vllm_api_base: str = Field(
        default="http://127.0.0.1:8000/v1", validation_alias=native_alias("VLLM_API_BASE")
    )
    openrouter_api_key: str = Field(
        default="", repr=False, validation_alias=native_alias("OPENROUTER_API_KEY")
    )
    groq_api_key: str = Field(default="", repr=False, validation_alias=native_alias("GROQ_API_KEY"))
    deepseek_api_key: str = Field(
        default="", repr=False, validation_alias=native_alias("DEEPSEEK_API_KEY")
    )

    # ---- 8. adapters -------------------------------------------------------
    adapters_enabled: CsvList = Field(
        default_factory=lambda: ["opencode", "claude-code", "codex", "crush"]
    )
    adapter_default: str = "opencode"
    adapter_structured_output: Literal["prefer", "require", "never"] = "prefer"
    adapter_parse_fallback: bool = True
    adapter_healthcheck_on_start: bool = True
    adapter_version_pin: str = ""
    adapter_crash_restart: Literal["never", "once", "backoff", "always"] = "backoff"
    adapter_max_restarts: int = 3
    adapter_output_buffer_kb: int = 1024
    adapter_term: str = "xterm-256color"

    opencode_bin: str = Field(default="opencode", validation_alias=native_alias("OPENCODE_BIN"))
    opencode_server_url: str = Field(
        default="http://127.0.0.1:4096", validation_alias=native_alias("OPENCODE_SERVER_URL")
    )
    opencode_api_key: str = Field(
        default="", repr=False, validation_alias=native_alias("OPENCODE_API_KEY")
    )
    #: Passed to `opencode serve --config`. `.env.example` documented this and
    #: the adapter read it, but the field itself was never declared — so the
    #: adapter raised AttributeError on every spawn while the env var sat in
    #: `.env` being silently dropped by `extra="ignore"`. A documented setting
    #: that nothing can read is worse than an undocumented one.
    #:
    #: Declaring the field was only half the fix: with an empty ``env_prefix``
    #: the variable was still not read, so the AttributeError became a silently
    #: empty string instead. The native alias is what makes it reachable.
    opencode_config_dir: str = Field(
        default="", validation_alias=native_alias("OPENCODE_CONFIG_DIR")
    )
    claude_code_bin: str = Field(default="claude", validation_alias=native_alias("CLAUDE_CODE_BIN"))
    codex_bin: str = Field(default="codex", validation_alias=native_alias("CODEX_BIN"))
    codex_sandbox: str = Field(
        default="workspace-write", validation_alias=native_alias("CODEX_SANDBOX")
    )
    codex_approval_policy: str = Field(
        default="on-request", validation_alias=native_alias("CODEX_APPROVAL_POLICY")
    )
    crush_bin: str = Field(default="crush", validation_alias=native_alias("CRUSH_BIN"))
    gemini_cli_bin: str = Field(default="gemini", validation_alias=native_alias("GEMINI_CLI_BIN"))
    aider_bin: str = Field(default="aider", validation_alias=native_alias("AIDER_BIN"))
    goose_bin: str = Field(default="goose", validation_alias=native_alias("GOOSE_BIN"))
    custom_adapter_dir: str = ".openburrow/adapters"
    custom_adapter_trust: Literal["prompt", "allow", "deny"] = "prompt"

    # ---- 9. git ------------------------------------------------------------
    git_bin: str = "git"
    worktree_root: str = ""
    worktree_cleanup: Literal["on-close", "on-session-end", "never"] = "on-close"
    worktree_prune_stale_days: int = 7
    branch_prefix: str = "burrow/"
    commit_signing: bool = False
    agents_md: str = "AGENTS.md"
    agents_md_max_bytes: int = 32_768

    # ---- 10. governance ----------------------------------------------------
    governance_enabled: bool = True
    governance_require_delegation_authority: bool = True
    governance_authority_inheritance: Literal["bounded", "full", "none"] = "bounded"
    governance_max_delegation_depth: int = 3
    governance_allow_redelegation: bool = False
    governance_cross_boundary_strict: bool = True
    governance_verify_capability_cards: Literal["off", "warn", "enforce"] = "warn"
    governance_ledger_retention_days: int = 365
    governance_audit_export_format: Literal["jsonl", "csv", "pdf-ready-html"] = "jsonl"
    governance_human_id: str = ""
    governance_human_email: str = ""
    governance_org_id: str = ""

    # ---- 11. policy / sandbox ---------------------------------------------
    policy_file: str = ".openburrow/policy.yaml"
    policy_enforce: bool = True
    #: ``None`` means "the repository's ``openburrow.yaml`` decides", not "deny".
    #: A concrete default here would override the committed policy on every run,
    #: because the env layer is merged last — so a repo that set ``allow`` in YAML
    #: would be silently reset to whatever this line said.
    policy_default_action: Literal["deny", "allow"] | None = None
    policy_allowed_commands: CsvList = Field(default_factory=list)
    policy_denied_commands: CsvList = Field(default_factory=list)
    policy_allowed_paths: CsvList = Field(default_factory=list)
    policy_denied_paths: CsvList = Field(default_factory=list)
    policy_budget_tokens_per_session: int = 2_000_000
    policy_budget_usd_per_session: float = 5.0
    policy_max_files_changed_per_step: int = 200
    sandbox_enabled: bool = False
    sandbox_backend: Literal["process", "bubblewrap", "firejail", "docker", "seatbelt"] = "process"
    sandbox_cpu_limit: float = 2.0
    sandbox_mem_limit_mb: int = 2048
    sandbox_network: Literal["deny", "allowlist", "allow"] = "deny"
    sandbox_network_allowlist: CsvList = Field(default_factory=list)
    #: Image for the ``docker`` backend. Empty on purpose, and an empty value
    #: makes that backend unavailable rather than falling back to a guessed
    #: image: the harness has to run in *something* that has the harness in it,
    #: and picking a base image on the operator's behalf would run their code in
    #: a container they never chose.
    sandbox_docker_image: str = ""
    secrets_scrub_relay: bool = True
    secrets_scan_before_commit: bool = True

    # ---- 12. approvals -----------------------------------------------------
    approvals_enabled: bool = True
    approvals_mode: Literal["interactive", "auto-deny", "auto-approve-safe"] = "interactive"
    approvals_timeout_s: int = 900
    approvals_on_timeout: Literal["deny", "escalate", "pause"] = "deny"
    approvals_any_teammate: bool = True
    approvals_risk_tier_file: str = ".openburrow/risk-tiers.yaml"
    approvals_ci_auto_deny: bool = True

    # ---- 12b. durable recovery (Stage 12) ----------------------------------
    #: False is the safe default: a human is asked before a crashed session
    #: restarts itself. Auto-resume without asking is item 168's opt-in, not the
    #: default, because a session that resumes into a half-finished negotiation
    #: is doing work nobody just watched it decide to do.
    recovery_auto_resume: bool = False
    #: How often a running lane's state is snapshotted. 0 disables periodic
    #: checkpointing; checkpoints are still taken on every lane state change.
    recovery_checkpoint_interval_s: int = 300
    #: Failures a single lane may retry before it lands in the dead-letter state
    #: (item 163) and waits for a human instead of consuming more budget.
    recovery_dead_letter_after: int = 5
    #: Consecutive harness failures before the circuit breaker (item 164) stops
    #: invoking that harness at all, and how long before a half-open probe.
    recovery_circuit_threshold: int = 5
    recovery_circuit_cooldown_s: int = 300
    #: Flag a lane that reports success but produced no artifacts — item 162's
    #: silent-failure detection. Off only because a harness that never reports
    #: artifacts would flag every healthy run.
    recovery_silent_failure: bool = True
    #: Per-lane bus queue depth for the bulkhead (item 165): one flooded lane
    #: must not be able to stall the others. Overflow drops to the log, which
    #: the lane replays from by seq — the same recovery path a slow subscriber
    #: already uses.
    recovery_bulkhead_queue: int = 256
    recovery_crash_log_keep: int = 50

    # ---- 13. brain / lessons / crdt ---------------------------------------
    brain_enabled: bool = True
    brain_scope: Literal["repo", "org"] = "repo"
    brain_ingest_agents_md: bool = True
    brain_export_agents_md: bool = True
    brain_staleness_check: bool = True
    brain_embeddings: bool = False
    brain_embedding_model: str = "text-embedding-3-small"
    lessons_enabled: bool = True
    lessons_scope: Literal["session", "repo", "org"] = "session"
    lessons_max_per_session: int = 200
    lessons_ttl_days: int = 30
    crdt_enabled: bool = True
    crdt_backend: Literal["yjs", "automerge"] = "yjs"
    crdt_doc: str = ""

    # ---- 14. radar ---------------------------------------------------------
    radar_enabled: bool = True
    radar_confidence_threshold: float = 0.65
    radar_run_before_claim: bool = True
    radar_max_pairs_per_round: int = 25
    radar_cache_verdicts: bool = True

    # ---- 15. reels ---------------------------------------------------------
    reel_enabled: bool = True
    reel_record_casts: bool = True
    reel_cast_dir: str = ".openburrow/casts"
    reel_export_dir: str = ".openburrow/reels"
    reel_max_bytes_per_lane: int = 52_428_800
    reel_sign_exports: bool = False
    reel_link_expiry_hours: int = 168
    reel_public_base_url: str = "http://localhost:3000"

    # ---- 16. observability -------------------------------------------------
    otel_enabled: bool = False
    otel_exporter_otlp_endpoint: str = Field(
        default="http://127.0.0.1:4317",
        validation_alias=native_alias("OTEL_EXPORTER_OTLP_ENDPOINT"),
    )
    otel_service_name: str = Field(
        default="openburrow", validation_alias=native_alias("OTEL_SERVICE_NAME")
    )
    metrics_prometheus: bool = False
    metrics_port: int = 9464
    #: Loopback by default, and that default is a security decision rather than
    #: a convenience. ``prometheus_client.start_http_server`` binds ``0.0.0.0``
    #: when given no address, which publishes session ids, lane names, token
    #: spend and governance-flag counts to every host on the network — from a
    #: daemon whose entire premise is that it owns one developer's machine. A
    #: scrape target on another box is a deliberate act: set this to that
    #: interface and say so.
    metrics_host: str = "127.0.0.1"

    # ---- 17. notifications -------------------------------------------------
    notify_enabled: bool = True
    notify_desktop: bool = True
    notify_on: CsvList = Field(
        default_factory=lambda: [
            "approval",
            "negotiation-unresolved",
            "session-done",
            "governance-flag",
        ]
    )
    slack_webhook_url: str = Field(
        default="", repr=False, validation_alias=native_alias("SLACK_WEBHOOK_URL")
    )
    slack_channel: str = Field(
        default="#openburrow", validation_alias=native_alias("SLACK_CHANNEL")
    )
    discord_webhook_url: str = Field(
        default="", repr=False, validation_alias=native_alias("DISCORD_WEBHOOK_URL")
    )
    teams_webhook_url: str = Field(
        default="", repr=False, validation_alias=native_alias("TEAMS_WEBHOOK_URL")
    )
    hooks_file: str = ".openburrow/hooks.yaml"
    webhook_secret: str = Field(default="", repr=False)
    github_token: str = Field(default="", repr=False, validation_alias=native_alias("GITHUB_TOKEN"))
    github_repo: str = Field(default="", validation_alias=native_alias("GITHUB_REPO"))
    github_pr_on_complete: bool = False
    github_status_checks: bool = False
    #: Email digest (item 224). Stdlib SMTP only — no new dependency for a
    #: channel most teams will never enable; the fields are inert until
    #: `email_digest_enabled` is set. Alias honours the documented
    #: `OPENBURROW_EMAIL_DIGEST` env name.
    email_digest_enabled: bool = Field(default=False, validation_alias=native_alias("EMAIL_DIGEST"))
    email_digest_recipients: CsvList = Field(default_factory=list)
    #: Alias set is explicit: `OPENBURROW_EMAIL_FROM` keeps the every-field-
    #: answers-its-prefixed-name contract, `SMTP_FROM` keeps the documented
    #: `.env.example` name working.
    email_from: str = Field(
        default="openburrow@localhost",
        validation_alias=AliasChoices("OPENBURROW_EMAIL_FROM", "SMTP_FROM"),
    )
    smtp_host: str = "localhost"
    smtp_port: int = 587
    smtp_user: str = Field(default="", repr=False)
    smtp_password: str = Field(default="", repr=False)
    #: `starttls` is mandatory: an unencrypted digest of governance events is
    #: worse than a missed one. Set false only for a loopback debug relay.
    smtp_use_tls: bool = True

    # ---- 18. relay ---------------------------------------------------------
    relay_enabled: bool = False
    relay_url: str = "ws://127.0.0.1:8080/ws"
    relay_token: str = Field(default="", repr=False)
    relay_org: str = ""
    relay_workspace: str = "default"
    relay_role: Literal["admin", "member", "viewer"] = "member"
    relay_reconnect_max_s: int = 60
    relay_queue_max: int = 10_000
    relay_private_by_default: bool = True
    relay_share_brain: bool = False
    # server-side
    #: Loopback by default, not 0.0.0.0. A relay that listens on every interface
    #: the moment it starts is reachable by anything on the network, and the
    #: operator never chose that — they chose to start a relay. The container
    #: images and compose files pass --host 0.0.0.0 explicitly, because a
    #: container must accept traffic from outside its own namespace; that is the
    #: case where binding wide is intended, so it is stated rather than assumed.
    relay_host: str = "127.0.0.1"
    relay_port: int = 8080
    relay_db_url: str = "postgresql+asyncpg://openburrow:openburrow@localhost:5432/openburrow"
    relay_db_pool_size: int = 10
    relay_db_max_overflow: int = 20
    relay_cors_origins: CsvList = Field(default_factory=lambda: ["http://localhost:3000"])
    relay_tenant_quota_sessions: int = 50
    relay_tenant_quota_lanes: int = 200
    relay_fair_scheduling: bool = True
    relay_rate_limit_per_min: int = 600
    relay_jwt_secret: str = Field(default="", repr=False)
    relay_jwt_alg: str = "HS256"
    relay_jwt_ttl_hours: int = 12
    relay_allow_signup: bool = False
    relay_trust_proxy: bool = False

    # ---- 19. security ------------------------------------------------------
    auth_mode: Literal["local", "ssh-key", "github-device", "jwt"] = "local"
    ssh_key: str = "~/.ssh/id_ed25519"
    socket_mode: str = "0600"
    session_token_ttl_hours: int = 12
    allow_insecure_http: bool = False

    # ---- 20. chaos (test only) --------------------------------------------
    chaos_enabled: bool = False
    chaos_kill_lane_after_s: int = 0
    chaos_drop_relay_messages: float = 0.0
    chaos_malform_plan_output: bool = False
    chaos_hang_approval: bool = False
    chaos_seed: int = 1337

    # ---- 22. tests ---------------------------------------------------------
    e2e: bool = False
    test_harness: str = "mock"

    # ---- 23. distribution --------------------------------------------------
    update_check: bool = True
    update_channel: Literal["stable", "beta", "nightly"] = "stable"
    #: Where the release manifest is fetched from. `.env.example` documents
    #: ``OPENBURROW_UPDATE_MANIFEST_URL``, but the field was never declared, so
    #: the CLI had grown a ``settings.__dict__.get("update_manifest_url")``
    #: workaround that always returned None and always fell back to the
    #: hardcoded URL. The variable was documented, set, and inert.
    update_manifest_url: str = "https://openburrow.dev/releases/manifest.json"
    uvx_mode: bool = False

    # ------------------------------------------------------------------ validators
    @field_validator(
        "a2a_external_peers",
        "mcp_config_paths",
        "mcp_deny_servers",
        "adapters_enabled",
        "policy_allowed_commands",
        "policy_denied_commands",
        "policy_allowed_paths",
        "policy_denied_paths",
        "sandbox_network_allowlist",
        "notify_on",
        "relay_cors_origins",
        mode="before",
    )
    @classmethod
    def _parse_csv_lists(cls, value: object) -> list[str]:
        return _split_csv(value)

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalise_level(cls, value: object) -> str:
        level = str(value or "INFO").upper()
        if level not in _LOG_LEVELS:
            raise ValueError(f"log_level must be one of {_LOG_LEVELS}, got {value!r}")
        return level

    @field_validator("sandbox_network_allowlist", "mcp_deny_servers", mode="after")
    @classmethod
    def _lowercase_hosts(cls, value: list[str]) -> list[str]:
        return [item.lower() for item in value]

    @model_validator(mode="after")
    def _guard_production_invariants(self) -> Settings:
        """Fail loudly on combinations that are unsafe outside development.

        These are the settings where getting it wrong is a security incident
        rather than a bug, so they are hard errors instead of warnings.
        """
        if self.env == "production":
            if self.relay_enabled and not self.relay_jwt_secret:
                raise ValueError(
                    "OPENBURROW_RELAY_JWT_SECRET is required when the relay runs in production"
                )
            if self.allow_insecure_http:
                raise ValueError(
                    "OPENBURROW_ALLOW_INSECURE_HTTP must be false in production "
                    "(A2A messages and relay tokens travel over this transport)"
                )
            if self.sandbox_network == "allow" and self.sandbox_enabled:
                raise ValueError(
                    "OPENBURROW_SANDBOX_NETWORK=allow defeats sandboxing in production; "
                    "use 'allowlist' or 'deny'"
                )
        if self.chaos_enabled and self.env == "production":
            raise ValueError("OPENBURROW_CHAOS_ENABLED must never be true in production")
        return self

    # ------------------------------------------------------------------ helpers
    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def is_ci(self) -> bool:
        return os.environ.get("CI", "").lower() in {"1", "true", "yes"}

    @property
    def ssh_key_path(self) -> Path:
        return Path(self.ssh_key).expanduser()

    @property
    def approved_provider_keys(self) -> dict[str, bool]:
        """Which providers are configured — booleans only, never the values.

        This is what ``burrow doctor`` prints and what the web dashboard shows.
        """
        return {
            "openai": bool(self.openai_api_key),
            "anthropic": bool(self.anthropic_api_key),
            "gemini": bool(self.gemini_api_key),
            "azure": bool(self.azure_api_key),
            "openrouter": bool(self.openrouter_api_key),
            "groq": bool(self.groq_api_key),
            "deepseek": bool(self.deepseek_api_key),
            "ollama": bool(self.ollama_api_base),
        }

    def redacted_dump(self) -> dict[str, object]:
        """A JSON-safe dump with every secret replaced by a presence flag."""
        data = self.model_dump()
        for key in list(data):
            if any(token in key for token in ("key", "secret", "token", "password")):
                data[key] = "***set***" if data[key] else ""
        return data


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton.

    Cached so repeated access is free; call :func:`clear_settings_cache` in
    tests after mutating the environment.
    """
    return Settings()


def clear_settings_cache() -> None:
    get_settings.cache_clear()


__all__ = ["Settings", "clear_settings_cache", "get_settings"]
