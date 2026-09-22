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
import hmac
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

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

#: The SSHSIG namespace this relay signs under. A client must pass the same
#: value to ``ssh-keygen -Y sign -n <namespace>``. It is checked before any
#: signature is verified, which is what stops a signature harvested from some
#: other service from being replayed here.
SSH_SIGNATURE_NAMESPACE = "openburrow-relay"

#: First bytes of an SSHSIG blob. The signed payload repeats it without a
#: length prefix, so it is written twice in two different encodings.
_SSHSIG_MAGIC = b"SSHSIG"

#: SSHSIG format version. Only 1 is defined.
_SSHSIG_VERSION = 1

#: Bytes in an SSHSIG field length prefix.
_SSH_LENGTH_BYTES = 4


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


# --------------------------------------------------------------------------
# SSH-key identity (item 243)
# --------------------------------------------------------------------------
#: Challenge TTL. Short because the challenge is only proof-of-possession
#: material; a leaked one is worthless after expiry.
SSH_CHALLENGE_TTL_S = 120


def new_ssh_challenge(*, nonce_bytes: int = 32, secret: str | None = None) -> tuple[str, float]:
    """A challenge the client must sign with its SSH key.

    Returns the challenge string and its expiry timestamp. The challenge embeds
    the relay's issuer name so a challenge captured from a *different* service
    cannot be replayed here.

    When ``secret`` is given (the relay's JWT secret — reusing it avoids a second
    configured key), the challenge carries an expiry and an HMAC over them, so
    challenge verification needs no server-side state: a challenge not minted by
    this relay, or minted more than TTL ago, fails before the signature is even
    checked. Without a secret the challenge is only bound by TTL and the caller
    must store it — the shape tests and offline tools use.
    """
    nonce = secrets.token_hex(nonce_bytes)
    expires = time.time() + SSH_CHALLENGE_TTL_S
    if secret is None:
        return f"openburrow-relay auth {nonce}", expires
    mac = _challenge_mac(nonce, expires, secret)
    return f"openburrow-relay auth {nonce} exp={int(expires)} mac={mac}", expires


def _challenge_mac(nonce: str, expires: float, secret: str) -> str:
    material = f"{nonce}|{int(expires)}".encode()
    return hmac.new(secret.encode(), material, hashlib.sha256).hexdigest()[:32]


def verify_ssh_challenge(challenge: str, *, secret: str | None = None) -> None:
    """Raise :class:`AuthError` if a presented challenge is not ours or is stale.

    Called before signature verification: an unbound challenge from an attacker
    must not become a valid proof merely because the attacker signs it too.
    """
    if not challenge.startswith("openburrow-relay auth "):
        raise AuthError(
            "challenge was not issued by this relay",
            hint="Request a fresh one from POST /auth/ssh/challenge.",
        )
    if secret is None:
        return
    parts = challenge.split()
    fields = dict(p.split("=", 1) for p in parts[3:] if "=" in p)
    # parts[0] is the service name, [1] the literal "auth", [2] the nonce —
    # the exp=/mac= fields follow. Getting the nonce index wrong would fail
    # closed, but it would fail every legitimate client too.
    nonce = parts[2] if len(parts) > 2 and "=" not in parts[2] else ""
    try:
        expires = float(fields["exp"])
        mac = fields["mac"]
    except KeyError as exc:
        raise AuthError(
            "challenge is not integrity-protected",
            hint="Challenges must come from POST /auth/ssh/challenge on this relay.",
        ) from exc
    if not hmac.compare_digest(mac, _challenge_mac(nonce, expires, secret)):
        raise AuthError(
            "challenge MAC does not match",
            hint="The challenge was modified or came from a different relay.",
        )
    if time.time() > expires + LEEWAY_S:
        raise AuthError(
            "challenge has expired",
            hint="Request a fresh challenge; challenges live for two minutes.",
        )


def _ssh_string(value: bytes) -> bytes:
    """An SSH wire string: a big-endian length, then the bytes."""
    return len(value).to_bytes(_SSH_LENGTH_BYTES, "big") + value


