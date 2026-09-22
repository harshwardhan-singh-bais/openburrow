"""HTTP and WebSocket routes.

Three decisions in here are worth knowing before reading the code.

**WebSocket auth is a first message, with a query parameter as fallback.** A
browser cannot set headers on a WebSocket handshake, so the obvious option is
``?token=...``. That puts a credential in the request line, which lands in access
logs, proxy logs, and browser history — so the *preferred* path is a first frame
``{"type":"auth","token":"..."}``, and the query parameter exists for
non-browser clients that find that awkward. Both are supported; only one is
recommended, and the logs say which was used.

**The socket is accepted before it is authenticated.** The alternative — closing
the handshake with a 403 — gives the client an HTTP status and nothing else. By
accepting first, an auth failure is a structured frame with a ``code`` and a
``hint``, then a close with a 4xxx code. The cost is that an unauthenticated
socket exists for up to five seconds, which is why the auth deadline is short and
why it is enforced by ``wait_for`` rather than by trusting the client.

**Publishing over HTTP is a first-class path, not a fallback.** A daemon behind a
proxy that will not pass a long-lived WebSocket can still publish with
``POST /rooms/{room}/events``. Both paths call the same ``append_events`` and the
same fan-out, so they cannot drift apart.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import os
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Header, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from starlette.websockets import WebSocketState

from openburrow.core.errors import OpenBurrowError
from openburrow.core.logging import get_logger
from openburrow.relay.errors import (
    AdminSurfaceDisabledError,
    AuthError,
    BadRequestError,
    NotAMemberError,
    RateLimitedError,
    RoomFullError,
    RoomNotFoundError,
)
from openburrow.relay.hub import Connection
from openburrow.relay.metrics import CONTENT_TYPE as METRICS_CONTENT_TYPE, render as render_metrics
from openburrow.relay.models import RoomRole, role_at_least
from openburrow.relay.ratelimit import TokenBucket
from openburrow.relay.security import (
    TokenClaims,
    extract_bearer,
    issue_token,
    new_ssh_challenge,
    provenance_extension,
    ssh_fingerprint,
    verify_ssh_challenge,
    verify_ssh_signature,
    verify_token,
)
from openburrow.relay.state import AppState
from openburrow.relay.store import (
    DEFAULT_TAIL_LIMIT,
    MAX_EVENTS_PER_APPEND,
    MAX_TAIL_LIMIT,
    AppendResult,
)

log = get_logger(__name__)

router = APIRouter()

#: How long a freshly accepted socket has to authenticate. Short, because an
#: unauthenticated socket is a resource anyone can allocate.
AUTH_DEADLINE_S = 5.0

#: WebSocket close codes. The 4000-4999 range is application-private.
WS_AUTH_REQUIRED = 4401
WS_FORBIDDEN = 4403
WS_AUTH_TIMEOUT = 4408
WS_TOO_MANY = 4409
#: 1009 is the standard "message too big", and is not in the private range
#: because clients already understand it.
WS_TOO_LARGE = 1009

#: Env var holding the operator key. Absent means the admin surface is off.
ADMIN_KEY_VAR = "OPENBURROW_RELAY_ADMIN_KEY"


def state_of(request: Request) -> AppState:
    return request.app.state.relay


def ws_state_of(websocket: WebSocket) -> AppState:
    return websocket.app.state.relay


# ==========================================================================
# Health
# ==========================================================================
@router.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, Any]:
    """Liveness only.

    Deliberately touches nothing. A liveness probe that checks the database
    turns a database blip into a container restart loop, which is a strictly
    worse outcome than the blip.
    """
    return {"ok": True}


@router.get("/readyz")
async def readyz(request: Request, response: Response) -> dict[str, Any]:
    """Readiness. Checks the database and reports honestly when it cannot."""
    state = state_of(request)
    database = await state.store.healthcheck()
    hub_counts = state.hub.counts()
    ready = bool(database.get("ok")) and state.ready

    if not ready:
        # 503 so a load balancer removes this instance. The body still carries
        # the detail, because a probe that returns nothing but a status code is
        # a probe someone has to reproduce by hand.
        response.status_code = 503

    return {
        "ok": ready,
        "uptime_s": round(state.uptime_s, 1),
        "database": database,
        "connections": hub_counts,
        "rooms": state.hub.rooms(),
    }


@router.get("/metrics", include_in_schema=False)
async def metrics(request: Request) -> Response:
    state = state_of(request)
    if not state.settings.metrics_enabled:
        # 200 with an explanation, not 404. A scrape that silently starts
        # failing looks like a broken relay; this says what is actually true.
        return PlainTextResponse(
            "# metrics are disabled on this relay\n"
            "# set OPENBURROW_RELAY_METRICS_ENABLED=1 to enable them\n",
            status_code=200,
        )
    return Response(content=render_metrics(state.metrics), media_type=METRICS_CONTENT_TYPE)


# ==========================================================================
# Request bodies
# ==========================================================================
class RedeemRequest(BaseModel):
    invite: str = Field(min_length=8, max_length=512)
    subject: str = Field(min_length=1, max_length=200)
    display_name: str = Field(default="", max_length=200)


class SshChallengeRequest(BaseModel):
    public_key: str = Field(min_length=1, max_length=4096)


class SshAuthRequest(BaseModel):
    public_key: str = Field(min_length=1, max_length=4096)
    challenge: str = Field(min_length=8, max_length=1024)
    signature: str = Field(min_length=8, max_length=8192)
    display_name: str = Field(default="", max_length=200)


class CreateRoomRequest(BaseModel):
    repo_slug: str = Field(min_length=1, max_length=300)
    name: str = Field(default="", max_length=200)
    created_by: str = Field(default="", max_length=200)
    retention_days: int | None = Field(default=None, ge=0, le=3650)


class CreateInviteRequest(BaseModel):
    role: str = Field(default=str(RoomRole.VIEWER))
    subject: str = Field(default="", max_length=200)
    days: int = Field(default=7, ge=1, le=365)
    max_uses: int = Field(default=1, ge=1, le=1000)
    label: str = Field(default="", max_length=200)


class PublishRequest(BaseModel):
    events: list[dict[str, Any]] = Field(default_factory=list)


# ==========================================================================
# Authentication
# ==========================================================================
@router.post("/auth/token")
async def auth_token(request: Request, body: RedeemRequest) -> dict[str, Any]:
    """Exchange an invite for a room token.

    Rate limited, and the limit is per client address. This is the only endpoint
    that accepts a secret from an unauthenticated caller, so it is the one that
    most needs a ceiling — without it, invite guessing is a free-for-all.
    """
    state = state_of(request)
    client = _client_key(request)
    bucket = state.http_buckets.get(client)
    if not bucket.allow():
        state.metrics.rate_limited.labels(surface="auth").inc()
        retry = bucket.retry_after()
        raise RateLimitedError(
            "too many token requests",
            retry_after=retry,
            hint="Wait and retry. Token requests are limited per client address.",
        )

    room, member, invite = await state.store.redeem_invite(
        token=body.invite,
        subject=body.subject,
        display_name=body.display_name,
    )
    token, claims = issue_token(
        state.settings,
        room=room.id,
        member=member.id,
        subject=member.subject,
        role=member.room_role,
    )
    state.metrics.tokens_issued.inc()

    log.info(
        "relay.token_issued",
        room=room.id,
        member=member.id,
        role=str(member.room_role),
        invite=invite.id,
        used=invite.uses,
    )
    return {
        "token": token,
        "token_type": "Bearer",
        "room": {
            "id": room.id,
            "repo_slug": room.repo_slug,
            "name": room.name,
        },
        "member": {
            "id": member.id,
            "subject": member.subject,
            "display_name": member.display_name,
            "role": str(member.room_role),
        },
        "expires_at": claims.expires_at,
        "expires_in_s": claims.ttl_remaining,
    }


# ==========================================================================
# SSH-key identity (item 243)
# ==========================================================================
@router.post("/auth/ssh/challenge")
async def ssh_challenge(request: Request, body: SshChallengeRequest) -> dict[str, Any]:
    """Mint a challenge for the caller to sign with their SSH key.

    Stateless: the challenge carries an HMAC over its nonce and expiry keyed
    with the JWT secret, so verification needs no challenge store. The public
    key is not validated here — an unparseable key simply cannot produce a
    signature the auth step will accept, and telling an attacker their key is
    malformed is free reconnaissance.
    """
    state = state_of(request)
    client = _client_key(request)
    bucket = state.http_buckets.get(client)
    if not bucket.allow():
        state.metrics.rate_limited.labels(surface="ssh-challenge").inc()
        raise RateLimitedError(
            "too many challenge requests",
            retry_after=bucket.retry_after(),
            hint="Wait and retry. Challenge requests are limited per client address.",
        )
    challenge, expires = new_ssh_challenge(secret=state.settings.jwt_secret)
    return {
        "challenge": challenge,
        "expires_at": expires,
        "fingerprint": ssh_fingerprint(body.public_key),
        "hint": (
            "Sign with: ssh-keygen -Y sign -f <key> <challenge-file>, then POST "
            "the base64 signature to /auth/ssh/auth."
        ),
    }


@router.post("/auth/ssh/auth")
async def ssh_auth(request: Request, body: SshAuthRequest) -> dict[str, Any]:
    """Exchange a signed SSH challenge for a room token.

    Trust order matters: the challenge is verified first (proves the relay
    minted it and it is fresh), then the signature (proves the caller holds the
    private key), and only then is membership granted — to the *fingerprint*, a
    derived identity, never to a name the caller typed. A key the room owner has
    not admitted fails here even with a valid signature.
    """
    state = state_of(request)
    client = _client_key(request)
    bucket = state.http_buckets.get(client)
    if not bucket.allow():
        state.metrics.rate_limited.labels(surface="ssh-auth").inc()
        raise RateLimitedError(
            "too many auth requests",
            retry_after=bucket.retry_after(),
            hint="Wait and retry. Auth requests are limited per client address.",
        )

    verify_ssh_challenge(body.challenge, secret=state.settings.jwt_secret)
    if not verify_ssh_signature(body.public_key, body.challenge, body.signature):
        state.metrics.auth_failures.labels(reason="ssh_signature").inc()
        raise AuthError(
            "signature does not verify against the presented key",
            hint="Sign the challenge bytes exactly as issued, with the matching private key.",
        )

    subject = ssh_fingerprint(body.public_key)
    if not subject:
        state.metrics.auth_failures.labels(reason="ssh_key_unparseable").inc()
        raise AuthError("public key could not be parsed")

    member = await state.store.get_member_by_subject(subject)
    if member is None:
        state.metrics.auth_failures.labels(reason="ssh_key_unknown").inc()
        raise AuthError(
            "this key has not been admitted to any room",
            hint="A room owner must create an invite or membership for this key's fingerprint first.",
            context={"fingerprint": subject},
        )

    room = await state.store.require_room(member.room_id)
    token, claims = issue_token(
        state.settings,
        room=room.id,
        member=member.id,
        subject=subject,
        role=member.room_role,
    )
    state.metrics.tokens_issued.inc()
    log.info("relay.ssh_auth", room=room.id, member=member.id, subject=subject[:32])
    return {
        "token": token,
        "token_type": "Bearer",
        "provenance": provenance_extension(claims)["provenance"],
        "room": {"id": room.id, "repo_slug": room.repo_slug, "name": room.name},
        "member": {
            "id": member.id,
            "subject": subject,
            "display_name": member.display_name,
            "role": str(member.room_role),
        },
        "expires_at": claims.expires_at,
        "expires_in_s": claims.ttl_remaining,
    }


# ==========================================================================
# Rooms
# ==========================================================================
@router.get("/rooms")
async def list_rooms(
    request: Request, authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    """Rooms the caller belongs to.

    Requires a token whose subject is the membership key. There is no "list all
    rooms" — a relay that can enumerate its tenants to any authenticated caller
    leaks its customer list.
    """
    state = state_of(request)
    claims = _require_token(state, authorization=authorization)
    rooms = await state.store.list_rooms_for(claims.subject)
    return {
        "rooms": [
            {
                "id": room.id,
                "repo_slug": room.repo_slug,
                "name": room.name,
                "archived": room.archived,
                "retention_days": room.retention_days,
            }
            for room in rooms
        ]
    }


@router.post("/rooms", status_code=201)
async def create_room(
    request: Request,
    body: CreateRoomRequest,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
) -> dict[str, Any]:
    """Create a room. Operator-only.

    Rooms are created by an operator, not self-served. A room is a tenancy
    boundary; letting any caller create one turns the relay into a free
    multi-tenant database with an unbounded row count.
    """
    state = state_of(request)
    _require_admin(state, x_admin_key)

    existing = await state.store.find_room_by_slug(body.repo_slug)
    if existing is not None:
        # Idempotent by slug rather than an error. Re-running a provisioning
        # script should not fail because the room already exists.
        return {
            "created": False,
            "room": {
                "id": existing.id,
                "repo_slug": existing.repo_slug,
                "name": existing.name,
            },
        }

    room = await state.store.create_room(
        repo_slug=body.repo_slug,
        name=body.name or body.repo_slug,
        created_by=body.created_by or "operator",
        retention_days=body.retention_days,
    )
    return {
        "created": True,
        "room": {"id": room.id, "repo_slug": room.repo_slug, "name": room.name},
    }


@router.get("/rooms/{room}")
async def get_room(
    request: Request,
    room: str,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    state = state_of(request)
    claims = _require_token(state, authorization=authorization)
    record = await state.store.require_room(room)
    member = await state.store.get_member(record.id, claims.subject)
    if member is None:
        raise NotAMemberError(
            f"you are not a member of room {record.id!r}",
            hint="Redeem an invite for this room first.",
            context={"room": record.id},
        )
    return {
        "room": {
            "id": record.id,
            "repo_slug": record.repo_slug,
            "name": record.name,
            "archived": record.archived,
            "retention_days": record.retention_days,
        },
        "you": {
            "member": member.id,
            "subject": member.subject,
            "role": str(member.room_role),
        },
        "members": len(await state.store.list_members(record.id)),
        "connections": len(state.hub.room_connections(record.id)),
        "latest_seqs": await state.store.latest_seqs(record.id),
        "event_count": await state.store.event_count(record.id),
    }


@router.post("/rooms/{room}/invites", status_code=201)
async def create_invite(
    request: Request,
    room: str,
    body: CreateInviteRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Mint an invite. Owner only.

    The plaintext token appears in this response and nowhere else, ever. It is
    stored as a SHA-256 hash, so "resend the invite" is not a feature the relay
    can offer — which is the correct property for a credential.
    """
    state = state_of(request)
    claims = _require_token(state, authorization=authorization)
    record = await state.store.require_room(room)
    _require_role(claims, RoomRole.OWNER, room=record.id)

    try:
        role = RoomRole(body.role)
    except ValueError as exc:
        raise BadRequestError(
            f"unknown role {body.role!r}",
            hint=f"Valid roles are: {', '.join(str(r) for r in RoomRole)}.",
            context={"role": body.role},
        ) from exc

    token, invite = await state.store.create_invite(
        room_id=record.id,
        role=role,
        created_by=claims.subject,
        days=body.days,
        max_uses=body.max_uses,
        label=body.label or body.subject,
    )
    return {
        "invite": {
            "id": invite.id,
            "room": record.id,
            "role": str(role),
            "expires_at": invite.expires_at.isoformat(),
            "max_uses": invite.max_uses,
            "label": invite.label,
        },
        # Shown once. Not retrievable later.
        "token": token,
        "redeem_with": "POST /auth/token",
    }


