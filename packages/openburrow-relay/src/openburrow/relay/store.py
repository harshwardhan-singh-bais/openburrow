"""Relay persistence.

Postgres through SQLAlchemy-async. Every method here is honest about failure: a
query that cannot run returns a result object saying so, or raises a
:class:`~openburrow.relay.errors.StorageUnavailableError`. Nothing returns an
empty list to mean "the database is down", because an empty event tail and an
unreachable database look identical to a client and only one of them is
recoverable by retrying.

Two things in here are worth reading before changing anything:

**``append_events`` dedupes on ``(room, origin_repo, origin_seq)``.** A daemon
that reconnects and replays its backlog is the normal case, not an error case.
``ON CONFLICT DO NOTHING`` makes replay free, and the returned counts
distinguish "stored" from "already had it" so the caller can report honestly.

**``tail_events`` resumes per origin.** There is no global sequence number,
because the relay does not assign one. A client resuming after a gap says what
it has *per origin repo*, which is the only question with a well-defined answer.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import case, delete, func, select as sa_select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

from openburrow.core.logging import get_logger
from openburrow.relay.config import RelaySettings
from openburrow.relay.errors import (
    InviteInvalidError,
    RoomNotFoundError,
    StorageUnavailableError,
)
from openburrow.relay.metrics import METRICS, RelayMetrics
from openburrow.relay.models import (
    EVENT_PREFIX,
    INVITE_PREFIX,
    MEMBER_PREFIX,
    ROOM_PREFIX,
    DocSnapshot,
    Invite,
    Member,
    RelayEvent,
    Room,
    RoomRole,
    new_relay_id,
    role_at_least,
)
from openburrow.relay.security import (
    check_invite_usable,
    hash_invite,
    invite_lookup_hash,
    new_invite_token,
)

log = get_logger(__name__)

#: A single bus event's payload cap. Anything larger is a bug or an attempt to
#: use the relay as a file store, and both should be refused with a reason
#: rather than stored.
MAX_EVENT_PAYLOAD_BYTES = 128 * 1024

#: Hard cap on events accepted in one publish. A daemon replaying a long backlog
#: is chunked by the client, not absorbed in one transaction.
MAX_EVENTS_PER_APPEND = 500

#: Default tail page size and its ceiling.
DEFAULT_TAIL_LIMIT = 200
MAX_TAIL_LIMIT = 2000

#: The relay's tables, in dependency order. Used by schema creation and by the
#: readiness probe, which must agree on what "the schema is present" means.
EXPECTED_TABLES: tuple[str, ...] = (
    "relay_rooms",
    "relay_members",
    "relay_invites",
    "relay_events",
    "relay_doc_snapshots",
)


@dataclass(slots=True)
class AppendResult:
    """What happened to a batch of events.

    ``accepted_events`` holds the rows that were actually inserted, in the order
    they were inserted. The fan-out needs exactly this list and nothing looser:
    the accepted events are *not* a prefix of the input, because validation
    rejects scattered entries, so slicing the input by ``accepted`` would deliver
    events the relay refused to store — including duplicates it had already
    seen. Reporting the precise set is the only way the fan-out and the database
    can be guaranteed to agree.
    """

    accepted: int = 0
    duplicates: int = 0
    rejected: list[dict[str, Any]] = field(default_factory=list)
    accepted_events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.accepted + self.duplicates + len(self.rejected)

    @property
    def ok(self) -> bool:
        return not self.rejected

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"accepted": self.accepted, "duplicates": self.duplicates}
        if self.rejected:
            payload["rejected"] = self.rejected
        return payload


def redact_url(url: str) -> str:
    """Strip the password out of a database URL for logs.

    SQLAlchemy's own ``repr`` on an engine already hides it, but the URL travels
    through several log sites in this module and one of them will eventually be
    a ``repr`` someone added in a hurry.
    """
    if "://" not in url or "@" not in url:
        return url
    scheme, rest = url.split("://", 1)
    creds, host = rest.rsplit("@", 1)
    user = creds.split(":", 1)[0]
    return f"{scheme}://{user}:***@{host}"


class RelayStore:
    """Async data access. One instance per process."""

    def __init__(
        self,
        settings: RelaySettings,
        *,
        metrics: RelayMetrics | None = None,
    ) -> None:
        self.settings = settings
        self.metrics = metrics or METRICS
        self._engine: AsyncEngine | None = None
        self._sessionmaker: async_sessionmaker[AsyncSession] | None = None

    # --- lifecycle --------------------------------------------------------
    async def connect(self) -> None:
        if self._engine is not None:
            return
        pool_size = max(1, self.settings.db_pool_min)
        overflow = max(0, self.settings.db_pool_max - pool_size)
        self._engine = create_async_engine(
            self.settings.async_db_url,
            pool_size=pool_size,
            max_overflow=overflow,
            # Without pre_ping, a connection dropped by a database restart is
            # handed to a request and fails there instead of here.
            pool_pre_ping=True,
            future=True,
        )
        self._sessionmaker = async_sessionmaker(
            self._engine, class_=AsyncSession, expire_on_commit=False
        )
        log.info("relay.store_connected", url=redact_url(self.settings.db_url))

    async def close(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._sessionmaker = None

    def session(self) -> AsyncSession:
        if self._sessionmaker is None:
            raise StorageUnavailableError(
                "the relay store is not connected",
                hint="Call connect() during application startup.",
            )
        return self._sessionmaker()

    async def ensure_schema(self) -> None:
        """Create the relay's tables if they are missing.

        Only the relay's five tables, listed explicitly. ``SQLModel.metadata`` is
        process-global and also contains the core session/lane/bus tables, so an
        unqualified ``create_all`` would quietly create the local SQLite schema
        inside the relay's Postgres database — which then makes it ambiguous
        which database is authoritative.

        This is not a migration system. A relay deployment that needs schema
        changes wants Alembic; this exists so a fresh database works.
        """
        if self._engine is None:
            raise StorageUnavailableError("cannot create schema before connect()")
        tables = [
            Room.__table__,
            Member.__table__,
            Invite.__table__,
            RelayEvent.__table__,
            DocSnapshot.__table__,
        ]
        async with self._engine.begin() as conn:
            await conn.run_sync(
                lambda sync_conn: SQLModel.metadata.create_all(sync_conn, tables=tables)
            )
        log.info("relay.schema_ready", tables=[t.name for t in tables])

    async def healthcheck(self) -> dict[str, Any]:
        """Report whether the database is usable.

        Returns rather than raises: this is called by ``/readyz``, where a raised
        exception would be rendered as a 500 by the framework and lose the
        detail that the caller needs to diagnose it.
        """
        if self._engine is None:
            return {
                "ok": False,
                "error": "store not connected",
                "url": redact_url(self.settings.db_url),
            }
        try:
            async with self.session() as session:
                # A raw connection, not `session.execute` — SQLModel's async
                # session warns on the latter because it returns Rows rather than
                # model instances, and these two queries want neither.
                connection = await session.connection()
                await connection.execute(text("SELECT 1"))
                # A reachable database with no tables is a different failure from
                # an unreachable one, and the distinction is the whole point of a
                # readiness probe.
                missing = await self._missing_tables(connection)
            if missing:
                return {
                    "ok": False,
                    "error": "schema is missing",
                    "missing_tables": missing,
                    "hint": (
                        "Start the relay once with OPENBURROW_RELAY_AUTO_SCHEMA unset (it defaults "
                        "to on) to create the tables, or run your migrations."
                    ),
                }
            return {"ok": True, "url": redact_url(self.settings.db_url)}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "url": redact_url(self.settings.db_url)}

    async def _missing_tables(self, connection: AsyncConnection) -> list[str]:
        """Which relay tables are absent from the public schema.

        ``information_schema`` rather than ``pg_tables``: the former is standard
        SQL and works against any Postgres-compatible service, including the
        managed ones that restrict catalog access.
        """
        result = await connection.execute(
            text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
        )
        present = {row[0] for row in result.all()}
        return [name for name in EXPECTED_TABLES if name not in present]

    # --- rooms ------------------------------------------------------------
    async def create_room(
        self,
        *,
        repo_slug: str,
        name: str,
        created_by: str,
        retention_days: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Room:
        room = Room(
            id=new_relay_id(ROOM_PREFIX),
            repo_slug=repo_slug,
            name=name,
            created_at=datetime.now(UTC),
            created_by=created_by,
            retention_days=self.settings.retention_days
            if retention_days is None
            else retention_days,
            metadata_=metadata or {},
        )
        async with self.session() as session:
            session.add(room)
            await session.commit()
            await session.refresh(room)
        log.info("relay.room_created", room=room.id, repo_slug=repo_slug, by=created_by)
        return room

    async def get_room(self, room_id: str) -> Room | None:
        async with self.session() as session:
            return await session.get(Room, room_id)

    async def require_room(self, room_id: str) -> Room:
        room = await self.get_room(room_id)
        if room is None:
            raise RoomNotFoundError(
                f"no room {room_id!r}",
                hint="Check the room id, or ask an owner to create it.",
                context={"room": room_id},
            )
        if room.archived:
            raise RoomNotFoundError(
                f"room {room_id!r} is archived",
                hint="Archived rooms are read-only and refuse new connections.",
                context={"room": room_id},
            )
        return room

    async def find_room_by_slug(self, repo_slug: str) -> Room | None:
        async with self.session() as session:
            result = await session.exec(
                select(Room).where(Room.repo_slug == repo_slug).order_by(Room.created_at.desc())
            )
            return result.first()

    async def list_rooms_for(self, subject: str) -> list[Room]:
        """Rooms a member belongs to. Driven by the membership table, not a flag."""
        async with self.session() as session:
            result = await session.exec(
                select(Room)
                .join(Member, Member.room_id == Room.id)
                .where(Member.subject == subject)
                .order_by(Room.created_at.desc())
            )
            return list(result.all())

    async def set_room_archived(self, room_id: str, archived: bool) -> bool:
        async with self.session() as session:
            room = await session.get(Room, room_id)
            if room is None:
                return False
            room.archived = archived
            session.add(room)
            await session.commit()
        return True

    # --- members ----------------------------------------------------------
    async def upsert_member(
        self,
        *,
        room_id: str,
        subject: str,
        display_name: str,
        role: RoomRole,
    ) -> tuple[Member, bool]:
        """Add a member, or update an existing one. Returns ``(member, created)``.

        **Role changes only ever elevate.** Re-redeeming a viewer invite must not
        demote an owner, and the sequence "owner generates a viewer invite for a
        colleague, then clicks it themselves to check it works" is common enough
        that getting this wrong would be a routine way to lock a room out. A
        deliberate demotion is an owner action, not a side effect of a token.
        """
        now = datetime.now(UTC)
        async with self.session() as session:
            result = await session.exec(
                select(Member).where(Member.room_id == room_id, Member.subject == subject)
            )
            existing = result.first()
            if existing is not None:
                existing.display_name = display_name or existing.display_name
                existing.last_seen_at = now
                current = existing.room_role
                if role_at_least(role, current) and role != current:
                    existing.role = str(role)
                    log.info(
                        "relay.member_elevated",
                        room=room_id,
                        member=existing.id,
                        was=str(current),
                        now=str(role),
                    )
                session.add(existing)
                await session.commit()
                await session.refresh(existing)
                return existing, False

            member = Member(
                id=new_relay_id(MEMBER_PREFIX),
                room_id=room_id,
                subject=subject,
                display_name=display_name or subject,
                role=str(role),
                joined_at=now,
                last_seen_at=now,
            )
            session.add(member)
            await session.commit()
            await session.refresh(member)
        log.info("relay.member_joined", room=room_id, member=member.id, role=str(role))
        return member, True

    async def get_member(self, room_id: str, subject: str) -> Member | None:
        async with self.session() as session:
            result = await session.exec(
                select(Member).where(Member.room_id == room_id, Member.subject == subject)
            )
            return result.first()

    async def list_members(self, room_id: str) -> list[Member]:
        async with self.session() as session:
            result = await session.exec(
                select(Member).where(Member.room_id == room_id).order_by(Member.joined_at)
            )
            return list(result.all())

    async def touch_member(self, member_id: str) -> None:
        """Record that a member was seen. Best-effort; failure is not an error."""
        try:
            async with self.session() as session:
                member = await session.get(Member, member_id)
                if member is not None:
                    member.last_seen_at = datetime.now(UTC)
                    session.add(member)
                    await session.commit()
        except Exception as exc:
            log.debug("relay.touch_member_failed", member=member_id, error=str(exc))

    # --- invites ----------------------------------------------------------
    async def create_invite(
        self,
        *,
        room_id: str,
        role: RoomRole,
        created_by: str,
        days: int = 7,
        max_uses: int = 1,
        label: str = "",
    ) -> tuple[str, Invite]:
        """Mint an invite. The plaintext token is returned once and never stored."""
        token = new_invite_token()
        invite = Invite(
            id=new_relay_id(INVITE_PREFIX),
            room_id=room_id,
            token_hash=hash_invite(token),
            role=str(role),
            created_at=datetime.now(UTC),
            created_by=created_by,
            expires_at=datetime.now(UTC) + timedelta(days=max(1, days)),
            max_uses=max(1, max_uses),
            label=label,
        )
        async with self.session() as session:
            session.add(invite)
            await session.commit()
            await session.refresh(invite)
        log.info(
            "relay.invite_created",
            room=room_id,
            invite=invite.id,
            role=str(role),
            max_uses=invite.max_uses,
        )
        return token, invite

    async def redeem_invite(
        self,
        *,
        token: str,
        subject: str,
        display_name: str = "",
    ) -> tuple[Room, Member, Invite]:
        """Exchange an invite token for membership.

        The usage counter is incremented in the same transaction that checks it,
        with a row lock, so two simultaneous redemptions of a single-use invite
        cannot both succeed. Doing the check in Python and the increment in SQL
        would leave exactly that window.
        """
        digest = invite_lookup_hash(token)
        async with self.session() as session:
            result = await session.exec(
                select(Invite).where(Invite.token_hash == digest).with_for_update()
            )
            invite = result.first()
            if invite is None:
                self.metrics.auth_failures.labels(reason="invite_not_found").inc()
                raise InviteInvalidError(
                    "that invite is not valid",
                    hint="Check for a truncated paste. Invite tokens are long and case-sensitive.",
                )

            check_invite_usable(
                revoked=invite.revoked,
                uses=invite.uses,
                max_uses=invite.max_uses,
                expires=invite.expires_at,
            )

            room = await session.get(Room, invite.room_id)
            if room is None:
                raise RoomNotFoundError(
                    "the invite points at a room that no longer exists",
                    hint="Ask an owner to create a new invite.",
                    context={"room": invite.room_id},
                )

            invite.uses += 1
            session.add(invite)
            await session.commit()

        # Membership is a separate transaction on purpose: a failure to add the
        # member must not roll back the usage increment, or a single-use invite
        # becomes infinitely reusable whenever member creation fails.
        member, _created = await self.upsert_member(
            room_id=room.id,
            subject=subject,
            display_name=display_name or subject,
            role=invite.room_role,
        )
        return room, member, invite

    async def list_invites(self, room_id: str) -> list[Invite]:
        async with self.session() as session:
            result = await session.exec(
                select(Invite).where(Invite.room_id == room_id).order_by(Invite.created_at.desc())
            )
            return list(result.all())

    async def revoke_invite(self, invite_id: str) -> bool:
        async with self.session() as session:
            invite = await session.get(Invite, invite_id)
            if invite is None:
                return False
            invite.revoked = True
            session.add(invite)
            await session.commit()
        return True

    # --- events -----------------------------------------------------------
    async def append_events(
        self,
        room_id: str,
        events: Sequence[dict[str, Any]],
    ) -> AppendResult:
        """Store relayed events, skipping any the relay already has.

        Validation happens here rather than in the route so that the rules apply
        to every caller — the WebSocket ingest path, an HTTP backfill, and a
        future import tool all get the same answer about what an event is.
        """
        result = AppendResult()
        if not events:
            return result
        if len(events) > MAX_EVENTS_PER_APPEND:
            result.rejected.append(
                {
                    "reason": "batch_too_large",
                    "limit": MAX_EVENTS_PER_APPEND,
                    "got": len(events),
                }
            )
            return result

        rows: list[dict[str, Any]] = []
        for raw in events:
            normalised, error = _normalise_event(room_id, raw)
            if error is not None:
                result.rejected.append(error)
                continue
            rows.append(normalised)

        if not rows:
            return result

        stmt = (
            pg_insert(RelayEvent.__table__)
            .values(rows)
            # The whole point: a reconnect-and-replay is free.
            .on_conflict_do_nothing(constraint="uq_relay_event_origin")
            .returning(RelayEvent.__table__.c.id)
        )
        async with self.session() as session:
            inserted = await session.execute(stmt)
            inserted_ids = {str(row[0]) for row in inserted.fetchall()}
            await session.commit()

        result.accepted_events = [row for row in rows if row["id"] in inserted_ids]
        result.accepted = len(result.accepted_events)
        result.duplicates = len(rows) - result.accepted
        self.metrics.events_received.inc(result.accepted)
        if result.duplicates:
            self.metrics.events_duplicate.inc(result.duplicates)
        return result

    async def tail_events(
        self,
        room_id: str,
        *,
        since: dict[str, int] | None = None,
        limit: int = DEFAULT_TAIL_LIMIT,
        session_id: str | None = None,
        event_types: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return events a client has not seen, per origin repo.

        ``since`` maps ``origin_repo -> highest origin_seq already seen``. Repos
        absent from the map are treated as "seen nothing", which is the correct
        reading: a client that has never heard from a daemon cannot have seen any
        of its events.

        The SQL is a ``CASE`` rather than a chain of ``OR`` clauses for exactly
        that reason. ``OR`` would silently drop events from any origin not
        mentioned in ``since`` — a client that knew about repo A and not repo B
        would never be told repo B existed.
        """
        limit = max(1, min(limit, MAX_TAIL_LIMIT))
        stmt = select(RelayEvent).where(RelayEvent.room_id == room_id)

        since = since or {}
        if since:
            thresholds = [(RelayEvent.origin_repo == repo, seq) for repo, seq in since.items()]
            stmt = stmt.where(
                RelayEvent.origin_seq > case(*thresholds, value=RelayEvent.origin_repo, else_=0)
            )

        if session_id:
            stmt = stmt.where(RelayEvent.session_id == session_id)
        if event_types:
            stmt = stmt.where(RelayEvent.event_type.in_(list(event_types)))

        # Ordered per origin. There is deliberately no global ordering to offer,
        # and sorting by `received_at` would imply one.
        stmt = stmt.order_by(RelayEvent.origin_repo, RelayEvent.origin_seq).limit(limit)

        async with self.session() as session:
            result = await session.exec(stmt)
            return [_event_to_dict(e) for e in result.all()]

    async def latest_seqs(self, room_id: str) -> dict[str, int]:
        """Highest ``origin_seq`` stored per origin repo.

        What a client asks for on first connect so it knows how far behind it is,
        and what a lag notice points at.
        """
        stmt = (
            sa_select(RelayEvent.origin_repo, func.max(RelayEvent.origin_seq))
            .where(RelayEvent.room_id == room_id)
            .group_by(RelayEvent.origin_repo)
        )
        async with self.session() as session:
            result = await session.execute(stmt)
            return {str(row[0]): int(row[1]) for row in result.all()}

    async def event_count(self, room_id: str) -> int:
        stmt = (
            sa_select(func.count())
            .select_from(RelayEvent.__table__)
            .where(RelayEvent.__table__.c.room_id == room_id)
        )
        async with self.session() as session:
            result = await session.execute(stmt)
            return int(result.scalar_one())

    # --- documents --------------------------------------------------------
    async def save_doc_update(
        self,
        room_id: str,
        *,
        update_b64: str,
        updated_by: str,
    ) -> int:
        """Replace the room's stored CRDT blob. Returns the new version.

        The blob is opaque to the relay. It is never decoded, never merged, and
        never inspected — the relay does not know what a Yjs update contains and
        should not. Merging belongs to the clients, which is what makes the doc
        safe to relay without the relay being able to corrupt it.
        """
        async with self.session() as session:
            snapshot = await session.get(DocSnapshot, room_id)
            if snapshot is None:
                snapshot = DocSnapshot(
                    room_id=room_id,
                    update_b64=update_b64,
                    version=1,
                    updated_at=datetime.now(UTC),
                    updated_by=updated_by,
                )
            else:
                snapshot.update_b64 = update_b64
                snapshot.version += 1
                snapshot.updated_at = datetime.now(UTC)
                snapshot.updated_by = updated_by
            session.add(snapshot)
            await session.commit()
            return snapshot.version

    async def load_doc(self, room_id: str) -> DocSnapshot | None:
        async with self.session() as session:
            return await session.get(DocSnapshot, room_id)

    # --- maintenance ------------------------------------------------------
    async def prune(self, *, now: datetime | None = None) -> int:
        """Delete events past their room's retention window. Returns rows removed.

        Per room, because ``retention_days`` is per room: a busy room and a quiet
        one have different ideas about how much history is worth keeping, and one
        global cutoff would impose the stricter of the two on both.
        """
        now = now or datetime.now(UTC)

        async with self.session() as session:
            rooms = await session.exec(select(Room))
            removed = 0
            for room in rooms.all():
                if room.retention_days <= 0:
                    continue
                cutoff = now - timedelta(days=room.retention_days)
                stmt = delete(RelayEvent.__table__).where(
                    RelayEvent.__table__.c.room_id == room.id,
                    RelayEvent.__table__.c.received_at < cutoff,
                )
                result = await session.execute(stmt)
                removed += int(result.rowcount or 0)
            await session.commit()
        if removed:
            log.info("relay.pruned", events=removed)
        return removed