def _ssh_take(raw: bytes, at: int) -> tuple[bytes, int] | None:
    """Read one SSH wire string at ``at``. Returns it and the next offset.

    ``None`` means the buffer is truncated or the length is absurd, which is the
    only thing a length-prefixed format can be wrong about. Every parse below
    threads the offset through this so a short input fails here rather than
    slicing past the end.
    """
    if at + _SSH_LENGTH_BYTES > len(raw):
        return None
    size = int.from_bytes(raw[at : at + _SSH_LENGTH_BYTES], "big")
    start = at + _SSH_LENGTH_BYTES
    end = start + size
    if end > len(raw):
        return None
    return raw[start:end], end


def _ssh_wire_blob(public_key: str) -> bytes | None:
    """The decoded key blob from an OpenSSH public-key line, or ``None``.

    This blob — not the key's raw bytes — is what ``ssh-keygen -l`` hashes, so
    it is what a ``ssh:SHA256:`` identity has to be built from. Deriving it this
    way also works for every key type, where ``public_bytes_raw()`` works only
    for Ed25519 and Ed448.
    """
    import base64

    parts = public_key.split()
    if len(parts) < 2:
        return None
    try:
        blob = base64.b64decode(parts[1], validate=True)
    except Exception:
        return None
    # The blob's first field is the key type. Checking it against the token on
    # the line is cheap and catches a line that is not a public key at all,
    # which would otherwise fingerprint happily and mint a real identity for it.
    first = _ssh_take(blob, 0)
    if first is None:
        return None
    key_type, _ = first
    if key_type != parts[0].encode("utf-8"):
        return None
    return blob


def ssh_fingerprint(public_key: str) -> str:
    """A stable identity for an SSH public key: ``ssh:SHA256:<b64 digest>``.

    This is the subject a room owner grants membership to. It is derived, not
    declared, so a member cannot log in as someone else by typing their name.

    The digest is over the key's SSH wire blob — the value ``ssh-keygen -l``
    prints — so an operator can check a fingerprint against the tool they
    already trust. An earlier version hashed ``public_bytes_raw()`` instead,
    which is Ed25519-only (so RSA keys fingerprinted as ``""``) and, for
    Ed25519, produced a digest no other SSH tool would agree with. Changing it
    is a breaking change to every existing subject, and was taken deliberately:
    the old value was not the thing its ``ssh:`` prefix claimed to be.
    """
    import base64
    import hashlib

    blob = _ssh_wire_blob(public_key)
    if blob is None:
        return ""
    digest = hashlib.sha256(blob).digest()
    return f"ssh:SHA256:{base64.b64encode(digest).rstrip(b'=').decode('ascii')}"


@dataclass(frozen=True, slots=True)
class _SshSignature:
    """A parsed SSHSIG envelope.

    Only the fields verification needs. ``namespace`` is kept rather than
    checked here because which namespace is acceptable is a policy decision,
    not a parsing detail.
    """

    key_blob: bytes
    namespace: str
    message_hash: str
    signature_format: str
    signature: bytes


class _MalformedSshsigError(Exception):
    """Internal: the input does not match OpenSSH's ``PROTOCOL.sshsig``."""


def _sshsig_field(raw: bytes, at: int) -> tuple[bytes, int]:
    """Read one field, raising ``_MalformedSshsigError`` if the buffer is short."""
    taken = _ssh_take(raw, at)
    if taken is None:
        raise _MalformedSshsigError
    return taken


def _parse_sshsig(armored: str) -> _SshSignature | None:
    """Parse the armored SSHSIG envelope ``ssh-keygen -Y sign`` writes.

    The layout is fixed by OpenSSH's ``PROTOCOL.sshsig``. Anything that does not
    match is ``None``, including a trailing byte — a format this rigid has no
    business accepting slack.
    """
    try:
        return _read_sshsig(armored)
    except _MalformedSshsigError:
        return None


