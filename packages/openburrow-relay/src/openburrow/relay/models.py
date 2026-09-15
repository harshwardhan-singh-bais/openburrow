"""Relay persistence.

Postgres via SQLModel/SQLAlchemy-async. The relay is the one component that must
survive a restart and serve more than one host, which is why SQLite is refused at
config time rather than silently supported.

Three constraints in here carry design weight rather than being hygiene:

``uq_relay_event_origin``
    ``(room_id, origin_repo, origin_seq)`` is unique. This is the mechanism by
    which the relay can claim it never invents an event *and* dedupe a daemon
    that reconnects and replays. A replayed event is not an error and not a new
    event — it is the same event, and the database says so.

``relay_members`` keyed on ``(room_id, subject)``
    One identity per room. A member who reconnects is the same member, not a new
    one, so membership does not accumulate rows every time someone opens a tab.

``relay_doc_snapshots``
    One row per room holding the latest CRDT update blob. Deliberately not a log
    of updates: the relay is not the authority for the document, so keeping the
    history here would imply it is. See ADR 0007.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, Column, DateTime, Index, Integer, Text, UniqueConstraint
from sqlmodel import Field, SQLModel

from openburrow.core.models.ids import new_ulid

#: Relay-local id prefixes. Not added to core's table because these ids never
#: cross into a session, a lane, or the bus — they are relay concepts.
ROOM_PREFIX = "room"
MEMBER_PREFIX = "mbr"
INVITE_PREFIX = "inv"
EVENT_PREFIX = "rev"
DOC_PREFIX = "doc"


def new_relay_id(kind: str) -> str:
    return f"{kind}_{new_ulid()}"


class RoomRole(StrEnum):
    """What a member may do in a room.

    Three roles, ranked. There is deliberately no per-room permission bitfield:
    the operations the relay exposes are "read events", "read the doc", "write
    the doc", and "publish events", and three ranks cover every useful
    combination of them without inventing a policy language.
    """

    VIEWER = "viewer"
    MAINTAINER = "maintainer"
    OWNER = "owner"


ROLE_RANK: dict[RoomRole, int] = {
    RoomRole.VIEWER: 1,
    RoomRole.MAINTAINER: 2,
    RoomRole.OWNER: 3,
}


def role_at_least(role: RoomRole, required: RoomRole) -> bool:
    return ROLE_RANK.get(role, 0) >= ROLE_RANK.get(required, 0)


class Room(SQLModel, table=True):
    """A collaboration room. One per repo, not one per user.

    The isolation boundary is the repo because two people in the same repo are
    collaborators who must see each other's events, and one person with two
    repos has two unrelated conversations. Scoping rooms to users would get both
    of those backwards.
    """

    __tablename__ = "relay_rooms"
    __table_args__ = (Index("ix_relay_rooms_repo_slug", "repo_slug"),)

    id: str = Field(primary_key=True, max_length=64)
    #: A normalised repo identity, e.g. ``github.com/acme/burrow``. The thing
    #: every daemon can compute for itself without a registry.
    repo_slug: str = Field(max_length=300)
    name: str = Field(max_length=200)
    created_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    created_by: str = Field(max_length=200)
    retention_days: int = Field(default=30, sa_column=Column(Integer, nullable=False))
    archived: bool = Field(default=False)
    #: Free-form, but small. Anything large belongs in an event payload.
    metadata_: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column("metadata", JSON, nullable=False),
    )


class Member(SQLModel, table=True):
    """A person's membership of one room."""

    __tablename__ = "relay_members"
    __table_args__ = (
        UniqueConstraint("room_id", "subject", name="uq_relay_member_room_subject"),
        Index("ix_relay_members_subject", "subject"),
    )

    id: str = Field(primary_key=True, max_length=64)
    room_id: str = Field(foreign_key="relay_rooms.id", index=True, max_length=64)
    #: Stable identity — an email or a handle. Never a display name, which
    #: changes and is not unique.
    subject: str = Field(max_length=200)
    display_name: str = Field(max_length=200)
    role: str = Field(max_length=32)
    joined_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    last_seen_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )

    @property
    def room_role(self) -> RoomRole:
        """Parse the stored role, defaulting to the least privilege.

        An unrecognised role in the database is a bug or a downgrade artifact;
        either way the safe reading is ``VIEWER`` rather than raising, because
        raising here would lock a room out over a string.
        """
        try:
            return RoomRole(self.role)
        except ValueError:
            return RoomRole.VIEWER