@router.get("/rooms/{room}/invites")
async def list_invites(
    request: Request,
    room: str,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    state = state_of(request)
    claims = _require_token(state, authorization=authorization)
    record = await state.store.require_room(room)
    _require_role(claims, RoomRole.OWNER, room=record.id)

    invites = await state.store.list_invites(record.id)
    return {
        "invites": [
            {
                "id": inv.id,
                "role": inv.role,
                "created_by": inv.created_by,
                "created_at": inv.created_at.isoformat(),
                "expires_at": inv.expires_at.isoformat(),
                "uses": inv.uses,
                "max_uses": inv.max_uses,
                "revoked": inv.revoked,
                "label": inv.label,
            }
            for inv in invites
        ]
    }


@router.delete("/invites/{invite_id}")
async def revoke_invite(
    request: Request,
    invite_id: str,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    state = state_of(request)
    claims = _require_token(state, authorization=authorization)
    # Look up the invite first so the permission check can be scoped to its room.
    invites = await state.store.list_invites("")
    target = next((inv for inv in invites if inv.id == invite_id), None)
    if target is None:
        # Fall back to a direct search across the caller's rooms.
        for room in await state.store.list_rooms_for(claims.subject):
            for inv in await state.store.list_invites(room.id):
                if inv.id == invite_id:
                    target = inv
                    break
            if target is not None:
                break
    if target is None:
        raise RoomNotFoundError(
            f"no invite {invite_id!r}",
            hint="Check the id, or list invites for the room.",
            context={"invite": invite_id},
        )
    _require_role(claims, RoomRole.OWNER, room=target.room_id)

    revoked = await state.store.revoke_invite(invite_id)
    return {"revoked": revoked, "invite": invite_id}


# ==========================================================================
# Events
# ==========================================================================
@router.get("/rooms/{room}/events")
async def tail_events(
    request: Request,
    room: str,
    *,
    since: str | None = None,
    limit: int = DEFAULT_TAIL_LIMIT,
    session: str | None = None,
    event_type: str | None = None,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Events a client has not seen, resuming per origin repo.

    ``since`` is ``repo:seq,repo:seq`` — the same shape the WebSocket's ``lag``
    notice hands out, so recovery from a gap is a copy and paste rather than a
    translation between two formats.
    """
    state = state_of(request)
    claims = _require_token(state, authorization=authorization)
    record = await state.store.require_room(room)
    await _require_membership(state, record.id, claims)

    parsed_since = _parse_since(since)
    events = await state.store.tail_events(
        record.id,
        since=parsed_since,
        limit=max(1, min(limit, MAX_TAIL_LIMIT)),
        session_id=session,
        event_types=[event_type] if event_type else None,
    )
    return {
        "room": record.id,
        "since": parsed_since,
        "count": len(events),
        "events": events,
        "latest_seqs": await state.store.latest_seqs(record.id),
        "ordering": "per-origin: (origin_repo, origin_seq). There is no global order.",
    }


@router.post("/rooms/{room}/events")
async def publish_events(
    request: Request,
    room: str,
    body: PublishRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Publish events without holding a socket. Maintainer or above."""
    state = state_of(request)
    claims = _require_token(state, authorization=authorization)
    record = await state.store.require_room(room)
    await _require_membership(state, record.id, claims)
    _require_role(claims, RoomRole.MAINTAINER, room=record.id)

    result = await state.store.append_events(record.id, body.events)
    if result.accepted:
        # Fan out only the events that were actually stored. Re-fanning a
        # duplicate would deliver the same event twice to every client, which is
        # the one thing the dedupe exists to prevent.
        await _fan_out(state, record.id, result)

    return {"room": record.id, **result.to_dict()}


# ==========================================================================
# WebSocket: events
# ==========================================================================
@router.websocket("/rooms/{room}/stream")
async def stream(websocket: WebSocket, room: str) -> None:
    state = ws_state_of(websocket)
    await websocket.accept()

    claims = await _authenticate_ws(websocket, state, room)
    if claims is None:
        return

    admitted = await _admit_socket(websocket, state, room, claims, kind="events")
    if admitted is None:
        return
    record, member, conn = admitted

    pump = asyncio.create_task(state.hub.pump(conn, websocket.send_json))
    try:
        await websocket.send_json(
            {
                "type": "hello",
                "connection": conn.id,
                "room": record.id,
                "you": {
                    "member": member.id,
                    "subject": member.subject,
                    "role": str(member.room_role),
                },
                "latest_seqs": await state.store.latest_seqs(record.id),
                "limits": {
                    "max_frame_bytes": state.settings.max_frame_bytes,
                    "event_rate_per_s": state.settings.event_rate_per_s,
                    "event_burst": state.settings.event_burst,
                    "max_events_per_publish": MAX_EVENTS_PER_APPEND,
                },
            }
        )
        await _stream_loop(websocket, state, conn)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.warning("relay.stream_error", connection=conn.id, error=str(exc))
    finally:
        await _teardown(state, conn, pump, member)


async def _stream_loop(websocket: WebSocket, state: AppState, conn: Connection) -> None:
    """Read frames from one events connection until it goes away.

    The frame dispatch is a flat chain of named handlers rather than inline
    blocks. That is not stylistic: inlining everything put the loop at thirteen
    branches and seventy statements, which is past the point where a reader can
    hold it in their head — and this is the loop that decides whether a client's
    events get stored.
    """
    bucket = TokenBucket(rate=state.settings.event_rate_per_s, burst=state.settings.event_burst)

    while True:
        frame = await websocket.receive()

        if frame.get("type") == "websocket.disconnect":
            break

        raw = frame.get("text")
        if raw is None:
            if frame.get("bytes") is not None:
                await _reject_binary(websocket, state)
            continue

        if len(raw.encode("utf-8")) > state.settings.max_frame_bytes:
            state.metrics.frames_rejected.labels(reason="too_large").inc()
            await websocket.send_json(
                {
                    "type": "error",
                    "code": "frame_too_large",
                    "message": f"frame exceeds {state.settings.max_frame_bytes} bytes",
                }
            )
            await _ws_close(websocket, WS_TOO_LARGE, "frame too large")
            return

        try:
            message = _decode(raw)
        except ValueError as exc:
            state.metrics.frames_rejected.labels(reason="bad_json").inc()
            await websocket.send_json({"type": "error", "code": "bad_json", "message": str(exc)})
            continue

        kind = message.get("type")
        if kind == "ping":
            await websocket.send_json({"type": "pong", "at": _now_iso()})
        elif kind == "publish":
            await _handle_publish(websocket, state, conn, bucket, message)
        elif kind == "resume":
            await _handle_resume(websocket, state, conn, message)
        else:
            await websocket.send_json(
                {
                    "type": "error",
                    "code": "unknown_message_type",
                    "message": f"unknown message type {kind!r}",
                    "known": ["ping", "publish", "resume"],
                }
            )


async def _reject_binary(websocket: WebSocket, state: AppState) -> None:
    """Refuse a binary frame explicitly rather than ignoring it.

    Silently dropping it would leave a client that is sending binary convinced it
    is publishing.
    """
    state.metrics.frames_rejected.labels(reason="binary").inc()
    await websocket.send_json(
        {
            "type": "error",
            "code": "binary_not_supported",
            "message": "this socket carries JSON text frames only",
        }
    )


async def _handle_publish(
    websocket: WebSocket,
    state: AppState,
    conn: Connection,
    bucket: TokenBucket,
    message: dict[str, Any],
) -> None:
    """Store a batch of events and fan them out."""
    events = message.get("events") or []
    if not isinstance(events, list):
        await websocket.send_json(
            {"type": "error", "code": "bad_publish", "message": "events must be a list"}
        )
        return

    # Rate limiting is charged per event, not per frame: one frame carrying a
    # thousand events is a thousand events.
    cost = max(1, len(events))
    if not bucket.allow(cost=cost):
        state.metrics.rate_limited.labels(surface="ws_publish").inc()
        await websocket.send_json(
            {
                "type": "error",
                "code": "rate_limited",
                "message": "publish rate exceeded",
                "retry_after_s": round(bucket.retry_after(cost=cost), 3),
            }
        )
        return

    result = await state.store.append_events(conn.room, events)
    if result.accepted:
        await _fan_out(state, conn.room, result, exclude=conn.id)
    await websocket.send_json({"type": "ack", **result.to_dict()})


async def _handle_resume(
    websocket: WebSocket,
    state: AppState,
    conn: Connection,
    message: dict[str, Any],
) -> None:
    """Send a client the events it missed after a gap."""
    since = message.get("since") or {}
    if not isinstance(since, dict):
        await websocket.send_json(
            {"type": "error", "code": "bad_resume", "message": "since must be an object"}
        )
        return

    try:
        resume = {str(repo): int(seq) for repo, seq in since.items()}
    except (TypeError, ValueError):
        await websocket.send_json(
            {
                "type": "error",
                "code": "bad_resume",
                "message": "since values must be integers, keyed by origin repo",
            }
        )
        return

    events = await state.store.tail_events(
        conn.room,
        since=resume,
        limit=min(int(message.get("limit") or DEFAULT_TAIL_LIMIT), MAX_TAIL_LIMIT),
    )
    await websocket.send_json(
        {
            "type": "resume_result",
            "count": len(events),
            "events": events,
            "latest_seqs": await state.store.latest_seqs(conn.room),
        }
    )


# ==========================================================================
# WebSocket: brain doc
# ==========================================================================
@router.websocket("/rooms/{room}/doc")
async def doc(websocket: WebSocket, room: str) -> None:
    """Relay CRDT updates without understanding them.

    The relay stores and forwards opaque base64 blobs. It does not decode them,
    does not merge them, and does not decide what is newer — merging is the
    clients' job, which is what makes it safe to relay a document the relay
    cannot read. See ADR 0007.
    """
    state = ws_state_of(websocket)
    await websocket.accept()

    claims = await _authenticate_ws(websocket, state, room)
    if claims is None:
        return

    admitted = await _admit_socket(websocket, state, room, claims, kind="doc")
    if admitted is None:
        return
    # The room record is not needed past admission: `conn.room` carries the id,
    # and `conn` is what every subsequent call takes.
    _record, member, conn = admitted

    pump = asyncio.create_task(state.hub.pump(conn, websocket.send_json))
    can_write = role_at_least(member.room_role, RoomRole.MAINTAINER)

    try:
        await _send_doc_hello(websocket, state, conn, can_write)
        await _doc_loop(websocket, state, conn, member, can_write=can_write)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.warning("relay.doc_error", connection=conn.id, error=str(exc))
    finally:
        await _teardown(state, conn, pump, member)


async def _send_doc_hello(
    websocket: WebSocket,
    state: AppState,
    conn: Connection,
    can_write: bool,
) -> None:
    """Hand the client the current snapshot and tell it whether it may write."""
    snapshot = await state.store.load_doc(conn.room)
    await websocket.send_json(
        {
            "type": "hello",
            "connection": conn.id,
            "room": conn.room,
            "can_write": can_write,
            "snapshot": {
                "update": snapshot.update_b64 if snapshot else "",
                "version": snapshot.version if snapshot else 0,
            },
            "max_frame_bytes": state.settings.max_doc_frame_bytes,
        }
    )


async def _doc_loop(
    websocket: WebSocket,
    state: AppState,
    conn: Connection,
    member: Any,
    *,
    can_write: bool,
) -> None:
    """Read CRDT frames from one doc connection until it goes away."""
    while True:
        frame = await websocket.receive()
        if frame.get("type") == "websocket.disconnect":
            return

        raw = frame.get("text")
        if raw is None:
            continue

        if len(raw.encode("utf-8")) > state.settings.max_doc_frame_bytes:
            state.metrics.frames_rejected.labels(reason="doc_too_large").inc()
            await _ws_close(websocket, WS_TOO_LARGE, "doc frame too large")
            return

        try:
            message = _decode(raw)
        except ValueError as exc:
            state.metrics.frames_rejected.labels(reason="bad_json").inc()
            await websocket.send_json({"type": "error", "code": "bad_json", "message": str(exc)})
            continue

        kind = message.get("type")
        if kind == "ping":
            await websocket.send_json({"type": "pong", "at": _now_iso()})
        elif kind == "update":
            await _handle_doc_update(websocket, state, conn, member, message, can_write=can_write)
        else:
            await websocket.send_json(
                {
                    "type": "error",
                    "code": "unknown_message_type",
                    "message": f"unknown message type {kind!r}",
                    "known": ["ping", "update"],
                }
            )


async def _handle_doc_update(
    websocket: WebSocket,
    state: AppState,
    conn: Connection,
    member: Any,
    message: dict[str, Any],
    *,
    can_write: bool,
) -> None:
    """Persist one CRDT update and relay it to the room's other doc sockets."""
    if not can_write:
        state.metrics.frames_rejected.labels(reason="doc_read_only").inc()
        await websocket.send_json(
            {
                "type": "error",
                "code": "read_only",
                "message": "your role does not allow writing to the brain doc",
            }
        )
        return

    update = str(message.get("update") or "")
    if not update:
        return

    version = await state.store.save_doc_update(
        conn.room, update_b64=update, updated_by=member.subject
    )
    state.metrics.doc_updates_in.inc()
    await websocket.send_json({"type": "saved", "version": version})

    delivered = state.hub.publish(
        conn.room,
        {
            "type": "update",
            "from": member.id,
            "from_subject": member.subject,
            "update": update,
            "version": version,
        },
        kind="doc",
        exclude=conn.id,
    )
    if delivered:
        state.metrics.doc_updates_out.inc(delivered)


async def _admit_socket(
    websocket: WebSocket,
    state: AppState,
    room: str,
    claims: TokenClaims,
    *,
    kind: str,
) -> tuple[Any, Any, Connection] | None:
    """Resolve the room and member, then register the connection.

    Returns ``(room, member, conn)``, or ``None`` after closing the socket with a
    reason. Shared by both socket kinds because the admission rules are identical
    — and two copies of an admission rule is how one of them quietly stops
    matching the other.
    """
    try:
        record = await state.store.require_room(room)
        member = await state.store.get_member(record.id, claims.subject)
        if member is None:
            await _ws_close(websocket, WS_FORBIDDEN, "you are not a member of this room")
            return None
        conn = state.hub.admit(
            kind=kind,
            room=record.id,
            member=member.id,
            subject=member.subject,
            role=member.room_role,
        )
    except RoomNotFoundError as exc:
        await _ws_close(websocket, 4404, exc.message)
        return None
    except RoomFullError as exc:
        await _ws_close(websocket, WS_TOO_MANY, exc.message)
        return None
    return record, member, conn


async def _teardown(
    state: AppState,
    conn: Connection,
    pump: asyncio.Task[None],
    member: Any,
) -> None:
    """Deregister a socket and stop its writer.

    Ordered so the hub forgets the connection before the writer is cancelled —
    otherwise a frame published in between is queued for a connection nobody is
    draining, and the queue silently grows until the task is collected.
    """
    state.hub.release(conn)
    pump.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await pump
    with contextlib.suppress(Exception):
        await state.store.touch_member(member.id)


# ==========================================================================
# Helpers
# ==========================================================================
def _client_key(request: Request) -> str:
    """Rate-limit key for an HTTP request.

    Honours ``X-Forwarded-For`` only when the operator has said a proxy is in
    front. Trusting it unconditionally makes the limiter trivially bypassable by
    anyone who can set a header — which is everyone.
    """
    settings = state_of(request).settings
    if settings.tls_terminated:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",", 1)[0].strip()
    return request.client.host if request.client else "unknown"


def _require_token(state: AppState, *, authorization: str | None) -> TokenClaims:
    token = extract_bearer(authorization)
    if token is None:
        raise AuthError(
            "no bearer token supplied",
            hint="Send `Authorization: Bearer <room token>`. Get one from POST /auth/token.",
        )
    return verify_token(state.settings, token)


def _require_admin(state: AppState, supplied: str | None) -> None:
    """Check the operator key.

    If the key is unset the surface is *disabled*, not open. A missing
    environment variable defaulting to "allow everyone" is how admin endpoints
    end up on the internet, and it is worth an explicit refusal to avoid.

    ``settings.extra`` is where the config loader puts any ``OPENBURROW_RELAY_*``
    variable it does not recognise, so the admin key arrives without needing a
    dedicated field — and a typo'd variable name is caught at startup rather
    than silently disabling provisioning.
    """
    expected = state.settings.extra.get("ADMIN_KEY") or os.environ.get(ADMIN_KEY_VAR, "")
    if not expected:
        raise AdminSurfaceDisabledError(
            "the admin surface is disabled",
            hint=f"Set {ADMIN_KEY_VAR} to enable room provisioning.",
            context={"surface": "admin"},
        )
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise AuthError(
            "admin key is missing or wrong",
            hint="Send the operator key in the X-Admin-Key header.",
        )


async def _require_membership(state: AppState, room_id: str, claims: TokenClaims) -> None:
    member = await state.store.get_member(room_id, claims.subject)
    if member is None:
        raise NotAMemberError(
            f"you are not a member of room {room_id!r}",
            hint="Redeem an invite for this room first.",
            context={"room": room_id},
        )


def _require_role(claims: TokenClaims, required: RoomRole, *, room: str) -> None:
    if not role_at_least(claims.role, required):
        raise NotAMemberError(
            f"this action needs the {required} role; you have {claims.role}",
            hint="Ask a room owner to elevate you, or mint a new invite at the higher role.",
            context={"room": room, "required": str(required), "actual": str(claims.role)},
        )


async def _authenticate_ws(
    websocket: WebSocket,
    state: AppState,
    room: str,
) -> TokenClaims | None:
    """Authenticate a socket, by handshake token or by first frame.

    Returns ``None`` after closing the socket if authentication failed, so
    callers can simply ``return`` on it without another branch.
    """
    token, source = _token_from_handshake(websocket)
    if token is None:
        token = await _token_from_first_frame(websocket, state)
        source = "frame"
        if token is None:
            return None

    try:
        claims = verify_token(state.settings, token, expected_room=room)
    except OpenBurrowError as exc:
        state.metrics.auth_failures.labels(reason=f"ws_{source}_token").inc()
        await _ws_close(websocket, WS_AUTH_REQUIRED, exc.message)
        return None

    # Logged, because a token in the query string is the one that ends up in
    # access logs and browser history. Seeing `via=query` is how an operator
    # finds out a client is doing it the leaky way.
    log.info("relay.ws_authenticated", room=room, subject=claims.subject, via=source)
    return claims


def _token_from_handshake(websocket: WebSocket) -> tuple[str | None, str]:
    """Look for a token in the handshake, and say where it came from.

    The query parameter is supported but not preferred: a browser cannot set
    headers on a WebSocket handshake, so clients reach for it, and it puts a
    credential in the request line where access logs and browser history can see
    it. The first-frame path exists so that the convenient option is not the only
    option.
    """
    token = websocket.query_params.get("token")
    if token:
        return token, "query"
    return extract_bearer(websocket.headers.get("authorization")), "header"


async def _token_from_first_frame(websocket: WebSocket, state: AppState) -> str | None:
    """Wait briefly for the client's auth frame, closing the socket on failure.

    The deadline matters because an unauthenticated socket is a resource anyone
    can allocate, and it is enforced with ``wait_for`` rather than by trusting
    the client to send something.
    """
    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=AUTH_DEADLINE_S)
    except TimeoutError:
        state.metrics.auth_failures.labels(reason="ws_timeout").inc()
        await _ws_close(
            websocket, WS_AUTH_TIMEOUT, f"no authentication frame within {AUTH_DEADLINE_S:g}s"
        )
        return None
    except WebSocketDisconnect:
        return None
    except Exception:
        state.metrics.auth_failures.labels(reason="ws_bad_first_frame").inc()
        await _ws_close(websocket, WS_AUTH_REQUIRED, "expected a JSON auth frame first")
        return None

    try:
        message = _decode(raw)
    except ValueError as exc:
        await _ws_close(websocket, WS_AUTH_REQUIRED, f"auth frame is not JSON: {exc}")
        return None

    if message.get("type") != "auth":
        await _ws_close(
            websocket,
            WS_AUTH_REQUIRED,
            'expected {"type":"auth","token":"..."} as the first frame',
        )
        return None

    return str(message.get("token") or "")


async def _ws_close(websocket: WebSocket, code: int, reason: str) -> None:
    """Close a socket, tolerating one that is already gone."""
    if websocket.client_state is WebSocketState.DISCONNECTED:
        return
    with contextlib.suppress(Exception):
        await websocket.close(code=code, reason=reason[:120])


def _decode(raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"frame is not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("frame must be a JSON object")
    return payload


def _parse_since(since: str | None) -> dict[str, int]:
    """Parse ``repo:seq,repo:seq`` into a resume map.

    Silently skipping malformed pairs would be wrong: a client that typo'd a repo
    would get events it already has, or worse, miss the ones it asked for. A
    malformed pair is a 400.
    """
    if not since:
        return {}
    parsed: dict[str, int] = {}
    for raw_pair in since.split(","):
        pair = raw_pair.strip()
        if not pair:
            continue
        if ":" not in pair:
            raise BadRequestError(
                f"could not parse `since` entry {pair!r}",
                hint="Use repo:seq pairs separated by commas, e.g. github.com/acme/burrow:42",
                context={"entry": pair},
            )
        repo, _, seq = pair.rpartition(":")
        try:
            parsed[repo] = int(seq)
        except ValueError as exc:
            raise BadRequestError(
                f"could not parse sequence number in {pair!r}",
                hint="The sequence must be an integer.",
                context={"entry": pair},
            ) from exc
    return parsed


async def _fan_out(
    state: AppState,
    room_id: str,
    result: AppendResult,
    *,
    exclude: str | None = None,
) -> None:
    """Deliver the events that were actually stored to the room's event sockets.

    Driven by ``result.accepted_events`` rather than by the request body. The
    accepted set is *not* a prefix of the input — validation rejects scattered
    entries and the database drops duplicates — so fanning out the raw input
    would deliver events the relay refused to store, and would deliver
    duplicates to clients that already have them.
    """
    if result.duplicates:
        log.info("relay.fan_out_skipped_duplicates", room=room_id, duplicates=result.duplicates)

    for row in result.accepted_events:
        occurred = row.get("occurred_at")
        # `hasattr` does not narrow `Any | None`, and the None case is
        # real: a row whose timestamp was never written.
        occurred_at = occurred
        if occurred is not None and hasattr(occurred, "isoformat"):
            occurred_at = occurred.isoformat()
        state.hub.publish(
            room_id,
            {
                "type": "event",
                "origin_repo": row["origin_repo"],
                "origin_seq": row["origin_seq"],
                "event_type": row["event_type"],
                "session_id": row.get("session_id"),
                "lane_id": row.get("lane_id"),
                "summary": row.get("summary"),
                "payload": row.get("payload") or {},
                "occurred_at": occurred_at,
            },
            kind="events",
            exclude=exclude,
            origin_repo=str(row["origin_repo"]),
            origin_seq=int(row["origin_seq"]),
        )


def _now_iso() -> str:

    return datetime.now(UTC).isoformat()


__all__ = ["AUTH_DEADLINE_S", "router"]
