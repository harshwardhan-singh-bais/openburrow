"""Exception hierarchy.

Every error a user can plausibly see carries three things: a stable machine
``code`` (so the CLI, the daemon, and the web UI can all branch on it without
string matching), a human ``hint`` (the thing you actually want to read at 2am),
and an optional ``context`` dict that lands in structured logs.

The base class is deliberately not a ``ValueError``/``RuntimeError`` subclass
with special behaviour — it is plain, so ``except OpenBurrowError`` catches
everything OpenBurrow itself raises and nothing from the standard library.
"""

from __future__ import annotations

from typing import Any


class OpenBurrowError(Exception):
    """Base class for every error OpenBurrow raises on purpose."""

    code: str = "openburrow.error"
    hint: str | None = None

    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        context: dict[str, Any] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if hint is not None:
            self.hint = hint
        self.context: dict[str, Any] = dict(context or {})
        if cause is not None:
            self.__cause__ = cause

    def to_dict(self) -> dict[str, Any]:
        """Wire/log representation. Stable keys — the UI renders these directly."""
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.hint:
            payload["hint"] = self.hint
        if self.context:
            payload["context"] = self.context
        return payload

    def __str__(self) -> str:
        base = self.message
        if self.hint:
            base = f"{base}\n  hint: {self.hint}"
        return base


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
class ConfigError(OpenBurrowError):
    """Configuration is missing, malformed, or internally contradictory."""

    code = "openburrow.config_error"
    hint = "Run `burrow doctor` to see which configuration source is failing."


class RepoNotInitializedError(ConfigError):
    """No ``openburrow.yaml`` found walking up from the current directory."""

    code = "openburrow.repo_not_initialized"
    hint = "Run `burrow init` at the repository root."


class ConfigSchemaError(ConfigError):
    """``openburrow.yaml`` targets a schema version this build does not understand."""

    code = "openburrow.config_schema_error"
    hint = "Run `burrow config migrate` to upgrade the file."


# --------------------------------------------------------------------------
# Protocol layer (A2A / ACP / MCP)
# --------------------------------------------------------------------------
class ProtocolError(OpenBurrowError):
    """A wire-level protocol contract was violated."""

    code = "openburrow.protocol_error"


class A2AProtocolError(ProtocolError):
    """Malformed Agent Card, illegal task transition, or bad JSON-RPC envelope."""

    code = "openburrow.a2a_protocol_error"


class IllegalTaskTransitionError(A2AProtocolError):
    """Something tried to move an A2A task between states the lifecycle forbids."""

    code = "openburrow.a2a_illegal_transition"
    hint = (
        "Task states are terminal once reached. Open a new task instead of "
        "reviving a completed/failed/canceled/rejected one."
    )


class ACPNegotiationError(ProtocolError):
    """A performative was used out of turn, or a negotiation failed to converge."""

    code = "openburrow.acp_negotiation_error"


class ConformanceError(ProtocolError):
    """OpenBurrow's bus failed an A2A conformance check against a peer."""

    code = "openburrow.a2a_conformance_error"


# --------------------------------------------------------------------------
# Bus / daemon
# --------------------------------------------------------------------------
class BusError(OpenBurrowError):
    """The local event bus could not deliver, persist, or replay an event."""

    code = "openburrow.bus_error"


class DaemonNotRunningError(BusError):
    """A CLI command needed the daemon and it was not up."""

    code = "openburrow.daemon_not_running"
    hint = "Start it with `burrow daemon start` (or run the command with --no-daemon)."


class DaemonAlreadyRunningError(BusError):
    """A second daemon tried to claim the same state directory."""

    code = "openburrow.daemon_already_running"
    hint = "Use `burrow daemon status` to find the existing PID, or `--force` to take over."


class BackpressureError(BusError):
    """A lane produced output faster than the bus could absorb it."""

    code = "openburrow.backpressure"


# --------------------------------------------------------------------------
# Adapters / harnesses
# --------------------------------------------------------------------------
class AdapterError(OpenBurrowError):
    """A harness adapter failed to start, drive, or interpret its harness."""

    code = "openburrow.adapter_error"


