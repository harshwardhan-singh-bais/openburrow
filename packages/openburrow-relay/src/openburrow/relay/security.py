"""Tokens, invites, and the one place that decides who someone is.

Two credential types, with deliberately different lifetimes and storage:

**Invite tokens** are long-lived, high-entropy, shown once, and stored only as a
SHA-256 hash. They are the bootstrap: you have one because a human handed it to
you out of band.

**JWTs** are short-lived, derived from an invite, and never stored. They carry
the room and the role so the WebSocket handshake does not need a database round
trip on every connection — which matters because a browser reconnecting after a
network blip should not be a database event.

HS256 rather than RS256 because there is one service and one secret. RS256's
value is that the verifier does not need the signing key, which only matters
once there is a second service verifying tokens. Adding a keypair before that is
inventory for a problem we do not have.

The algorithm is pinned explicitly at decode time. Accepting the algorithm named
in the token header is the classic JWT vulnerability — an attacker sets
``alg: none`` or swaps HS256 for RS256 and signs with the public key — and the
only defence that works is refusing to read the header at all.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from jose import ExpiredSignatureError, JWTError, jwt

from openburrow.core.logging import get_logger
from openburrow.relay.config import RelaySettings
from openburrow.relay.errors import AuthError, InviteInvalidError, TokenExpiredError
from openburrow.relay.models import RoomRole

log = get_logger(__name__)

ALGORITHM = "HS256"

#: Bytes of entropy in an invite token. ``token_urlsafe(32)`` yields 43 chars.
INVITE_BYTES = 32

#: Clock skew allowance, in seconds, applied to ``iat`` and ``exp``. Machines in
#: a relay deployment are not perfectly synchronised, and a token that is valid
#: but rejected because a VM's clock drifted two seconds is an outage with a
#: confusing cause.
LEEWAY_S = 30

#: A well-formed bearer header splits into exactly two whitespace-separated parts.
_BEARER_PARTS = 2


@dataclass(frozen=True, slots=True)
class TokenClaims:
    """What a verified token asserts."""

    room: str
    member: str
    subject: str
    role: RoomRole
    issued_at: int
    expires_at: int
    jti: str

    @property
    def ttl_remaining(self) -> int:
        return max(0, self.expires_at - int(time.time()))

    def to_dict(self) -> dict[str, object]:
        return {
            "room": self.room,
            "member": self.member,
            "subject": self.subject,
            "role": str(self.role),
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "ttl_remaining_s": self.ttl_remaining,
        }


# --------------------------------------------------------------------------
# Invites
# --------------------------------------------------------------------------
def new_invite_token() -> str:
    """A fresh invite token. URL-safe so it survives being pasted into a link."""
    return secrets.token_urlsafe(INVITE_BYTES)


def hash_invite(token: str) -> str:
    """The stored form of an invite.

    SHA-256 with no salt, and that is correct here rather than lazy: the input
    has 256 bits of entropy, so there is no dictionary to attack and no rainbow
    table to build. Salting would only prevent the same token hashing to the same
    value twice, which is information the database already has in the row id.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def invite_lookup_hash(token: str) -> str:
    """Constant-time-safe lookup key.

    Identical to :func:`hash_invite` today. It exists as a separate name so that
    if the storage scheme ever changes, the *lookup* path is a deliberate
    decision rather than a coincidence — a hash you look rows up by and a hash
    you verify against have different requirements.
    """
    return hash_invite(token)


def expires_at(*, days: int = 7) -> datetime:
    """When an invite created now would expire."""
    return datetime.now(UTC) + timedelta(days=days)


# --------------------------------------------------------------------------
# JWTs
# --------------------------------------------------------------------------
def issue_token(
    settings: RelaySettings,
    *,
    room: str,
    member: str,
    subject: str,
    role: RoomRole,
    ttl_s: int | None = None,
) -> tuple[str, TokenClaims]:
    """Mint a room token. Returns the encoded JWT and the claims it carries."""
    now = int(time.time())
    ttl = settings.token_ttl_s if ttl_s is None else ttl_s
    claims = TokenClaims(
        room=room,
        member=member,
        subject=subject,
        role=role,
        issued_at=now,
        expires_at=now + ttl,
        jti=secrets.token_hex(8),
    )
    payload = {
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "sub": subject,
        "room": room,
        "member": member,
        "role": str(role),
        "iat": claims.issued_at,
        "nbf": claims.issued_at,
        "exp": claims.expires_at,
        "jti": claims.jti,
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=ALGORITHM)
    return token, claims