def _normalise_event(  # noqa: PLR0911 - one early return per distinct rejection
    room_id: str, raw: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Validate and coerce one inbound event.

    Returns ``(row, None)`` or ``({}, error)``.

    The nine early returns are the point of the function: each names a different
    reason an event was refused, and each refusal carries the field it concerns so
    the caller can report which event in a batch failed and why. Collapsing them
    into a single exit would mean building the reason list as data, which reads
    worse and loses the ability to say "this one, because of that".

    Validation is deliberately strict about the fields that carry meaning and
    lenient about the rest: an unknown extra key in the payload is passed through
    untouched, because the relay has no business knowing what a payload contains.
    """
    origin_repo = str(raw.get("origin_repo") or "").strip()
    if not origin_repo:
        return {}, {"reason": "missing_origin_repo"}

    try:
        origin_seq = int(raw.get("origin_seq"))
    except (TypeError, ValueError):
        return {}, {"reason": "missing_origin_seq", "origin_repo": origin_repo}
    if origin_seq <= 0:
        # A zero or negative sequence is not an event the bus can produce, and
        # accepting it would break the resume arithmetic for every later query.
        return {}, {"reason": "non_positive_origin_seq", "origin_seq": origin_seq}

    event_type = str(raw.get("event_type") or "").strip()
    if not event_type:
        return {}, {"reason": "missing_event_type", "origin_repo": origin_repo}

    payload = raw.get("payload") or {}
    if not isinstance(payload, dict):
        return {}, {"reason": "payload_not_an_object", "origin_repo": origin_repo}

    try:
        encoded = json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8")
    except (TypeError, ValueError) as exc:
        return {}, {"reason": "payload_not_serialisable", "error": str(exc)}
    if len(encoded) > MAX_EVENT_PAYLOAD_BYTES:
        return {}, {
            "reason": "payload_too_large",
            "bytes": len(encoded),
            "limit": MAX_EVENT_PAYLOAD_BYTES,
            "origin_repo": origin_repo,
        }

    occurred_at = _coerce_datetime(raw.get("occurred_at"))
    if occurred_at is None:
        return {}, {"reason": "missing_or_invalid_occurred_at", "origin_repo": origin_repo}

    row = {
        "id": new_relay_id(EVENT_PREFIX),
        "room_id": room_id,
        "origin_repo": origin_repo,
        "origin_seq": origin_seq,
        "event_type": event_type,
        "session_id": _optional_str(raw.get("session_id")),
        "lane_id": _optional_str(raw.get("lane_id")),
        "summary": str(raw.get("summary") or "")[:2000],
        "payload": payload,
        "occurred_at": occurred_at,
        "received_at": datetime.now(UTC),
    }
    return row, None


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text_value = str(value).strip()
    return text_value or None


def _coerce_datetime(value: Any) -> datetime | None:
    """Parse an inbound timestamp, defaulting to naive-but-UTC aware.

    A naive ISO string is read as UTC rather than rejected. The alternative is
    that one daemon with a slightly different serialiser cannot publish at all,
    and ``occurred_at`` is not used for ordering — it is informational.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _event_to_dict(event: RelayEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "room": event.room_id,
        "origin_repo": event.origin_repo,
        "origin_seq": event.origin_seq,
        "event_type": event.event_type,
        "session_id": event.session_id,
        "lane_id": event.lane_id,
        "summary": event.summary,
        "payload": event.payload,
        "occurred_at": event.occurred_at.isoformat() if event.occurred_at else None,
        "received_at": event.received_at.isoformat() if event.received_at else None,
    }


__all__ = [
    "DEFAULT_TAIL_LIMIT",
    "MAX_EVENTS_PER_APPEND",
    "MAX_EVENT_PAYLOAD_BYTES",
    "MAX_TAIL_LIMIT",
    "AppendResult",
    "RelayStore",
    "redact_url",
]
