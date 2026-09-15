"""Token minting and verification.

The security-relevant assertions here are mostly negative: a token for room A
must not work in room B, a tampered token must be rejected, and the decoder must
refuse to take the algorithm from the token header. Each of those is a real
attack that has shipped in real systems, so each gets a test rather than a
comment.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from openburrow.relay.config import RelaySettings
from openburrow.relay.errors import AuthError, InviteInvalidError, TokenExpiredError
from openburrow.relay.models import RoomRole
from openburrow.relay.security import (
    ALGORITHM,
    check_invite_usable,
    extract_bearer,
    hash_invite,
    invite_lookup_hash,
    issue_token,
    new_invite_token,
    verify_token,
)

pytestmark = pytest.mark.unit

ROOM = "room_01HQ0000000000000000000000"
MEMBER = "mbr_01HQ0000000000000000000000"
SUBJECT = "ada@example.com"


def mint(settings: RelaySettings, **overrides: object) -> str:
    token, _ = issue_token(
        settings,
        room=str(overrides.get("room", ROOM)),
        member=str(overrides.get("member", MEMBER)),
        subject=str(overrides.get("subject", SUBJECT)),
        role=RoomRole(overrides.get("role", RoomRole.MAINTAINER)),  # type: ignore[arg-type]
        ttl_s=overrides.get("ttl_s"),  # type: ignore[arg-type]
    )
    return token


class TestRoundTrip:
    def test_claims_survive_the_round_trip(self, settings: RelaySettings) -> None:
        claims = verify_token(settings, mint(settings))
        assert claims.room == ROOM
        assert claims.member == MEMBER
        assert claims.subject == SUBJECT
        assert claims.role is RoomRole.MAINTAINER
        assert claims.jti  # every token is individually identifiable

    def test_two_tokens_for_the_same_member_differ(self, settings: RelaySettings) -> None:
        # Distinct jti values, so a token can be revoked individually later
        # without invalidating the member's other sessions.
        first = verify_token(settings, mint(settings))
        second = verify_token(settings, mint(settings))
        assert first.jti != second.jti

    def test_ttl_remaining_counts_down(self, settings: RelaySettings) -> None:
        claims = verify_token(settings, mint(settings, ttl_s=60))
        assert 0 < claims.ttl_remaining <= 60

    def test_to_dict_is_json_serialisable(self, settings: RelaySettings) -> None:
        # The frontend renders this directly.
        claims = verify_token(settings, mint(settings))
        json.dumps(claims.to_dict())


class TestRoomScoping:
    def test_token_for_another_room_is_rejected(self, settings: RelaySettings) -> None:
        token = mint(settings, room="room_OTHER")
        with pytest.raises(AuthError) as excinfo:
            verify_token(settings, token, expected_room=ROOM)
        assert "different room" in excinfo.value.message
        # Both rooms appear in the context, so an operator can see the mix-up
        # rather than guessing which side is wrong.
        assert excinfo.value.context["token_room"] == "room_OTHER"

    def test_token_without_an_expected_room_still_verifies(self, settings: RelaySettings) -> None:
        # /auth/token has no room in the path, so it must be able to verify
        # without one.
        assert verify_token(settings, mint(settings)).room == ROOM


class TestTampering:
    def test_signature_change_is_rejected(self, settings: RelaySettings) -> None:
        token = mint(settings)
        header, payload, signature = token.split(".")
        forged = f"{header}.{payload}.{'A' * len(signature)}"
        with pytest.raises(AuthError):
            verify_token(settings, forged)

    def test_payload_edit_is_rejected(self, settings: RelaySettings) -> None:
        token = mint(settings)
        header, payload, signature = token.split(".")
        decoded = json.loads(base64.urlsafe_b64decode(payload + "=="))
        decoded["role"] = "owner"  # privilege escalation attempt
        forged_payload = (
            base64.urlsafe_b64encode(json.dumps(decoded).encode()).rstrip(b"=").decode()
        )
        with pytest.raises(AuthError):
            verify_token(settings, f"{header}.{forged_payload}.{signature}")

    def test_alg_none_is_rejected(self, settings: RelaySettings) -> None:
        """The classic JWT vulnerability: `alg: none` with an empty signature.

        The only defence that works is never reading the algorithm from the
        header, which is why `verify_token` pins `algorithms=[ALGORITHM]`.
        """
        forged_header = (
            base64.urlsafe_b64encode(json.dumps({"alg": "none", "typ": "JWT"}).encode())
            .rstrip(b"=")
            .decode()
        )
        payload = (
            base64.urlsafe_b64encode(
                json.dumps(
                    {
                        "iss": settings.jwt_issuer,
                        "aud": settings.jwt_audience,
                        "sub": SUBJECT,
                        "room": ROOM,
                        "member": MEMBER,
                        "role": "owner",
                        "exp": int(datetime.now(UTC).timestamp()) + 3600,
                    }
                ).encode()
            )
            .rstrip(b"=")
            .decode()
        )
        with pytest.raises(AuthError):
            verify_token(settings, f"{forged_header}.{payload}.")

    def test_token_signed_with_another_secret_is_rejected(
        self, settings: RelaySettings, settings_factory: Callable[..., RelaySettings]
    ) -> None:
        other = settings_factory(JWT_SECRET="a-completely-different-secret-value-32b")
        with pytest.raises(AuthError):
            verify_token(settings, mint(other))

    def test_garbage_is_rejected(self, settings: RelaySettings) -> None:
        with pytest.raises(AuthError):
            verify_token(settings, "not.a.token")

    def test_empty_token_is_rejected_with_a_hint(self, settings: RelaySettings) -> None:
        with pytest.raises(AuthError) as excinfo:
            verify_token(settings, "")
        assert "query parameter" in (excinfo.value.hint or "")


class TestExpiry:
    def test_expired_token_is_rejected_as_expired_not_invalid(
        self, settings: RelaySettings
    ) -> None:
        # The distinction matters to a client: expired means "get a new token",
        # invalid means "your token is wrong", and they lead to different fixes.
        token = mint(settings, ttl_s=-7200)  # well past the 30s leeway
        with pytest.raises(TokenExpiredError) as excinfo:
            verify_token(settings, token)
        assert "expired" in excinfo.value.message
        assert "Redeem your invite again" in (excinfo.value.hint or "")

    def test_leeway_tolerates_small_clock_skew(self, settings: RelaySettings) -> None:
        # A VM whose clock drifted a few seconds should not cause an outage.
        token = mint(settings, ttl_s=-5)
        assert verify_token(settings, token).room == ROOM

    def test_algorithm_is_hs256(self) -> None:
        assert ALGORITHM == "HS256"


class TestInvites:
    def test_tokens_are_unique_and_long(self) -> None:
        tokens = {new_invite_token() for _ in range(50)}
        assert len(tokens) == 50
        assert all(len(t) >= 40 for t in tokens)

    def test_hash_is_stable_and_not_the_token(self) -> None:
        token = new_invite_token()
        digest = hash_invite(token)
        assert digest == hash_invite(token)
        assert digest != token
        assert len(digest) == 64  # sha256 hex

    def test_lookup_hash_matches_the_stored_hash(self) -> None:
        # If these ever diverge, redemption silently stops finding rows.
        token = new_invite_token()
        assert invite_lookup_hash(token) == hash_invite(token)

    def test_different_tokens_hash_differently(self) -> None:
        assert hash_invite(new_invite_token()) != hash_invite(new_invite_token())


class TestInviteUsability:
    def test_revoked_is_refused(self) -> None:
        with pytest.raises(InviteInvalidError) as excinfo:
            check_invite_usable(
                revoked=True, uses=0, max_uses=1, expires=datetime.now(UTC) + timedelta(days=1)
            )
        assert "revoked" in excinfo.value.message

    def test_exhausted_is_refused(self) -> None:
        with pytest.raises(InviteInvalidError) as excinfo:
            check_invite_usable(
                revoked=False, uses=1, max_uses=1, expires=datetime.now(UTC) + timedelta(days=1)
            )
        assert "already been used" in excinfo.value.message

    def test_expired_is_refused(self) -> None:
        with pytest.raises(InviteInvalidError) as excinfo:
            check_invite_usable(
                revoked=False, uses=0, max_uses=1, expires=datetime.now(UTC) - timedelta(seconds=1)
            )
        assert "expired" in excinfo.value.message

    def test_naive_expiry_is_read_as_utc_not_a_crash(self) -> None:
        """A naive timestamp from the database must not produce a 500.

        Postgres returns aware datetimes for `timestamptz`, but a driver change or
        a hand-written row can produce a naive one. Comparing naive against aware
        raises `TypeError`, which would surface as a 500 on what is really a 401.
        """
        naive_future = datetime.now(UTC).replace(tzinfo=None) + timedelta(days=1)
        check_invite_usable(revoked=False, uses=0, max_uses=1, expires=naive_future)

    def test_multi_use_invite_is_usable_until_exhausted(self) -> None:
        check_invite_usable(
            revoked=False, uses=4, max_uses=5, expires=datetime.now(UTC) + timedelta(hours=1)
        )


class TestBearerParsing:
    @pytest.mark.parametrize(
        ("header", "expected"),
        [
            ("Bearer abc123", "abc123"),
            ("bearer abc123", "abc123"),
            ("BEARER abc123", "abc123"),
            ("Bearer   abc123  ", "abc123"),
            ("Basic abc123", None),
            ("abc123", None),
            ("Bearer", None),
            ("", None),
            (None, None),
        ],
    )
    def test_bearer_extraction(self, header: str | None, expected: str | None) -> None:
        assert extract_bearer(header) == expected

    def test_returns_none_rather_than_raising(self) -> None:
        # The WebSocket handshake falls through to the query parameter when this
        # returns None; an exception mid-handshake would be much harder to handle.
        assert extract_bearer("garbage") is None
