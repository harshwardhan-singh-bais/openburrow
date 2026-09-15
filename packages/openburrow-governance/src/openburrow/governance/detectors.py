"""Anomaly detectors — the detection half of the governance layer.

Everything in :mod:`openburrow.governance.ledger` *enforces*. Everything here
*notices*. Keeping the two apart is a deliberate failure-direction choice: a
false positive from a detector produces a flag a human can dismiss, whereas a
false positive from an enforcer stalls a session. Detection is allowed to be
noisy; enforcement is not.

The detectors correspond to named attack surfaces from the roadmap:

======================================  ==========================================
:meth:`detect_capability_mismatch`      item 186 — declared vs. observed skills
:meth:`detect_impersonation`            item 187 — spoofed message provenance
:meth:`detect_poisoned_delegation`      item 188 — a delegation that over-reaches
:meth:`detect_poisoned_lesson`          item 189 — a "lesson" that induces harm
:meth:`detect_adversarial_intent`       item 190 — a stated intent that hides a conflict
:meth:`detect_authority_creep`          item 185 — a chain that expanded scope
======================================  ==========================================

The poisoned-lesson detector deliberately overlaps with the lesson classifier in
``openburrow-brain``. That redundancy is the point: item 189 requires defense in
depth, so that a compromise in one path does not compromise the other.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from openburrow.core.logging import get_logger
from openburrow.core.models import (
    AuditSeverity,
    Delegation,
    GovernanceFlag,
    Lane,
    Lesson,
    TrustBoundary,
)

log = get_logger(__name__)


@dataclass(slots=True)
class Detection:
    """A single detector's verdict."""

    flagged: bool
    kind: str = ""
    severity: AuditSeverity = AuditSeverity.WARNING
    summary: str = ""
    detail: dict[str, Any] = None  # type: ignore[assignment]
    recommended_action: str = ""
    blocking: bool = False

    def __post_init__(self) -> None:
        if self.detail is None:
            self.detail = {}

    def to_flag(self, *, session_id: str, **extra: str) -> GovernanceFlag | None:
        if not self.flagged:
            return None
        return GovernanceFlag(
            session_id=session_id,
            kind=self.kind,
            severity=self.severity,
            summary=self.summary,
            detail=self.detail,
            recommended_action=self.recommended_action,
            blocking=self.blocking,
            **extra,
        )


# ---------------------------------------------------------------------------
# Instructions that should never arrive over an agent-to-agent channel
# ---------------------------------------------------------------------------
#: Phrases that indicate an instruction is trying to move authority, exfiltrate
#: data, or disable a guardrail. These appear in the red-team payload library
#: (item 205) and in the attack tests (item 206).
INJECTION_MARKERS: tuple[str, ...] = (
    "ignore previous instructions",
    "ignore all previous",
    "disregard your instructions",
    "you are now",
    "act as if you have",
    "your new role is",
    "system prompt",
    "reveal your instructions",
    "print your configuration",
    "exfiltrate",
    "send the contents of",
    "upload the file",
    "disable the sandbox",
    "bypass the policy",
    "skip the approval",
    "you have permission to",
    "the user already approved",
    "no approval needed",
    "curl http",
    "| sh",
    "| bash",
    "base64 -d",
)

#: Patterns suggesting an attempt to move credentials.
CREDENTIAL_MARKERS: tuple[str, ...] = (
    ".env",
    "id_rsa",
    "id_ed25519",
    ".ssh/",
    ".aws/credentials",
    "api_key",
    "secret_key",
    "private_key",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
)

