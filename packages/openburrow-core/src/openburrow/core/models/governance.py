"""The governance layer's data model — the answer to a named, documented gap.

A2A, MCP and ACP all specify *how* agents talk. None of them specifies *who is
accountable* when Agent A delegates to Agent B, what authority actually moved,
or how an action taken across a trust boundary gets audited. That gap is
explicitly acknowledged by the protocols' own authors.

Three models close it:

* :class:`Delegation` — records that authority moved, from whom, to whom, and
  under what bounded scope. The key field is ``authorized_by``: the *human*,
  not the agent. An agent cannot bootstrap its own authority.
* :class:`AuthorityScope` — the bounded capability set a delegation carries.
  Inherited by default as a strict subset, never the full set.
* :class:`AuditRecord` — the immutable, cross-boundary-strict log. Every
  delegation, every cross-boundary message, every policy decision lands here.

:class:`GovernanceFlag` is the detection side: silent authority creep,
capability-card mismatches, impersonation attempts. A flag is not an error — it
is a thing a human should look at, which is a different and more useful category.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from pydantic import Field, computed_field, field_validator

from openburrow.core.models.base import BurrowModel, now
from openburrow.core.models.enums import (
    AuditSeverity,
    DelegationStatus,
    TrustBoundary,
)


class AuthorityScope(BurrowModel):
    """A bounded set of capabilities, optionally narrowed from a parent scope.

    Capabilities are strings in ``<verb>:<target>`` form — ``read:src/**``,
    ``write:src/api/**``, ``exec:git commit``, ``delegate:lane``. Free-form
    enough to express real permissions, structured enough to compare and
    intersect, which is what :meth:`narrow` needs.
    """

    id_kind: ClassVar[str] = "delegation"

    capabilities: list[str] = Field(default_factory=list)
    #: Scopes explicitly denied even if an ancestor granted them.
    denials: list[str] = Field(default_factory=list)
    #: Ceilings that travel with the scope.
    max_depth: int = 0
    expires_at: datetime | None = None
    #: Set when this scope came from narrowing another; enables chain tracing.
    derived_from: str = ""

    def allows(self, capability: str) -> bool:
        """Fail-closed capability check.

        An empty scope permits nothing. An explicit denial always beats a grant,
        including a wildcard grant — that ordering is what makes
        ``denials=["exec:git push"]`` meaningful even when the scope contains
        ``exec:*``.

        An empty capability is never granted, and that guard is load-bearing
        rather than defensive tidiness. ``_matches`` treats a bare ``*`` as a
        match for anything, so without this check ``allows("")`` is ``True`` for
        any wildcard scope — which turns every unset or blank capability into a
        granted one. That is reachable in practice: a blank entry in a policy
        file, or a delegation row whose capability field never got populated,
        would sail through :meth:`DelegationLedger.check_action` and the action
        would run. The module's contract is that an unknown capability is
        refused, so the check belongs at the entry point where it cannot be
        bypassed by a caller that forgets to validate its input.
        """
        if not capability or not capability.strip():
            return False
        if any(self._matches(denied, capability) for denied in self.denials):
            return False
        return any(self._matches(granted, capability) for granted in self.capabilities)

    @staticmethod
    def _matches(pattern: str, capability: str) -> bool:
        """Exact match, or ``prefix:*`` wildcard on the verb or the target."""
        if pattern == capability:
            return True
        if pattern == "*":
            return True
        if pattern.endswith(":*"):
            return capability.startswith(pattern[:-1])
        if pattern.endswith("**"):
            return capability.startswith(pattern[:-2])
        return False

    def narrow(
        self, capabilities: list[str], *, denials: list[str] | None = None
    ) -> AuthorityScope:
        """Produce a strict subset — the only sanctioned way to hand off authority.

        Intersects the requested capabilities with what this scope already
        permits, so a delegation can never *widen* its own authority by asking
        for more than it holds. That single behaviour is what makes
        ``authority_inheritance: bounded`` enforceable rather than aspirational.
        """
        granted = [cap for cap in capabilities if self.allows(cap)]
        merged_denials = sorted({*self.denials, *(denials or [])})
        return AuthorityScope(
            capabilities=granted,
            denials=merged_denials,
            max_depth=max(0, self.max_depth - 1),
            derived_from=self.id,
        )

    @classmethod
    def unrestricted(cls) -> AuthorityScope:
        """The human's own scope — the root of every delegation chain.

        Only ever created from an explicit human action; there is no code path
        where an agent constructs this for itself.
        """
        return cls(capabilities=["*"], max_depth=10)

    @classmethod
    def read_only(cls) -> AuthorityScope:
        return cls(capabilities=["read:*"], max_depth=0)

    def describe(self) -> str:
        if not self.capabilities:
            return "(no authority)"
        text = ", ".join(self.capabilities[:5])
        if len(self.capabilities) > 5:
            text += f" (+{len(self.capabilities) - 5} more)"
        if self.denials:
            text += f"  [denies: {', '.join(self.denials[:3])}]"
        return text


class Delegation(BurrowModel):
    """One transfer of authority from a human (via an agent) to another agent.

    The chain is the point. ``parent_delegation_id`` links a sub-delegation to
    the one that authorised it, so :func:`walk_chain` can reconstruct the full
    path from the originating human to the acting agent — which is precisely the
    reconstruction item 182 requires and none of the underlying protocols provide.
    """

    id_kind: ClassVar[str] = "delegation"

    session_id: str = ""
    task_id: str = ""

    # --- who -----------------------------------------------------------------
    #: The HUMAN who authorised this. Never an agent. This is the accountability anchor.
    authorized_by: str = ""
    authorized_by_email: str = ""
    #: Lane the authority originated from.
    delegator_lane: str = ""
    #: Lane receiving it.
    delegatee_lane: str = ""
    delegatee_owner: str = ""

    # --- chain ---------------------------------------------------------------
    parent_delegation_id: str = ""
    depth: int = 0
    #: Full ancestor ids, denormalised so chain queries are a single read.
    chain: list[str] = Field(default_factory=list)

    # --- authority -----------------------------------------------------------
    scope: AuthorityScope = Field(default_factory=AuthorityScope)
    #: Scope the delegator actually held, for the "did they have the right to give this?" check.
    parent_scope_snapshot: list[str] = Field(default_factory=list)
    #: True when the delegation narrowed rather than reproduced its parent scope.
    narrowed: bool = True

    # --- what ----------------------------------------------------------------
    purpose: str = ""
    task_description: str = ""
    transferable: bool = False
    trust_boundary: TrustBoundary = TrustBoundary.INTRA_REPO
    status: DelegationStatus = DelegationStatus.PROPOSED

    # --- consent -------------------------------------------------------------
    #: Set when the human explicitly clicked/typed approval, vs. an implicit
    #: policy-driven grant. The distinction matters for the audit report.
    explicit_consent: bool = False
    consent_evidence: str = ""
    revoked_by: str = ""
    revoked_reason: str = ""
    expires_at: datetime | None = None

    @field_validator("authorized_by", mode="before")
    @classmethod
    def _human_required(cls, value: object) -> str:
        """A delegation without a human author is exactly the gap we are closing."""
        text = str(value or "").strip()
        if not text:
            raise ValueError(
                "a delegation must record the human who authorized it; "
                "agents cannot originate their own authority"
            )
        return text

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_active(self) -> bool:
        return self.status == DelegationStatus.ACTIVE

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_root(self) -> bool:
        """True when this delegation came directly from a human."""
        return not self.parent_delegation_id

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_cross_boundary(self) -> bool:
        return self.trust_boundary not in {TrustBoundary.INTRA_REPO, TrustBoundary.INTRA_LANE}

    def activate(self) -> None:
        self.status = DelegationStatus.ACTIVE
        self.touch()

    def complete(self) -> None:
        self.status = DelegationStatus.COMPLETED
        self.touch()

    def revoke(self, *, by: str, reason: str = "") -> None:
        self.status = DelegationStatus.REVOKED
        self.revoked_by = by
        self.revoked_reason = reason
        self.touch()

    def deny(self, *, reason: str = "") -> None:
        self.status = DelegationStatus.DENIED
        self.revoked_reason = reason
        self.touch()

    def authority_line(self) -> str:
        """One-line accountability rendering, used in `burrow audit`."""
        return (
            f"{self.authorized_by} → {self.delegator_lane} → {self.delegatee_lane} "
            f"[depth {self.depth}] {{{self.scope.describe()}}}"
        )


class AuditRecord(BurrowModel):
    """An immutable audit entry.

    Records are append-only by convention and never updated in place. When the
    severity is ``VIOLATION`` or ``CRITICAL`` the record is additionally
    mirrored into the relay (if enabled) so it survives a local disk loss —
    the audit trail is the one thing that must outlive the machine.
    """

    id_kind: ClassVar[str] = "audit"

    session_id: str = ""
    lane_id: str = ""
    task_id: str = ""
    delegation_id: str = ""
    message_id: str = ""

    # --- classification ------------------------------------------------------
    #: What kind of thing happened: delegation.created, policy.blocked,
    #: message.cross_boundary, capability.mismatch, authority.creep …
    event: str = ""
    severity: AuditSeverity = AuditSeverity.INFO
    trust_boundary: TrustBoundary = TrustBoundary.INTRA_REPO
    #: Stricter retention + relay mirroring for cross-boundary records (item 184).
    strict: bool = False

    summary: str = ""
    detail: dict[str, Any] = Field(default_factory=dict)

    # --- who -----------------------------------------------------------------
    actor_lane: str = ""
    actor_harness: str = ""
    #: Human on whose behalf the actor was working.
    on_behalf_of: str = ""
    affected_lanes: list[str] = Field(default_factory=list)

    # --- outcome -------------------------------------------------------------
    allowed: bool = True
    reason: str = ""
    #: Correlates this record with the policy check / approval that produced it.
    correlation_id: str = ""

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_violation(self) -> bool:
        return self.severity in {AuditSeverity.VIOLATION, AuditSeverity.CRITICAL}

    @classmethod
    def cross_boundary(
        cls,
        *,
        session_id: str,
        message_id: str,
        actor_lane: str,
        on_behalf_of: str,
        boundary: TrustBoundary,
        summary: str,
        **extra: Any,
    ) -> AuditRecord:
        return cls(
            session_id=session_id,
            message_id=message_id,
            actor_lane=actor_lane,
            on_behalf_of=on_behalf_of,
            trust_boundary=boundary,
            strict=True,
            event="message.cross_boundary",
            severity=AuditSeverity.NOTICE,
            summary=summary,
            **extra,
        )

    @classmethod
    def blocked(
        cls,
        *,
        session_id: str,
        lane_id: str,
        event: str,
        reason: str,
        summary: str,
        **extra: Any,
    ) -> AuditRecord:
        return cls(
            session_id=session_id,
            lane_id=lane_id,
            actor_lane=lane_id,
            event=event,
            severity=AuditSeverity.VIOLATION,
            allowed=False,
            reason=reason,
            summary=summary,
            **extra,
        )


#: Every flag kind the governance layer emits. Kept here rather than derived at
#: runtime because the value is in it being *written down*: it is the list a UI,
#: an alerting rule, or a runbook author reads to find out what they must handle.
#:
#: Nothing validates ``GovernanceFlag.kind`` against this at construction time —
#: it is deliberately an open string so a new detector does not need a schema
#: change. What ``tests/test_flag_kinds.py`` does instead is scan the detector and
#: ledger sources for emitted kinds and assert the two sets are equal. So adding
#: a detector with a new kind fails the suite until this list is updated, and
#: this list cannot accumulate entries nothing emits.
#:
#: The two directions catch different mistakes. A missing entry means a consumer
#: silently ignores a real flag. A stale entry means a consumer handles a case
#: that can no longer happen, which is how dead branches survive for years.
FLAG_KINDS: frozenset[str] = frozenset(
    {
        "adversarial_intent",
        "authority_creep",
        "capability_mismatch",
        "capability_overclaim",
        "capability_undeclared",
        "credential_access_attempt",
        "delegation_purpose_mismatch",
        "impersonation",
        "low_confidence_lesson",
        "poisoned_delegation",
        "poisoned_lesson",
        "prompt_injection",
        "redelegation_denied",
        "secret_in_message",
        "signature_invalid",
    }
)


class GovernanceFlag(BurrowModel):
    """A detected anomaly a human should review.

    Flags are raised, not enforced — the enforcing happens in
    :class:`~openburrow.core.models.governance.AuthorityScope`. Keeping detection
    and enforcement separate means a false positive degrades to a notification
    rather than a stalled session, which is the right failure direction for a
    system whose whole pitch is "you can trust what it did".
    """

    id_kind: ClassVar[str] = "audit"

    session_id: str = ""
    #: Which detector or ledger check raised this. An open string rather than an
    #: enum, so a detector can add a kind without a schema migration. The set of
    #: kinds in use is :data:`FLAG_KINDS`, which is checked against the source by
    #: ``tests/test_flag_kinds.py`` — an unlisted kind fails the suite rather
    #: than quietly rendering as a blank chip in the UI.
    kind: str = ""
    severity: AuditSeverity = AuditSeverity.WARNING
    summary: str = ""
    detail: dict[str, Any] = Field(default_factory=dict)

    lane_id: str = ""
    delegation_id: str = ""
    message_id: str = ""

    #: What the flag recommends a human do.
    recommended_action: str = ""
    #: Blocking flags pause the affected lane until resolved.
    blocking: bool = False
    resolved: bool = False
    resolved_by: str = ""
    resolution: str = ""
    resolved_at: datetime | None = None

    def resolve(self, *, by: str, resolution: str = "") -> None:
        self.resolved = True
        self.resolved_by = by
        self.resolution = resolution
        self.resolved_at = now()
        self.touch()

    @classmethod
    def authority_creep(
        cls, *, session_id: str, delegation_id: str, detail: dict[str, Any]
    ) -> GovernanceFlag:
        return cls(
            session_id=session_id,
            delegation_id=delegation_id,
            kind="authority_creep",
            severity=AuditSeverity.VIOLATION,
            summary="a delegation chain expanded scope beyond the original human approval",
            detail=detail,
            recommended_action="review the delegation chain with `burrow audit <session>`",
            blocking=True,
        )

    @classmethod
    def capability_mismatch(
        cls, *, session_id: str, lane_id: str, declared: list[str], observed: list[str]
    ) -> GovernanceFlag:
        return cls(
            session_id=session_id,
            lane_id=lane_id,
            kind="capability_mismatch",
            severity=AuditSeverity.WARNING,
            summary=f"lane {lane_id} declared skills it did not use, or used skills it did not declare",
            detail={"declared": declared, "observed": observed},
            recommended_action="confirm the Agent Card matches the harness's real behaviour",
        )


__all__ = ["AuditRecord", "AuthorityScope", "Delegation", "GovernanceFlag"]
