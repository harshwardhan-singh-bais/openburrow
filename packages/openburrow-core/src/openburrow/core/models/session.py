"""Sessions and lanes.

A **session** is one branch, one plan, many lanes, and one shared A2A thread.
It is the unit of coordination: everything else in OpenBurrow is scoped to a
session, and closing a session is what flushes the replay bundle.

A **lane** is one teammate's harness instance, exposed on the bus as exactly one
A2A peer. The important structural property: a lane is *not* an agent in the
abstract, it is a specific process running a specific harness in a specific git
worktree, with a specific Agent Card describing what it can do. That concreteness
is what makes the governance layer's accountability claims checkable.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

from pydantic import Field, computed_field, field_validator

from openburrow.core.models.base import BurrowModel, ensure_aware, now
from openburrow.core.models.enums import LaneRole, LaneStatus, SessionStatus


class Lane(BurrowModel):
    """One harness instance participating in a session, as one A2A peer."""

    id_kind: ClassVar[str] = "lane"

    session_id: str = ""
    name: str = ""
    harness: str = ""
    role: LaneRole = LaneRole.IMPLEMENTER
    status: LaneStatus = LaneStatus.STARTING

    # --- process identity --------------------------------------------------
    pid: int | None = None
    worktree_path: str = ""
    branch: str = ""
    base_commit: str = ""
    head_commit: str = ""
    command: list[str] = Field(default_factory=list)
    #: Env var *names* passed through to this lane. Values are never persisted.
    env_passthrough: list[str] = Field(default_factory=list)

    # --- A2A identity ------------------------------------------------------
    #: This lane's Agent Card URL. Every lane is a standard A2A peer from the outside.
    agent_card_url: str = ""
    a2a_endpoint: str = ""
    #: Skills this lane declared in its Agent Card.
    declared_skills: list[str] = Field(default_factory=list)
    #: Skills actually observed in behavior — compared against declared (item 186).
    observed_skills: list[str] = Field(default_factory=list)

    # --- ownership / governance -------------------------------------------
    #: Which human owns this lane. The accountability ledger keys off this.
    owner: str = ""
    #: Trust boundary classification relative to the session owner.
    trust_boundary: str = "intra_repo"
    can_delegate: bool = True
    transferable: bool = True
    #: Authority scope granted to this lane, as a set of capability strings.
    authority_scope: list[str] = Field(default_factory=list)

    # --- resource ceilings -------------------------------------------------
    max_runtime_s: int = 0
    idle_timeout_s: int = 900
    started_at: datetime | None = None
    stopped_at: datetime | None = None

    # --- usage -------------------------------------------------------------
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    restarts: int = 0
    messages_sent: int = 0
    messages_received: int = 0
    last_heartbeat: datetime | None = None

    @field_validator("owner", "name", "harness", mode="before")
    @classmethod
    def _required_strings(cls, value: object) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("field must not be empty")
        return text

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_alive(self) -> bool:
        return self.status not in {LaneStatus.STOPPED, LaneStatus.CRASHED}

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_blocking(self) -> bool:
        """True when this lane is waiting on a human or another lane."""
        return self.status in {
            LaneStatus.WAITING_INPUT,
            LaneStatus.WAITING_AUTH,
            LaneStatus.BLOCKED,
        }

    def heartbeat(self) -> None:
        self.last_heartbeat = now()

    @property
    def is_stale(self) -> bool:
        """No heartbeat within the idle window — candidate for orphan requeue."""
        if self.last_heartbeat is None:
            return False
        elapsed = (now() - ensure_aware(self.last_heartbeat)).total_seconds()
        return elapsed > self.idle_timeout_s

    @property
    def worktree(self) -> Path:
        return Path(self.worktree_path) if self.worktree_path else Path()

    @property
    def runtime_seconds(self) -> float:
        if self.started_at is None:
            return 0.0
        end = ensure_aware(self.stopped_at) if self.stopped_at else now()
        return (end - ensure_aware(self.started_at)).total_seconds()

    @property
    def budget_exceeded(self) -> bool:
        return bool(self.max_runtime_s) and self.runtime_seconds > self.max_runtime_s

    def record_usage(
        self,
        *,
        tokens_in: int = 0,
        tokens_out: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        """Accumulate reported usage. Harnesses that report nothing leave these at 0."""
        self.tokens_in += max(0, tokens_in)
        self.tokens_out += max(0, tokens_out)
        self.cost_usd = round(self.cost_usd + max(0.0, cost_usd), 6)
        self.touch()


class Session(BurrowModel):
    """One coordinated unit of work: a branch, a plan, many lanes, one A2A thread."""

    id_kind: ClassVar[str] = "session"

    name: str = ""
    description: str = ""
    owner: str = ""
    status: SessionStatus = SessionStatus.CREATED

    # --- git ---------------------------------------------------------------
    repo_root: str = ""
    branch: str = ""
    base_branch: str = "main"
    base_commit: str = ""
    head_commit: str = ""
    #: Rare multi-branch case (roadmap item 82).
    extra_branches: list[str] = Field(default_factory=list)

    # --- membership --------------------------------------------------------
    lanes: list[str] = Field(default_factory=list)
    #: Humans who can read the thread and inject messages as peers.
    participants: list[str] = Field(default_factory=list)

    # --- timing ------------------------------------------------------------
    started_at: datetime | None = None
    ended_at: datetime | None = None

    # --- knowledge ---------------------------------------------------------
    plan_id: str = ""
    #: The session-wide A2A thread id that every lane's messages land in.
    thread_id: str = ""
    tags: list[str] = Field(default_factory=list)

    # --- rollup ------------------------------------------------------------
    total_cost_usd: float = 0.0
    total_tokens: int = 0
    negotiations_run: int = 0
    collisions_avoided: int = 0
    governance_flags: int = 0
    approvals_requested: int = 0

    @field_validator("name", mode="before")
    @classmethod
    def _default_name(cls, value: object) -> str:
        return str(value or "").strip() or "unnamed-session"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_open(self) -> bool:
        return self.status in {SessionStatus.CREATED, SessionStatus.ACTIVE, SessionStatus.PAUSED}

    @computed_field  # type: ignore[prop-decorator]
    @property
    def lane_count(self) -> int:
        return len(self.lanes)

    @property
    def duration_seconds(self) -> float:
        if self.started_at is None:
            return 0.0
        end = ensure_aware(self.ended_at) if self.ended_at else now()
        return (end - ensure_aware(self.started_at)).total_seconds()

    def attach_lane(self, lane: Lane) -> None:
        if lane.id not in self.lanes:
            self.lanes.append(lane.id)
        lane.session_id = self.id
        self.touch()

    def detach_lane(self, lane_id: str) -> None:
        if lane_id in self.lanes:
            self.lanes.remove(lane_id)
        self.touch()

    def close(self, status: SessionStatus = SessionStatus.COMPLETED) -> None:
        self.status = status
        self.ended_at = now()
        self.touch()

    def summary(self) -> dict[str, Any]:
        """Compact rollup used by `burrow session list` and the web dashboard."""
        return {
            "id": self.id,
            "name": self.name,
            "status": str(self.status),
            "branch": self.branch,
            "lanes": self.lane_count,
            "duration_s": round(self.duration_seconds, 1),
            "cost_usd": round(self.total_cost_usd, 4),
            "negotiations": self.negotiations_run,
            "collisions_avoided": self.collisions_avoided,
            "governance_flags": self.governance_flags,
        }


__all__ = ["Lane", "Session"]