def _read_sshsig(armored: str) -> _SshSignature:
    """``_parse_sshsig`` with the error handling taken out.

    Every failure below is the same failure, so they raise and the caller
    converts once. Threading ``None`` through eleven separate returns instead
    made the shape of the format harder to read than the format itself.
    """
    import base64

    body = "".join(
        line.strip() for line in armored.strip().splitlines() if not line.startswith("-----")
    )
    try:
        raw = base64.b64decode(body, validate=True)
    except Exception as exc:
        raise _MalformedSshsigError from exc
    if not raw.startswith(_SSHSIG_MAGIC):
        raise _MalformedSshsigError
    offset = len(_SSHSIG_MAGIC)
    if offset + _SSH_LENGTH_BYTES > len(raw):
        raise _MalformedSshsigError
    if int.from_bytes(raw[offset : offset + _SSH_LENGTH_BYTES], "big") != _SSHSIG_VERSION:
        raise _MalformedSshsigError
    offset += _SSH_LENGTH_BYTES

    fields: list[bytes] = []
    for _ in range(5):  # key blob, namespace, reserved, message hash, signature
        value, offset = _sshsig_field(raw, offset)
        fields.append(value)
    if offset != len(raw):
        raise _MalformedSshsigError
    key_blob, namespace, _reserved, message_hash, signature_blob = fields

    # The signature field is itself an SSH string pair: the algorithm name, then
    # the raw signature. Unwrapping it is not optional — those bytes are not a
    # signature on their own, and the algorithm name is what tells RSA's SHA-512
    # variant from its SHA-256 one.
    algorithm, offset = _sshsig_field(signature_blob, 0)
    signature, offset = _sshsig_field(signature_blob, offset)
    if offset != len(signature_blob):
        raise _MalformedSshsigError
    try:
        return _SshSignature(
            key_blob=key_blob,
            namespace=namespace.decode("utf-8"),
            message_hash=message_hash.decode("utf-8"),
            signature_format=algorithm.decode("utf-8"),
            signature=signature,
        )
    except UnicodeDecodeError as exc:
        raise _MalformedSshsigError from exc


def _der_integer(value: bytes) -> bytes:
    """One DER INTEGER, minimal and sign-correct."""
    value = value.lstrip(b"\x00") or b"\x00"
    if value[0] & 0x80:
        value = b"\x00" + value
    return b"\x02" + bytes([len(value)]) + value


def _ecdsa_ssh_to_der(raw: bytes) -> bytes | None:
    """Convert an SSH ECDSA signature to the DER form ``cryptography`` wants.

    SSH carries ECDSA as two bare mpints; every other consumer of the format
    expects a DER ``SEQUENCE`` of two ``INTEGER``s. Handing the SSH bytes to
    ``verify`` raises rather than returning False, which is why this conversion
    is load-bearing rather than cosmetic.
    """
    first = _ssh_take(raw, 0)
    if first is None:
        return None
    r, offset = first
    second = _ssh_take(raw, offset)
    if second is None:
        return None
    s, offset = second
    if offset != len(raw):
        return None
    body = _der_integer(r) + _der_integer(s)
    return b"\x30" + bytes([len(body)]) + body


def _verify_with_key(key: object, signature: bytes, data: bytes, algorithm: str) -> bool:
    """Verify one signature under the scheme its key type defines.

    ``signature`` is in the canonical form for the key type: raw bytes for
    Ed25519, PKCS#1 v1.5 for RSA, DER for ECDSA, raw for DSA.

    One scheme per key type, stated rather than discovered. An earlier version
    tried a bare ``verify`` and fell back to PSS on any exception, which meant a
    bad Ed25519 signature and an RSA key took the same path, and the fallback
    could not run at all: ``PSS`` takes ``mgf`` and ``salt_length``, has no
    ``algorithm`` parameter, and ``PSS.SALT_LENGTH_DIGEST`` does not exist.
    """
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import dsa, ec, ed25519, padding, rsa

    try:
        if isinstance(key, ed25519.Ed25519PublicKey):
            key.verify(signature, data)
        elif isinstance(key, rsa.RSAPublicKey):
            digest = hashes.SHA512() if algorithm.endswith("512") else hashes.SHA256()
            key.verify(signature, data, padding.PKCS1v15(), digest)
        elif isinstance(key, ec.EllipticCurvePublicKey):
            # DER by the time it gets here. The SSHSIG branch converts from
            # SSH's mpint form before calling; a raw signature from a language
            # binding is already DER. Converting inside this helper would mean
            # guessing which of the two arrived.
            key.verify(signature, data, ec.ECDSA(hashes.SHA256()))
        elif isinstance(key, dsa.DSAPublicKey):
            key.verify(signature, data, hashes.SHA256())
        else:
            # A key type with no scheme here. False is the honest answer: this
            # is an auth check, and "cannot verify" must not read as "verified".
            return False
    except InvalidSignature:
        return False
    return True


