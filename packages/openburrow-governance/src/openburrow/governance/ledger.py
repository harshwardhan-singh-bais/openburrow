"""The delegation-accountability ledger.

A2A describes how agents exchange messages. It says nothing about *who is
accountable* when Agent A delegates to Agent B, what authority actually
transferred, or how a cross-boundary action gets audited. The protocol authors
have acknowledged this gap in print. This module is OpenBurrow's answer to it.

The core invariant, enforced in :meth:`DelegationLedger.authorize`:

    **Authority originates with a human and can only ever narrow.**

Three consequences follow, and each is a specific check:

1. A delegation must name a human (``authorized_by``). An agent cannot
   bootstrap its own authority — :class:`~openburrow.core.models.Delegation`
   refuses to construct without one.
2. A delegated scope must be a strict subset of the delegator's. Asking for
   more is not "escalation", it is a rejected request, and it is rejected at
   the point of request rather than logged after the fact.
3. Chain depth is bounded. Each hop narrows the remaining depth, so a chain
   cannot grow indefinitely and quietly accumulate capability.

Everything here is *enforcement*, not detection. Detection lives in
:mod:`openburrow.governance.detectors`, and the separation is deliberate: a
false positive in detection should produce a notification, never a stalled
session.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from openburrow.core.config.settings import Settings
from openburrow.core.db.repository import AuditLog, Repository
from openburrow.core.errors import (
    GovernanceError,
    RedelegationDeniedError,
    SilentAuthorityCreepError,
)
from openburrow.core.logging import get_logger
from openburrow.core.models import (
    AuditRecord,
    AuditSeverity,
    AuthorityScope,
    Delegation,
    DelegationStatus,
    GovernanceFlag,
    Lane,
    TrustBoundary,
    now,
)

log = get_logger(__name__)


@dataclass(slots=True)
class AuthorizationResult:
    """Outcome of an authorization attempt."""

    allowed: bool
    delegation: Delegation | None = None
    scope: AuthorityScope | None = None
    reason: str = ""
    flag: GovernanceFlag | None = None

    def __bool__(self) -> bool:
        return self.allowed


class DelegationLedger:
    """Records and validates every transfer of authority."""

    def __init__(
        self,
        repo: Repository,
        audit: AuditLog,
        settings: Settings,
        *,
        human_id: str = "",
        human_email: str = "",
        max_depth: int = 3,
        allow_redelegation: bool = False,
        authority_inheritance: str = "bounded",
    ) -> None:
        self.repo = repo
        self.audit = audit
        self.settings = settings
        self.human_id = human_id
        self.human_email = human_email
        self.max_depth = max_depth
        self.allow_redelegation = allow_redelegation
        self.authority_inheritance = authority_inheritance

    # --- authorization -----------------------------------------------------
    async def authorize(
        self,
        *,
        session_id: str,
        delegator_lane: Lane,
        delegatee_lane: Lane,
        task_id: str,
        requested_scope: list[str],
        purpose: str,
        task_description: str = "",
        parent_delegation_id: str = "",
        trust_boundary: TrustBoundary = TrustBoundary.INTRA_REPO,
        transferable: bool = False,
        explicit_consent: bool = False,
        consent_evidence: str = "",
    ) -> AuthorizationResult:
        """Validate and record a delegation.

        Returns a result rather than raising for the *policy* outcomes (denied
        scope, too deep, not transferable), because those are ordinary
        refusals the caller must handle. It raises only for programmer errors
        such as a delegation with no human attached.
        """
        # --- 1. the human anchor ------------------------------------------
        authorizer = self.human_id or delegator_lane.owner
        if not authorizer:
            raise GovernanceError(
                "delegation has no human authorizer",
                hint=(
                    "Set OPENBURROW_GOVERNANCE_HUMAN_ID, or give the delegating lane "
                    "an owner. Agents cannot originate their own authority."
                ),
                context={"delegator_lane": delegator_lane.id, "session_id": session_id},
            )

        # --- 2. the chain -------------------------------------------------
        parent: Delegation | None = None
        chain: list[str] = []
        depth = 0
        parent_scope: AuthorityScope | None = None

        if parent_delegation_id:
            parent = await self.repo.get(Delegation, parent_delegation_id)
            if parent is None:
                return await self._deny(
                    session_id=session_id,
                    lane_id=delegator_lane.id,
                    reason=f"parent delegation {parent_delegation_id} not found",
                    summary="delegation rejected: unknown parent",
                )
            depth = parent.depth + 1
            chain = [*parent.chain, parent.id]
            parent_scope = parent.scope

            if depth > self.max_depth:
                return await self._deny(
                    session_id=session_id,
                    lane_id=delegator_lane.id,
                    reason=(
                        f"delegation depth {depth} exceeds the configured maximum "
                        f"of {self.max_depth}"
                    ),
                    summary="delegation rejected: chain too deep",
                    detail={"depth": depth, "max_depth": self.max_depth, "chain": chain},
                )

            # --- 3. re-delegation consent --------------------------------
            if not parent.transferable and not self.allow_redelegation:
                flag = GovernanceFlag(
                    session_id=session_id,
                    delegation_id=parent.id,
                    kind="redelegation_denied",
                    severity=AuditSeverity.VIOLATION,
                    summary=(
                        f"lane {delegator_lane.id} tried to re-delegate a task marked "
                        "non-transferable"
                    ),
                    detail={"parent": parent.id, "requested_by": delegator_lane.id},
                    recommended_action="approve explicitly as a human, or do the work in this lane",
                    blocking=True,
                )
                await self._flag(flag)
                raise RedelegationDeniedError(
                    f"delegation {parent.id} is not transferable",
                    hint=(
                        "The human who created it did not grant onward delegation. "
                        "Ask them to re-authorize, or complete the task here."
                    ),
                    context={"parent_delegation_id": parent.id, "lane": delegator_lane.id},
                )

        # --- 4. scope narrowing -------------------------------------------
        if parent_scope is not None:
            granted = parent_scope.narrow(requested_scope)
        else:
            # Root delegation: the delegator's own scope is the ceiling.
            base = AuthorityScope(
                capabilities=list(delegator_lane.authority_scope) or ["*"],
                max_depth=self.max_depth,
            )
            granted = base.narrow(requested_scope)

        if self.authority_inheritance == "none" and not requested_scope:
            granted = AuthorityScope(capabilities=[], max_depth=max(0, depth and 0))

        # Asking for something you were not granted is the silent-creep case.
        ungranted = [cap for cap in requested_scope if not granted.allows(cap)]
        if ungranted:
            flag = GovernanceFlag(
                session_id=session_id,
                delegation_id=parent_delegation_id,
                kind="authority_creep",
                severity=AuditSeverity.VIOLATION,
                summary=(
                    f"lane {delegator_lane.id} requested authority it does not hold: "
                    f"{', '.join(ungranted[:5])}"
                ),
                detail={
                    "requested": requested_scope,
                    "granted": granted.capabilities,
                    "ungranted": ungranted,
                    "chain": chain,
                },
                recommended_action="review the delegation chain with `burrow audit <session>`",
                blocking=True,
            )
            await self._flag(flag)
            raise SilentAuthorityCreepError(
                f"requested authority exceeds what {delegator_lane.id} holds",
                hint=(
                    "Delegation narrows authority by default. Grant the missing "
                    "capability at the root, as a human, if it is genuinely needed."
                ),
                context={"ungranted": ungranted, "requested": requested_scope},
            )

        # --- 5. record ----------------------------------------------------
        delegation = Delegation(
            session_id=session_id,
            task_id=task_id,
            authorized_by=authorizer,
            authorized_by_email=self.human_email,
            delegator_lane=delegator_lane.id,
            delegatee_lane=delegatee_lane.id,
            delegatee_owner=delegatee_lane.owner,
            parent_delegation_id=parent_delegation_id,
            depth=depth,
            chain=chain,
            scope=granted,
            parent_scope_snapshot=list(parent_scope.capabilities) if parent_scope else ["*"],
            narrowed=len(granted.capabilities) < len(requested_scope) or bool(parent_scope),
            purpose=purpose,
            task_description=task_description,
            transferable=transferable,
            trust_boundary=trust_boundary,
            explicit_consent=explicit_consent,
            consent_evidence=consent_evidence,
            status=DelegationStatus.ACTIVE,
        )
        await self.repo.save(delegation)

        await self.audit.record(
            AuditRecord(
                session_id=session_id,
                lane_id=delegator_lane.id,
                task_id=task_id,
                delegation_id=delegation.id,
                event="delegation.created",
                severity=AuditSeverity.NOTICE if depth == 0 else AuditSeverity.INFO,
                trust_boundary=trust_boundary,
                strict=trust_boundary not in {TrustBoundary.INTRA_REPO, TrustBoundary.INTRA_LANE},
                summary=delegation.authority_line(),
                detail={
                    "scope": granted.capabilities,
                    "depth": depth,
                    "chain": chain,
                    "purpose": purpose,
                },
                actor_lane=delegator_lane.id,
                actor_harness=delegator_lane.harness,
                on_behalf_of=authorizer,
                affected_lanes=[delegatee_lane.id],
                allowed=True,
            )
        )

        log.info(
            "governance.delegation_authorized",
            delegation_id=delegation.id,
            depth=depth,
            capabilities=len(granted.capabilities),
            authorized_by=authorizer,
        )
        return AuthorizationResult(allowed=True, delegation=delegation, scope=granted)

    # --- enforcement -------------------------------------------------------
    async def check_action(
        self,
        *,
        session_id: str,
        delegation_id: str,
        lane_id: str,
        capability: str,
        action: str,
    ) -> bool:
        """Gate a concrete action against the authority a delegation granted.

        Called immediately before a delegated action executes. Returning False
        is the enforcement half of the gap this layer closes — the action does
        not run, and the refusal is audited.
        """
        if not delegation_id:
            return True  # not delegated work; the policy gate handles local actions

        delegation = await self.repo.get(Delegation, delegation_id)
        if delegation is None:
            await self._violation(
                session_id=session_id,
                lane_id=lane_id,
                event="action.unknown_delegation",
                summary=f"action '{action}' referenced unknown delegation {delegation_id}",
            )
            return False

        if delegation.status != DelegationStatus.ACTIVE:
            await self._violation(
                session_id=session_id,
                lane_id=lane_id,
                event="action.inactive_delegation",
                summary=(f"action '{action}' attempted under a {delegation.status} delegation"),
            )
            return False

        if delegation.expires_at is not None and now() > delegation.expires_at:
            await self._violation(
                session_id=session_id,
                lane_id=lane_id,
                event="action.expired_delegation",
                summary=f"action '{action}' attempted under an expired delegation",
            )
            return False

        if not delegation.scope.allows(capability):
            await self._violation(
                session_id=session_id,
                lane_id=lane_id,
                event="action.outside_scope",
                summary=(
                    f"action '{action}' requires '{capability}', which delegation "
                    f"{delegation.id} did not grant"
                ),
                detail={
                    "capability": capability,
                    "granted": delegation.scope.capabilities,
                    "action": action,
                },
            )
            return False

        return True

    # --- chain reconstruction ---------------------------------------------
    async def trace(self, delegation_id: str) -> list[Delegation]:
        """Reconstruct the full chain from the originating human to the actor.

        Item 182. This is the operation the underlying protocols cannot perform,
        because they never recorded the human in the first place.
        """
        return await self.repo.delegation_chain(delegation_id)

    async def accountability_report(self, delegation_id: str) -> str:
        """Human-readable chain rendering for ``burrow audit --delegation``."""
        chain = await self.trace(delegation_id)
        if not chain:
            return f"no delegation chain found for {delegation_id}"

        lines = [f"Delegation chain for {delegation_id}", ""]
        root = chain[0]
        lines.append(f"  ORIGIN  human: {root.authorized_by}")
        if root.authorized_by_email:
            lines.append(f"          email: {root.authorized_by_email}")
        lines.append("")

        for index, delegation in enumerate(chain):
            indent = "  " * (index + 1)
            lines.append(
                f"{indent}hop {delegation.depth}: "
                f"{delegation.delegator_lane} → {delegation.delegatee_lane}"
            )
            lines.append(f"{indent}  scope: {delegation.scope.describe()}")
            lines.append(
                f"{indent}  consent: "
                f"{'explicit' if delegation.explicit_consent else 'policy-derived'}"
                f"  transferable: {delegation.transferable}"
            )
            lines.append(f"{indent}  purpose: {delegation.purpose}")
            lines.append(f"{indent}  status: {delegation.status}")
            lines.append("")

        lines.append(f"  Final actor: {chain[-1].delegatee_lane}")
        lines.append(f"  On behalf of: {chain[-1].authorized_by}")
        return "\n".join(lines)

    # --- revocation --------------------------------------------------------
    async def revoke(self, delegation_id: str, *, by: str, reason: str = "") -> bool:
        delegation = await self.repo.get(Delegation, delegation_id)
        if delegation is None:
            return False
        delegation.revoke(by=by, reason=reason)
        await self.repo.save(delegation)
        await self.audit.record(
            AuditRecord(
                session_id=delegation.session_id,
                delegation_id=delegation.id,
                event="delegation.revoked",
                severity=AuditSeverity.WARNING,
                trust_boundary=delegation.trust_boundary,
                strict=True,
                summary=f"delegation revoked by {by}: {reason or 'no reason given'}",
                actor_lane=by,
                on_behalf_of=delegation.authorized_by,
                affected_lanes=[delegation.delegatee_lane],
                allowed=False,
                reason=reason,
            )
        )
        log.info("governance.delegation_revoked", delegation_id=delegation_id, by=by)
        return True

    # --- internals ---------------------------------------------------------
    async def _deny(
        self,
        *,
        session_id: str,
        lane_id: str,
        reason: str,
        summary: str,
        detail: dict | None = None,
    ) -> AuthorizationResult:
        await self._violation(
            session_id=session_id,
            lane_id=lane_id,
            event="delegation.denied",
            summary=summary,
            detail={"reason": reason, **(detail or {})},
        )
        return AuthorizationResult(allowed=False, reason=reason)

    async def _violation(
        self,
        *,
        session_id: str,
        lane_id: str,
        event: str,
        summary: str,
        detail: dict | None = None,
    ) -> None:
        await self.audit.record(
            AuditRecord.blocked(
                session_id=session_id,
                lane_id=lane_id,
                event=event,
                reason=summary,
                summary=summary,
                detail=detail or {},
            )
        )

    async def _flag(self, flag: GovernanceFlag) -> None:
        await self.repo.save(flag)
        await self.audit.record(
            AuditRecord(
                session_id=flag.session_id,
                lane_id=flag.lane_id,
                delegation_id=flag.delegation_id,
                event=f"governance.{flag.kind}",
                severity=flag.severity,
                strict=True,
                summary=flag.summary,
                detail=flag.detail,
                allowed=False,
                reason=flag.summary,
                correlation_id=flag.id,
            )
        )
        log.warning("governance.flag_raised", kind=flag.kind, summary=flag.summary)


def default_scope_for_role(role: str) -> AuthorityScope:
    """Sensible starting scopes per lane role.

    Used when a lane template does not declare an explicit scope. A reviewer
    gets read-only plus the ability to propose; an implementer gets write access
    to its claimed paths; an observer gets nothing but reads.
    """
    mapping: dict[str, list[str]] = {
        "reviewer": ["read:*", "inform:*", "propose:*"],
        "implementer": ["read:*", "write:*", "exec:test", "propose:*"],
        "coordinator": ["read:*", "delegate:*", "adjudicate:*"],
        "observer": ["read:*"],
    }
    return AuthorityScope(capabilities=mapping.get(str(role), ["read:*"]), max_depth=1)


def scope_ttl(seconds: int) -> object:
    """Helper for building time-bounded scopes."""
    return now() + timedelta(seconds=seconds)


__all__ = ["AuthorizationResult", "DelegationLedger", "default_scope_for_role", "scope_ttl"]
