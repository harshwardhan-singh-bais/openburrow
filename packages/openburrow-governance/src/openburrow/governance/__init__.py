"""OpenBurrow's governance and delegation-accountability layer.

This is the package that answers a *named, documented* gap rather than a
speculative one. A2A, MCP, and ACP specify how agents talk; none of them
specifies who is accountable when one agent acts on another's behalf, what
authority actually transferred, or how a cross-boundary action gets audited.
The protocol authors have said so in print.

Two halves, deliberately separated:

**Enforcement** — :class:`~openburrow.governance.ledger.DelegationLedger`
  Refuses delegations that over-reach, refuses re-delegation of
  non-transferable tasks, refuses actions outside a delegation's scope. A
  refusal here means the action does not run.

**Enforcement** — :class:`~openburrow.governance.policy.PolicyGate`
  Refuses a command before it runs, and grades the ones it allows into risk
  tiers. Also a refusal that means the action does not run, and it is here rather
  than in the CLI for the same reason the ledger is here rather than in the
  command that reads it: a rule evaluator only one caller can reach is a report,
  not a gate.

**Detection** — :mod:`openburrow.governance.detectors`
  Notices authority creep, capability mismatches, impersonation, poisoned
  delegations and lessons, and adversarial intent. A hit here produces a flag a
  human reviews.

The asymmetry is intentional. Detection is allowed to be noisy because a false
positive costs a dismissal; enforcement must not be, because a false positive
costs a stalled session.

What this layer does **not** claim: it narrows the accountability gap, it does
not close it. It cannot make a malicious *human* teammate's requests safe, and
it does not solve open problems in agent alignment. Item 193 requires that
limitation be stated in the README, not buried.
"""

from openburrow.governance.detectors import (
    Detection,
    detect_adversarial_intent,
    detect_authority_creep,
    detect_capability_mismatch,
    detect_cross_boundary_message,
    detect_impersonation,
    detect_injection,
    detect_poisoned_delegation,
    detect_poisoned_lesson,
    scan_for_secrets,
)
from openburrow.governance.ledger import (
    AuthorizationResult,
    DelegationLedger,
    default_scope_for_role,
)
from openburrow.governance.policy import (
    APPROVAL_TIERS,
    RISK_ORDER,
    PolicyGate,
    PolicyVerdict,
    RolePolicy,
    matches_command,
    matches_path,
    resolve_role,
)

__all__ = [
    "APPROVAL_TIERS",
    "RISK_ORDER",
    "AuthorizationResult",
    "DelegationLedger",
    "Detection",
    "PolicyGate",
    "PolicyVerdict",
    "RolePolicy",
    "default_scope_for_role",
    "detect_adversarial_intent",
    "detect_authority_creep",
    "detect_capability_mismatch",
    "detect_cross_boundary_message",
    "detect_impersonation",
    "detect_injection",
    "detect_poisoned_delegation",
    "detect_poisoned_lesson",
    "matches_command",
    "matches_path",
    "resolve_role",
    "scan_for_secrets",
]
