"""Identifier generation.

OpenBurrow uses **ULID** rather than UUID4 for every primary key, for one
practical reason: ULIDs are lexicographically sortable by creation time. That
means the append-only bus log sorts correctly with a plain ``ORDER BY id``, and
a human reading ``burrow logs`` sees events in the order they happened without
any secondary timestamp index.

IDs are prefixed by type (``sess_``, ``lane_``, ``task_`` …) so a stray
identifier in a log line is self-describing. The prefix is part of the string,
which costs a few bytes and saves a lot of confusion.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime

_PREFIXES: dict[str, str] = {
    "session": "sess",
    "lane": "lane",
    "task": "task",
    "message": "msg",
    "claim": "claim",
    "plan": "plan",
    "step": "step",
    "brain": "brain",
    "lesson": "les",
    "delegation": "dlg",
    "audit": "aud",
    "approval": "appr",
    "checkpoint": "ckpt",
    "reel": "reel",
    "policy": "pol",
    "negotiation": "nego",
    "peer": "peer",
    "tenant": "tnt",
    "workspace": "ws",
    "hook": "hook",
}

# Crockford base32, excluding I, L, O, U to avoid visual ambiguity.
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _encode(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        value, remainder = divmod(value, 32)
        chars.append(_ALPHABET[remainder])
    return "".join(reversed(chars))


def new_ulid(*, timestamp_ms: int | None = None) -> str:
    """Generate a 26-character Crockford-base32 ULID.

    Layout: 48-bit millisecond timestamp + 80 bits of randomness. Monotonicity
    within a single millisecond is not guaranteed across processes — that is
    what the SQLite ``created_at`` column is for. It is guaranteed to *sort*
    correctly at millisecond granularity, which is all the bus log needs.
    """
    ts = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
    randomness = int.from_bytes(os.urandom(10), "big")
    return _encode(ts, 10) + _encode(randomness, 16)


def new_id(kind: str, *, timestamp_ms: int | None = None) -> str:
    """Generate a prefixed identifier, e.g. ``sess_01JBX...``.

    Unknown ``kind`` values raise, because a typo'd kind that silently produces
    an unprefixed id is the kind of bug that surfaces three weeks later in a
    database query.
    """
    try:
        prefix = _PREFIXES[kind]
    except KeyError as exc:
        raise ValueError(
            f"unknown id kind {kind!r}; known kinds: {', '.join(sorted(_PREFIXES))}"
        ) from exc
    return f"{prefix}_{new_ulid(timestamp_ms=timestamp_ms)}"


def ulid_to_datetime(value: str) -> datetime:
    """Recover the creation timestamp embedded in a ULID.

    Useful for `burrow session list` which wants a timestamp without a join, and
    for detecting clock skew when a ULID's embedded time disagrees with its
    stored ``created_at``.
    """
    raw = value.split("_", 1)[1] if "_" in value else value
    if len(raw) != 26:
        raise ValueError(f"not a ULID: {value!r}")
    millis = 0
    for char in raw[:10]:
        index = _ALPHABET.find(char)
        if index < 0:
            raise ValueError(f"invalid base32 character {char!r} in ULID {value!r}")
        millis = millis * 32 + index
    return datetime.fromtimestamp(millis / 1000, tz=UTC)


def is_valid_id(value: str, kind: str | None = None) -> bool:
    """Cheap structural check — no database round trip."""
    if not isinstance(value, str) or "_" not in value:
        return False
    prefix, _, body = value.partition("_")
    if len(body) != 26 or not all(char in _ALPHABET for char in body):
        return False
    if kind is not None:
        return _PREFIXES.get(kind) == prefix
    return prefix in set(_PREFIXES.values())


def id_kind(value: str) -> str | None:
    """Reverse lookup: ``"sess_01JBX..."`` -> ``"session"``."""
    prefix = value.split("_", 1)[0]
    return next((kind for kind, p in _PREFIXES.items() if p == prefix), None)


__all__ = [
    "id_kind",
    "is_valid_id",
    "new_id",
    "new_ulid",
    "ulid_to_datetime",
]