def verify_token(
    settings: RelaySettings,
    token: str,
    *,
    expected_room: str | None = None,
) -> TokenClaims:
    """Verify a token and return its claims.

    ``expected_room`` is checked *after* signature verification, never instead
    of it. Passing the room into ``decode`` as a claim to match would be
    equivalent, but doing it here keeps the comparison visible and makes it
    obvious that a token for room A cannot be replayed against room B.
    """
    if not token:
        raise AuthError(
            "no token supplied",
            hint="Pass the room token as the `token` query parameter or in an `Authorization: Bearer` header.",
        )

    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[ALGORITHM],  # pinned: never read `alg` from the header
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            options={"leeway": LEEWAY_S},
        )
    except ExpiredSignatureError as exc:
        raise TokenExpiredError(
            "room token has expired",
            hint="Redeem your invite again, or ask a room owner for a fresh one.",
        ) from exc
    except JWTError as exc:
        raise AuthError(
            "room token is not valid",
            hint="The token is malformed, signed with a different secret, or issued for another relay.",
            cause=exc,
        ) from exc

    room = str(payload.get("room") or "")
    member = str(payload.get("member") or "")
    subject = str(payload.get("sub") or "")
    raw_role = str(payload.get("role") or "")

    if not room or not member:
        raise AuthError(
            "room token is missing its room or member claim",
            hint="This token was not issued by the relay. Re-redeem your invite.",
            context={"room": bool(room), "member": bool(member)},
        )

    if expected_room is not None and room != expected_room:
        raise AuthError(
            "room token is for a different room",
            hint=f"This token is scoped to {room!r}. Request a token for {expected_room!r}.",
            context={"token_room": room, "requested_room": expected_room},
        )

    try:
        role = RoomRole(raw_role)
    except ValueError as exc:
        raise AuthError(
            f"room token carries an unknown role {raw_role!r}",
            context={"role": raw_role},
            cause=exc,
        ) from exc

    return TokenClaims(
        room=room,
        member=member,
        subject=subject,
        role=role,
        issued_at=int(payload.get("iat") or 0),
        expires_at=int(payload.get("exp") or 0),
        jti=str(payload.get("jti") or ""),
    )


def extract_bearer(header: str | None) -> str | None:
    """Pull the token out of an ``Authorization`` header.

    Returns ``None`` for anything that is not a well-formed bearer header rather
    than raising, so the caller can fall through to the query parameter without
    an exception in the middle of a handshake.
    """
    if not header:
        return None
    parts = header.split(None, 1)
    if len(parts) != _BEARER_PARTS or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def check_invite_usable(
    *,
    revoked: bool,
    uses: int,
    max_uses: int,
    expires: datetime,
) -> None:
    """Raise if an invite row is not redeemable.

    Separated from the store so the rule is testable without a database, and so
    there is exactly one definition of "usable" rather than one in the query and
    another in the handler.
    """
    now = datetime.now(UTC)
    if revoked:
        raise InviteInvalidError(
            "this invite has been revoked",
            hint="Ask a room owner for a new one.",
        )
    if expires.tzinfo is None:
        # A naive timestamp from the database would otherwise compare against an
        # aware `now` and raise TypeError — a 500 for what is really a 401.
        expires = expires.replace(tzinfo=UTC)
    if expires <= now:
        raise InviteInvalidError(
            "this invite has expired",
            hint="Ask a room owner for a new one.",
            context={"expired_at": expires.isoformat()},
        )
    if uses >= max_uses:
        raise InviteInvalidError(
            "this invite has already been used",
            hint="Invites are single-use unless created with --uses. Ask for a new one.",
            context={"uses": uses, "max_uses": max_uses},
        )


__all__ = [
    "ALGORITHM",
    "INVITE_BYTES",
    "LEEWAY_S",
    "TokenClaims",
    "check_invite_usable",
    "expires_at",
    "extract_bearer",
    "hash_invite",
    "invite_lookup_hash",
    "issue_token",
    "new_invite_token",
    "verify_token",
]