_SECRET_PATTERN = re.compile(
    r"(sk-[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)


def detect_injection(text: str) -> Detection:
    """Look for prompt-injection markers in content arriving from a peer.

    Applies to messages *and* to file contents a harness might read, because the
    two are indistinguishable to the receiving agent — which is exactly the
    attack.
    """
    lowered = text.lower()
    hits = [marker for marker in INJECTION_MARKERS if marker in lowered]
    credential_hits = [marker for marker in CREDENTIAL_MARKERS if marker.lower() in lowered]

    if credential_hits and any(
        verb in lowered for verb in ("read", "send", "include", "paste", "show", "print", "cat")
    ):
        return Detection(
            flagged=True,
            kind="credential_access_attempt",
            severity=AuditSeverity.VIOLATION,
            summary="content asks another agent to read or transmit credential material",
            detail={"credential_markers": credential_hits},
            recommended_action="reject the message; review the sender's lane and delegation chain",
            blocking=True,
        )

    if hits:
        return Detection(
            flagged=True,
            kind="prompt_injection",
            severity=AuditSeverity.VIOLATION if len(hits) > 1 else AuditSeverity.WARNING,
            summary=f"content contains {len(hits)} prompt-injection marker(s)",
            detail={"markers": hits[:8]},
            recommended_action="quarantine the message; do not act on its instructions",
            blocking=len(hits) > 1,
        )

    if _SECRET_PATTERN.search(text):
        return Detection(
            flagged=True,
            kind="secret_in_message",
            severity=AuditSeverity.CRITICAL,
            summary="an outbound or inbound message appears to contain a live credential",
            detail={"pattern": "secret-looking token"},
            recommended_action="the scrubber should have caught this; investigate the redaction path",
            blocking=True,
        )

    return Detection(flagged=False)


def detect_capability_mismatch(lane: Lane) -> Detection:
    """Compare declared Agent Card skills against observed behaviour (item 186).

    Three cases, not two:

    * declared but never used — the card over-claims, which matters because
      other lanes make delegation decisions from it
    * used but never declared — the lane did something it never said it could,
      which is the more serious direction
    * used while declaring nothing — the comparison cannot be made at all, so
      the card is unverified rather than clean. Reported as
      ``capability_undeclared``.

    The ordering matters when both conditions hold at once: an undeclared use
    is returned before an over-claim, because reporting the cosmetic problem
    instead of the serious one is worse than reporting neither.
    """
    declared = set(lane.declared_skills)
    observed = set(lane.observed_skills)

    # No observed behaviour means there is genuinely nothing to compare, so a
    # clean verdict is honest here.
    if not observed:
        return Detection(flagged=False)

    # An empty *declaration* is a different case, and collapsing it into the
    # clean verdict above was a real blind spot. The lane acted, and its card
    # accounts for none of what it did — which is the "used but never declared"
    # direction this docstring calls the more serious one, in its purest form.
    #
    # It is reported as a NOTICE rather than a VIOLATION, and deliberately not
    # as ``blocking``. An empty declared list has two possible causes that this
    # function cannot tell apart from the data it is given: the harness really
    # did over-reach, or the adapter has no skill introspection at all and
    # ``Lane.declared_skills`` was left at its default. Flagging the second as
    # blocking would stall every session on that harness — an enforcement
    # mistake. Flagging it as a dismissable notice is a detection mistake at
    # worst, which is the trade this module's failure-direction doctrine
    # chooses. What is *not* licensed by that doctrine is silence: a clean
    # verdict here is indistinguishable from "we checked and the card was
    # accurate", and we did not check.
    if not declared:
        return Detection(
            flagged=True,
            kind="capability_undeclared",
            severity=AuditSeverity.NOTICE,
            summary=(
                f"lane {lane.name} used {len(observed)} skill(s) but declared none, "
                "so its Agent Card could not be verified"
            ),
            detail={"declared": [], "observed": sorted(observed)},
            recommended_action=(
                "check whether the harness publishes a card; if it does, the lane acted outside it"
            ),
        )

    undeclared = sorted(observed - declared)
    unused = sorted(declared - observed)

    if undeclared:
        return Detection(
            flagged=True,
            kind="capability_mismatch",
            severity=AuditSeverity.VIOLATION,
            summary=f"lane {lane.name} used skills it never declared: {', '.join(undeclared[:5])}",
            detail={
                "declared": sorted(declared),
                "observed": sorted(observed),
                "undeclared": undeclared,
            },
            recommended_action="verify the Agent Card matches the harness's real behaviour",
            blocking=True,
        )

    if len(unused) > max(2, len(declared) // 2):
        return Detection(
            flagged=True,
            kind="capability_overclaim",
            severity=AuditSeverity.NOTICE,
            summary=(
                f"lane {lane.name} declared {len(unused)} skills it never used; "
                "other lanes may over-trust its card"
            ),
            detail={"declared": sorted(declared), "observed": sorted(observed), "unused": unused},
            recommended_action="narrow the declared skills to what the harness actually does",
        )

    return Detection(flagged=False)


def detect_impersonation(
    *,
    claimed_lane: str,
    actual_lane: str,
    claimed_harness: str = "",
    actual_harness: str = "",
    signature_valid: bool | None = None,
) -> Detection:
    """Verify message provenance so one lane cannot be spoofed as another (item 187)."""
    if claimed_lane and actual_lane and claimed_lane != actual_lane:
        return Detection(
            flagged=True,
            kind="impersonation",
            severity=AuditSeverity.CRITICAL,
            summary=(
                f"message claimed to come from {claimed_lane} but arrived on "
                f"{actual_lane}'s connection"
            ),
            detail={
                "claimed_lane": claimed_lane,
                "actual_lane": actual_lane,
                "claimed_harness": claimed_harness,
                "actual_harness": actual_harness,
            },
            recommended_action="reject the message and inspect the sending lane's process",
            blocking=True,
        )

    if signature_valid is False:
        return Detection(
            flagged=True,
            kind="signature_invalid",
            severity=AuditSeverity.CRITICAL,
            summary="message signature did not verify against the claimed sender",
            detail={"claimed_lane": claimed_lane},
            recommended_action="reject the message; the local SSH key may have changed",
            blocking=True,
        )

    return Detection(flagged=False)


def detect_poisoned_delegation(
    *,
    delegation: Delegation,
    requested_capabilities: list[str],
    delegator_scope: list[str],
) -> Detection:
    """A delegation crafted to make the receiver act outside its real authority.

    Unlike the ledger's enforcement (which refuses the delegation outright),
    this detector reasons about *intent*: a delegation whose stated purpose does
    not match the authority it asks for is suspicious even when every individual
    capability is technically within scope.
    """
    from openburrow.core.models import AuthorityScope

    ceiling = AuthorityScope(capabilities=delegator_scope or ["*"])
    over_reach = [cap for cap in requested_capabilities if not ceiling.allows(cap)]

    if over_reach:
        return Detection(
            flagged=True,
            kind="poisoned_delegation",
            severity=AuditSeverity.VIOLATION,
            summary=(
                f"delegation '{delegation.purpose}' requests authority the delegator "
                f"does not hold: {', '.join(over_reach[:5])}"
            ),
            detail={
                "delegation_id": delegation.id,
                "requested": requested_capabilities,
                "delegator_scope": delegator_scope,
                "over_reach": over_reach,
            },
            recommended_action="reject the delegation; verify who authorized it",
            blocking=True,
        )

    dangerous = [
        cap
        for cap in requested_capabilities
        if any(token in cap for token in ("exec:", "write:", "network:", "secrets:"))
    ]
    purpose = delegation.purpose.lower()
    reads_only = any(word in purpose for word in ("review", "read", "inspect", "check", "audit"))

    if reads_only and dangerous:
        return Detection(
            flagged=True,
            kind="delegation_purpose_mismatch",
            severity=AuditSeverity.WARNING,
            summary=(
                f"delegation described as '{delegation.purpose}' requests write or "
                f"exec authority: {', '.join(dangerous[:5])}"
            ),
            detail={"purpose": delegation.purpose, "dangerous_capabilities": dangerous},
            recommended_action="confirm the delegation's purpose matches the authority it carries",
        )

    return Detection(flagged=False)


def detect_poisoned_lesson(lesson: Lesson) -> Detection:
    """A 'lesson' designed to make another agent take a harmful action (item 189).

    Runs independently of the Brain's own classifier, so a compromise in one
    path does not defeat both. Defense in depth is the explicit requirement here.
    """
    combined = f"{lesson.title}\n{lesson.body}\n{lesson.trigger}\n{lesson.remedy}"
    injection = detect_injection(combined)
    if injection.flagged:
        return Detection(
            flagged=True,
            kind="poisoned_lesson",
            severity=AuditSeverity.VIOLATION,
            summary=f"lesson '{lesson.title}' carries injection markers",
            detail={"lesson_id": lesson.id, **injection.detail},
            recommended_action="retire the lesson and review its source message",
            blocking=True,
        )

    if lesson.confidence < 0.3 and lesson.promoted_by != "human":
        return Detection(
            flagged=True,
            kind="low_confidence_lesson",
            severity=AuditSeverity.NOTICE,
            summary=f"lesson '{lesson.title}' was promoted with very low confidence",
            detail={"lesson_id": lesson.id, "confidence": lesson.confidence},
            recommended_action="confirm before this lesson is injected into another lane",
        )

    return Detection(flagged=False)


def detect_adversarial_intent(
    *,
    stated_intent: str,
    actual_tool_calls: list[str],
    contested_refs: list[str],
) -> Detection:
    """A Merge Radar intent that hides a real conflict (item 190).

    The check: does the set of files the lane *actually touched* intersect the
    set it *claimed* to be working on? A lane that says "I am only touching the
    docs" while its tool calls edit the same interface another lane is changing
    has either made an error or misrepresented its intent.
    """
    touched = set()
    for call in actual_tool_calls:
        for token in re.findall(r"[\w./-]+\.\w{1,5}", call):
            touched.add(token)

    claimed = set(contested_refs)
    if not touched or not claimed:
        return Detection(flagged=False)

    undisclosed = sorted(touched & claimed)
    if undisclosed and not any(ref in stated_intent for ref in undisclosed):
        return Detection(
            flagged=True,
            kind="adversarial_intent",
            severity=AuditSeverity.VIOLATION,
            summary=(
                "a lane touched contested paths it did not disclose in its stated intent: "
                f"{', '.join(undisclosed[:5])}"
            ),
            detail={
                "stated_intent": stated_intent[:500],
                "contested_refs": sorted(claimed),
                "undisclosed_touches": undisclosed,
            },
            recommended_action="open a negotiation before either lane commits",
            blocking=True,
        )

    return Detection(flagged=False)


def detect_authority_creep(chain: list[Delegation]) -> Detection:
    """Flag a delegation chain that expanded scope beyond the original approval (item 185).

    A chain where a later hop holds *more* capability than the root is the
    signature of silent authority creep — each individual hop may look legal
    while the composition is not.
    """
    if len(chain) < 2:
        return Detection(flagged=False)

    root = chain[0]
    root_caps = set(root.scope.capabilities)

    for delegation in chain[1:]:
        caps = set(delegation.scope.capabilities)
        added = caps - root_caps
        if added:
            return Detection(
                flagged=True,
                kind="authority_creep",
                severity=AuditSeverity.VIOLATION,
                summary=(
                    f"delegation chain gained capabilities the original human never "
                    f"approved: {', '.join(sorted(added)[:5])}"
                ),
                detail={
                    "root_delegation": root.id,
                    "root_authorized_by": root.authorized_by,
                    "root_capabilities": sorted(root_caps),
                    "gained_at": delegation.id,
                    "gained": sorted(added),
                    "chain": [d.id for d in chain],
                },
                recommended_action="revoke the affected delegation and review `burrow audit`",
                blocking=True,
            )

    return Detection(flagged=False)


def detect_cross_boundary_message(
    *,
    sender_owner: str,
    recipient_owner: str,
    sender_harness: str,
    recipient_harness: str,
    sender_org: str = "",
    recipient_org: str = "",
) -> TrustBoundary:
    """Classify the trust boundary a message crosses (item 184).

    The classification drives *audit strictness*, not blocking. A cross-org
    message is not inherently wrong; it is inherently more expensive to get
    wrong, so it gets a stricter record.
    """
    if sender_org and recipient_org and sender_org != recipient_org:
        return TrustBoundary.CROSS_ORG
    if sender_owner and recipient_owner and sender_owner != recipient_owner:
        return TrustBoundary.CROSS_HUMAN
    if sender_harness and recipient_harness and sender_harness != recipient_harness:
        return TrustBoundary.CROSS_VENDOR
    return TrustBoundary.INTRA_REPO


def scan_for_secrets(text: str) -> list[str]:
    """Return any secret-looking tokens found in ``text``.

    Used by the outbound scrubber (item 201) and the pre-commit scanner
    (item 247). Returns matches, never the surrounding context, so the scanner
    itself cannot become a leak vector.
    """
    return [match.group(0)[:8] + "…" for match in _SECRET_PATTERN.finditer(text)]


__all__ = [
    "CREDENTIAL_MARKERS",
    "INJECTION_MARKERS",
    "Detection",
    "detect_adversarial_intent",
    "detect_authority_creep",
    "detect_capability_mismatch",
    "detect_cross_boundary_message",
    "detect_impersonation",
    "detect_injection",
    "detect_poisoned_delegation",
    "detect_poisoned_lesson",
    "scan_for_secrets",
]