class Invite(SQLModel, table=True):
    """A redeemable invitation.

    Only the SHA-256 of the token is stored. The token itself has 256 bits of
    entropy from ``secrets.token_urlsafe``, so a fast hash is the right tool —
    an offline attacker with the hash has nothing to brute-force. That is also
    why there is no password KDF anywhere in this service.
    """

    __tablename__ = "relay_invites"
    __table_args__ = (
        Index("ix_relay_invites_hash", "token_hash"),
        Index("ix_relay_invites_room", "room_id"),
    )

    id: str = Field(primary_key=True, max_length=64)
    room_id: str = Field(foreign_key="relay_rooms.id", max_length=64)
    token_hash: str = Field(max_length=64)
    role: str = Field(max_length=32)
    created_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    created_by: str = Field(max_length=200)
    expires_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    max_uses: int = Field(default=1, sa_column=Column(Integer, nullable=False))
    uses: int = Field(default=0, sa_column=Column(Integer, nullable=False))
    revoked: bool = Field(default=False)
    label: str = Field(default="", max_length=200)

    @property
    def room_role(self) -> RoomRole:
        """The role this invite confers.

        An unrecognised role downgrades to ``VIEWER`` rather than raising, for the
        same reason :attr:`Member.room_role` does: a bad string in one row should
        not be able to take a room down.
        """
        try:
            return RoomRole(self.role)
        except ValueError:
            return RoomRole.VIEWER


class RelayEvent(SQLModel, table=True):
    """A bus event, relayed.

    Every field that describes *what happened* comes from the originating
    daemon. ``received_at`` is the only thing the relay contributes, and it is
    named so that nobody mistakes it for a clock. Ordering is
    ``(origin_repo, origin_seq)``; see the unique constraint above.
    """

    __tablename__ = "relay_events"
    __table_args__ = (
        UniqueConstraint("room_id", "origin_repo", "origin_seq", name="uq_relay_event_origin"),
        Index("ix_relay_events_room_seq", "room_id", "origin_repo", "origin_seq"),
        Index("ix_relay_events_room_received", "room_id", "received_at"),
    )

    id: str = Field(primary_key=True, max_length=64)
    room_id: str = Field(foreign_key="relay_rooms.id", index=True, max_length=64)
    #: The daemon that owns this event. A repo slug, so a client can group by
    #: origin without knowing anything about daemon instances.
    origin_repo: str = Field(max_length=300)
    #: The originating daemon's ``bus_events.seq``. Unmodified.
    origin_seq: int = Field(sa_column=Column(Integer, nullable=False))
    event_type: str = Field(max_length=120)
    session_id: str | None = Field(default=None, max_length=64)
    lane_id: str | None = Field(default=None, max_length=64)
    summary: str = Field(default="", sa_column=Column(Text, nullable=False))
    payload: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSON, nullable=False),
    )
    #: When the originating daemon says it happened.
    occurred_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    #: When the relay stored it. Not a clock. Not used for ordering.
    received_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))


class DocSnapshot(SQLModel, table=True):
    """The latest CRDT update blob for a room's brain doc.

    One row, replaced in place. The relay stores bytes it does not decode and
    does not merge. ``version`` increments per accepted update so a client can
    tell "nothing new" from "I am out of date" without fetching the blob.
    """

    __tablename__ = "relay_doc_snapshots"

    room_id: str = Field(primary_key=True, foreign_key="relay_rooms.id", max_length=64)
    update_b64: str = Field(default="", sa_column=Column(Text, nullable=False))
    version: int = Field(default=0, sa_column=Column(Integer, nullable=False))
    updated_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    updated_by: str = Field(default="", max_length=200)


#: Import-time sanity check. SQLModel's ``table=True`` classes register in a
#: shared metadata object; if a name collides with a core model the failure
#: surfaces much later as a confusing DDL error.
RELAY_TABLES: tuple[str, ...] = (
    Room.__tablename__,
    Member.__tablename__,
    Invite.__tablename__,
    RelayEvent.__tablename__,
    DocSnapshot.__tablename__,
)


__all__ = [
    "DOC_PREFIX",
    "EVENT_PREFIX",
    "INVITE_PREFIX",
    "MEMBER_PREFIX",
    "RELAY_TABLES",
    "ROLE_RANK",
    "ROOM_PREFIX",
    "DocSnapshot",
    "Invite",
    "Member",
    "RelayEvent",
    "Room",
    "RoomRole",
    "new_relay_id",
    "role_at_least",
]