def _verify_raw(key: object, signature_b64: str, message: bytes) -> bool:
    """Verify a bare base64 signature over the message.

    This is the path a client using a language binding takes, where the
    signature is the raw output of ``sign()`` rather than an SSHSIG envelope.
    """
    import base64

    try:
        signature = base64.b64decode(signature_b64, validate=True)
    except Exception:
        return False
    return _verify_with_key(key, signature, message, "")


def _verify_envelope(key: object, envelope: _SshSignature, message: bytes, public_key: str) -> bool:
    """Verify an SSHSIG envelope against the key the caller presented."""
    import hashlib

    from cryptography.hazmat.primitives.asymmetric import ec

    if envelope.namespace != SSH_SIGNATURE_NAMESPACE:
        return False
    # The envelope carries its own copy of the key. It must be the key being
    # claimed, or a member could sign with their own key and present someone
    # else's public key alongside it.
    if envelope.key_blob != _ssh_wire_blob(public_key):
        return False
    try:
        digest = hashlib.new(envelope.message_hash, message).digest()
    except ValueError:
        return False
    # The signed payload is not the message: SSHSIG signs a framing of the
    # namespace, the reserved field, the hash name, and the message digest.
    data = (
        _SSHSIG_MAGIC
        + _ssh_string(envelope.namespace.encode("utf-8"))
        + _ssh_string(b"")
        + _ssh_string(envelope.message_hash.encode("utf-8"))
        + _ssh_string(digest)
    )
    signature = envelope.signature
    if isinstance(key, ec.EllipticCurvePublicKey):
        converted = _ecdsa_ssh_to_der(signature)
        if converted is None:
            return False
        signature = converted
    return _verify_with_key(key, signature, data, envelope.signature_format)


def verify_ssh_signature(public_key: str, challenge: str, signature_b64: str) -> bool:
    """Verify an SSH signature over the challenge (item 243).

    Accepts the two encodings a client can realistically produce:

    * the armored SSHSIG envelope from
      ``ssh-keygen -Y sign -n openburrow-relay <file>``, which is the documented
      client flow and the same mechanism git uses for commit verification; and
    * a bare base64 raw signature over the challenge bytes, for a client using a
      language binding instead of the CLI.

    They are told apart by the armor, never by trying one and falling back to the
    other. A fallback chain would let a signature that fails under the intended
    scheme succeed under a different one, which is algorithm confusion in the one
    function where it matters most.

    ``public_key`` is the OpenSSH one-line public key (``ssh-ed25519 AAAA...``).
    Returns False on any malformed input: a bad signature is an auth failure, not
    an error.
    """
    try:
        from cryptography.hazmat.primitives.serialization import load_ssh_public_key
    except ImportError:
        # Declared transitively via python-jose's dependency chain; if it is
        # genuinely missing, SSH auth is unavailable and False is honest.
        return False

    if not public_key or not challenge or not signature_b64:
        return False
    try:
        key = load_ssh_public_key(public_key.encode("utf-8"))
    except Exception:
        return False

    message = challenge.encode("utf-8")
    envelope = _parse_sshsig(signature_b64)
    if envelope is None:
        return _verify_raw(key, signature_b64, message)
    return _verify_envelope(key, envelope, message, public_key)


def provenance_extension(claims: TokenClaims) -> dict[str, Any]:
    """The token fields the Stage 14 ledger can chain on.

    A relay-issued token asserts *who the relay authenticated*, not just which
    room it may join: the subject, the role the relay assigned, and the token's
    identity (jti). The daemon's impersonation detector treats a relayed event
    whose sender subject does not match the provenance of the connection that
    carried it as a violation — the tie-in this function enables.
    """
    return {
        "provenance": {
            "subject": claims.subject,
            "role": str(claims.role),
            "jti": claims.jti,
            "issued_at": claims.issued_at,
            "issuer": "openburrow-relay",
        }
    }


__all__ = [
    "ALGORITHM",
    "INVITE_BYTES",
    "LEEWAY_S",
    "SSH_CHALLENGE_TTL_S",
    "TokenClaims",
    "check_invite_usable",
    "expires_at",
    "extract_bearer",
    "hash_invite",
    "invite_lookup_hash",
    "issue_token",
    "new_invite_token",
    "new_ssh_challenge",
    "provenance_extension",
    "ssh_fingerprint",
    "verify_ssh_challenge",
    "verify_ssh_signature",
    "verify_token",
]
