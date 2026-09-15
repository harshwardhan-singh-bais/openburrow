"""Human-in-the-loop approvals.

An approval is a *blocking* event: the lane's A2A task moves to
``auth_required`` and stays there. That mapping is not cosmetic — it means an
approval shows up in the same state machine as everything else, so the TUI, the
replay viewer, and the audit ledger all render it without special cases.

The design choice worth defending: **any authorized teammate may respond, not
just the task owner.** A blocked lane waiting on someone who went to lunch is a
worse failure than an extra person being able to unblock it, and the audit
record captures who actually answered either way.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, ClassVar

from pydantic import Field, computed_field

from openburrow.core.models.base import BurrowModel, ensure_aware, now
from openburrow.core.models.enums import ApprovalStatus


class ApprovalRequest(BurrowModel):
    """A risky action paused pending a human decision."""

    id_kind: ClassVar[str] = "approval"

    session_id: str = ""
    lane_id: str = ""
    task_id: str = ""
    step_id: str = ""

    # --- what needs approving ------------------------------------------------
    #: The command or action, verbatim. Never paraphrased — a human is being
    #: asked to approve a specific string, so the specific string is what they see.
    action: str = ""
    #: Normalised category: exec, write, network, git-push, secret-access …
    action_kind: str = "exec"
    #: Risk tier from the repo's risk-tier config.
    risk_tier: str = "medium"
    #: Why the detector flagged it.
    reason: str = ""
    #: Repo-relative paths the action would touch.
    affected_paths: list[str] = Field(default_factory=list)
    #: Diff or command output shown to the approver for context.
    preview: str = ""

    # --- who can answer ------------------------------------------------------
    #: Lanes authorized to respond. Empty + ``any_teammate`` means the session.
    requested_from: list[str] = Field(default_factory=list)
    any_teammate: bool = True
    #: Human who initiated the lane, always eligible.
    task_owner: str = ""

    # --- outcome -------------------------------------------------------------
    status: ApprovalStatus = ApprovalStatus.PENDING
    responded_by: str = ""
    responded_at: datetime | None = None
    #: When a human edits the action rather than approving it verbatim.
    edited_action: str = ""
    note: str = ""

    # --- timing --------------------------------------------------------------
    requested_at: datetime = Field(default_factory=now)
    expires_at: datetime | None = None
    on_timeout: str = "deny"  # deny | escalate | pause

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_pending(self) -> bool:
        return self.status == ApprovalStatus.PENDING

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and now() > ensure_aware(self.expires_at)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def effective_action(self) -> str:
        """What should actually run: the edited version if a human edited it."""
        return self.edited_action or self.action

    @computed_field  # type: ignore[prop-decorator]
    @property
    def was_edited(self) -> bool:
        return bool(self.edited_action) and self.edited_action != self.action

    @property
    def wait_seconds(self) -> float:
        end = ensure_aware(self.responded_at) if self.responded_at else now()
        return (end - ensure_aware(self.requested_at)).total_seconds()

    def approve(self, *, by: str, note: str = "", edited_action: str = "") -> None:
        self.status = ApprovalStatus.APPROVED_EDITED if edited_action else ApprovalStatus.APPROVED
        self.edited_action = edited_action
        self.responded_by = by
        self.responded_at = now()
        self.note = note
        self.touch()

    def deny(self, *, by: str, note: str = "") -> None:
        self.status = ApprovalStatus.DENIED
        self.responded_by = by
        self.responded_at = now()
        self.note = note
        self.touch()

    def time_out(self) -> None:
        self.status = ApprovalStatus.TIMED_OUT
        self.responded_at = now()
        self.note = f"no response within the configured window; policy was '{self.on_timeout}'"
        self.touch()

    def escalate(self, *, to: list[str], note: str = "") -> None:
        self.status = ApprovalStatus.ESCALATED
        self.requested_from = sorted({*self.requested_from, *to})
        if note:
            self.note = note
        self.touch()

    @classmethod
    def for_action(
        cls,
        *,
        session_id: str,
        lane_id: str,
        action: str,
        action_kind: str = "exec",
        risk_tier: str = "medium",
        reason: str = "",
        timeout_s: int = 900,
        on_timeout: str = "deny",
        task_owner: str = "",
        **extra: Any,
    ) -> ApprovalRequest:
        return cls(
            session_id=session_id,
            lane_id=lane_id,
            action=action,
            action_kind=action_kind,
            risk_tier=risk_tier,
            reason=reason,
            task_owner=task_owner,
            on_timeout=on_timeout,
            expires_at=now() + timedelta(seconds=timeout_s) if timeout_s else None,
            **extra,
        )

    def render(self) -> str:
        """Multi-line terminal rendering used by `burrow approvals list`."""
        lines = [
            f"Approval {self.id}  [{self.risk_tier.upper()} risk]  {self.status}",
            f"  lane:    {self.lane_id}",
            f"  action:  {self.effective_action}",
        ]
        if self.was_edited:
            lines.append(f"  (edited from: {self.action})")
        if self.reason:
            lines.append(f"  reason:  {self.reason}")
        if self.affected_paths:
            lines.append(f"  paths:   {', '.join(self.affected_paths[:6])}")
        if self.is_pending:
            ttl = self.expires_at
            if ttl is not None:
                remaining = max(0, int((ensure_aware(ttl) - now()).total_seconds()))
                lines.append(f"  expires: in {remaining}s (on timeout: {self.on_timeout})")
        elif self.responded_by:
            lines.append(f"  by:      {self.responded_by}")
        return "\n".join(lines)


__all__ = ["ApprovalRequest"]