class HarnessNotFoundError(AdapterError):
    """The harness binary is not on PATH, or its version pin does not match."""

    code = "openburrow.harness_not_found"
    hint = "Install the harness, or set <HARNESS>_BIN to its absolute path."


class HarnessCrashedError(AdapterError):
    """The harness process died while a lane depended on it."""

    code = "openburrow.harness_crashed"


class OutputParseError(AdapterError):
    """Neither structured output nor the fallback parser could read the harness."""

    code = "openburrow.output_parse_error"
    hint = "Set OPENBURROW_ADAPTER_PARSE_FALLBACK=true, or file an adapter bug with the raw output."


class InjectionError(AdapterError):
    """An incoming A2A message could not be delivered into the harness's native input."""

    code = "openburrow.injection_error"


# --------------------------------------------------------------------------
# Governance / policy
# --------------------------------------------------------------------------
class GovernanceError(OpenBurrowError):
    """The delegation-accountability layer refused an action."""

    code = "openburrow.governance_error"


class AuthorityScopeError(GovernanceError):
    """A delegated task asked for authority the delegator never held."""

    code = "openburrow.authority_scope_violation"
    hint = (
        "Delegation inherits a bounded subset of the delegator's permissions by "
        "default. Widen it explicitly on the delegation, or approve as a human."
    )


class SilentAuthorityCreepError(GovernanceError):
    """A delegation chain expanded scope beyond what the originating human approved."""

    code = "openburrow.silent_authority_creep"


class RedelegationDeniedError(GovernanceError):
    """A non-transferable task was re-delegated."""

    code = "openburrow.redelegation_denied"


class ImpersonationError(GovernanceError):
    """Message provenance did not verify against the claimed sender."""

    code = "openburrow.impersonation_detected"


class PolicyViolation(OpenBurrowError):
    """The pre-execution policy gate blocked an action."""

    code = "openburrow.policy_violation"
    hint = "Inspect `.openburrow/policy.yaml`; use `burrow policy test` to dry-run a change."


class ApprovalRequired(OpenBurrowError):
    """Execution is paused pending a human decision (A2A ``auth_required``)."""

    code = "openburrow.approval_required"
    hint = "Respond with `burrow approvals respond <id> --approve|--deny`."


class ApprovalTimeout(OpenBurrowError):
    """Nobody answered an approval inside the configured window."""

    code = "openburrow.approval_timeout"


# --------------------------------------------------------------------------
# Durable sessions
# --------------------------------------------------------------------------
class CheckpointError(OpenBurrowError):
    """Snapshotting or restoring a session checkpoint failed."""

    code = "openburrow.checkpoint_error"


class SessionNotFoundError(OpenBurrowError):
    """No session matches the given id or name."""

    code = "openburrow.session_not_found"


class LaneNotFoundError(OpenBurrowError):
    """No lane matches the given id, name, or harness."""

    code = "openburrow.lane_not_found"


class ClaimConflictError(OpenBurrowError):
    """A resource is already claimed by another lane."""

    code = "openburrow.claim_conflict"
    hint = "Use `burrow offer <step> <teammate>` to negotiate a transfer."


__all__ = [
    "A2AProtocolError",
    "ACPNegotiationError",
    "AdapterError",
    "ApprovalRequired",
    "ApprovalTimeout",
    "AuthorityScopeError",
    "BackpressureError",
    "BusError",
    "CheckpointError",
    "ClaimConflictError",
    "ConfigError",
    "ConfigSchemaError",
    "ConformanceError",
    "DaemonAlreadyRunningError",
    "DaemonNotRunningError",
    "GovernanceError",
    "HarnessCrashedError",
    "HarnessNotFoundError",
    "IllegalTaskTransitionError",
    "ImpersonationError",
    "InjectionError",
    "LaneNotFoundError",
    "OpenBurrowError",
    "OutputParseError",
    "PolicyViolation",
    "ProtocolError",
    "RedelegationDeniedError",
    "RepoNotInitializedError",
    "SessionNotFoundError",
    "SilentAuthorityCreepError",
]
