"""Claims — advisory ownership of a file, directory, step, or resource.

Two design decisions worth stating plainly:

1. **First claim wins.** There is no voting, no quorum, no lease auction. The
   first lane to post a claim owns the resource until it releases it, expires,
   or is revoked by a human. Determinism beats fairness here, because an
   ambiguous owner is worse than a slightly suboptimal one.

2. **Claims are advisory, not locks.** A claim does not prevent a file write —
   it makes the intent visible on the bus so the *other* lane can react. The
   enforcement mechanism is the negotiation, not the filesystem. This is
   deliberate: hard locks across heterogeneous harnesses would deadlock the
   moment one harness crashed, which is exactly the failure mode the durability
   stage exists to avoid.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from fnmatch import fnmatch
from typing import ClassVar

from pydantic import Field, computed_field

from openburrow.core.models.base import BurrowModel, ensure_aware, now
from openburrow.core.models.enums import ClaimKind, ClaimStatus


class Claim(BurrowModel):
    """A lane's stated intent to own a resource for a while."""

    id_kind: ClassVar[str] = "claim"

    session_id: str = ""
    lane_id: str = ""
    #: Human-readable owner, denormalised so audit exports do not need a join.
    owner: str = ""

    kind: ClaimKind = ClaimKind.FILE
    #: Repo-relative path or symbolic step/resource name.
    resource: str = ""
    #: Extra glob patterns covered by this claim (e.g. a whole directory).
    patterns: list[str] = Field(default_factory=list)

    status: ClaimStatus = ClaimStatus.ACTIVE
    #: Why the lane wants it. Surfaced on rejection so the other lane can judge
    #: whether to negotiate rather than just back off (item 90).
    intent: str = ""
    step_id: str = ""

    expires_at: datetime | None = None
    released_at: datetime | None = None
    released_reason: str = ""
    #: Set when the claim was granted by force rather than by first-come.
    forced: bool = False
    revoked_by: str = ""

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_active(self) -> bool:
        return self.status == ClaimStatus.ACTIVE and not self.is_expired

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and now() > ensure_aware(self.expires_at)

    @property
    def ttl_seconds(self) -> float | None:
        if self.expires_at is None:
            return None
        return (ensure_aware(self.expires_at) - now()).total_seconds()

    def covers(self, path: str) -> bool:
        """Does this claim cover ``path``?

        Exact match, then glob patterns, then directory prefix. The prefix check
        is what makes a claim on ``src/api/`` cover ``src/api/routes.py`` without
        the claiming lane having to enumerate every file.
        """
        if not path:
            return False
        if self.resource == path:
            return True
        for pattern in self.patterns:
            if fnmatch(path, pattern):
                return True
        if self.kind in {ClaimKind.DIRECTORY, ClaimKind.RESOURCE}:
            prefix = self.resource.rstrip("/") + "/"
            return path.startswith(prefix)
        return fnmatch(path, self.resource)

    def overlaps(self, other: Claim) -> bool:
        """Do two claims touch the same resource?

        Used by the claim service to decide whether a new claim is a conflict or
        merely adjacent. A directory claim overlapping a file claim *is* a
        conflict — that asymmetry is the whole reason this method exists instead
        of a string comparison.
        """
        if self.kind == other.kind == ClaimKind.STEP:
            return self.resource == other.resource
        if self.covers(other.resource) or other.covers(self.resource):
            return True
        for pattern in [*self.patterns, self.resource]:
            for other_pattern in [*other.patterns, other.resource]:
                if pattern == other_pattern:
                    return True
        return False

    def release(self, *, reason: str = "released") -> None:
        self.status = ClaimStatus.RELEASED
        self.released_at = now()
        self.released_reason = reason
        self.touch()

    def expire(self) -> None:
        self.status = ClaimStatus.EXPIRED
        self.released_at = now()
        self.released_reason = "expired"
        self.touch()

    def revoke(self, *, by: str, reason: str = "revoked") -> None:
        self.status = ClaimStatus.REVOKED
        self.revoked_by = by
        self.released_at = now()
        self.released_reason = reason
        self.touch()

    def extend(self, *, seconds: int) -> None:
        base = ensure_aware(self.expires_at) if self.expires_at else now()
        self.expires_at = base + timedelta(seconds=seconds)
        self.touch()

    @classmethod
    def for_file(
        cls,
        *,
        session_id: str,
        lane_id: str,
        owner: str,
        path: str,
        intent: str = "",
        ttl_seconds: int = 1800,
    ) -> Claim:
        return cls(
            session_id=session_id,
            lane_id=lane_id,
            owner=owner,
            kind=ClaimKind.FILE,
            resource=path,
            intent=intent,
            expires_at=now() + timedelta(seconds=ttl_seconds),
        )

    @classmethod
    def for_step(
        cls,
        *,
        session_id: str,
        lane_id: str,
        owner: str,
        step_id: str,
        intent: str = "",
    ) -> Claim:
        """Step claims have no TTL — a step stays owned until handed off."""
        return cls(
            session_id=session_id,
            lane_id=lane_id,
            owner=owner,
            kind=ClaimKind.STEP,
            resource=step_id,
            step_id=step_id,
            intent=intent,
        )

    def __str__(self) -> str:
        return f"{self.kind}:{self.resource} by {self.lane_id} ({self.status})"


__all__ = ["Claim"]
