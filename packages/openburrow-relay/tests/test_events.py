"""Inbound event validation and resume-point parsing.

These are the two places where a caller's input becomes something the relay
stores or queries with, so both are tested directly rather than through the HTTP
surface. The functions are private, which is normally a reason not to test them —
but the alternative here is a Postgres dependency for logic that is entirely
about rejecting malformed input, and that is a worse trade.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from openburrow.relay.errors import BadRequestError
from openburrow.relay.routes import _parse_since
from openburrow.relay.store import (
    MAX_EVENT_PAYLOAD_BYTES,
    _normalise_event,
)

pytestmark = pytest.mark.unit

ROOM = "room_test"


def event(**overrides: object) -> dict:
    base = {
        "origin_repo": "github.com/acme/burrow",
        "origin_seq": 1,
        "event_type": "lane.completed",
        "session_id": "sess_01HQ0000000000000000000000",
        "lane_id": "lane_01HQ0000000000000000000000",
        "summary": "lane finished",
        "payload": {"exit_code": 0},
        "occurred_at": datetime.now(UTC).isoformat(),
    }
    base.update(overrides)
    return base


class TestRequiredFields:
    def test_a_well_formed_event_normalises(self) -> None:
        row, error = _normalise_event(ROOM, event())
        assert error is None
        assert row["room_id"] == ROOM
        assert row["origin_repo"] == "github.com/acme/burrow"
        assert row["origin_seq"] == 1
        assert row["id"].startswith("rev_")
        assert row["received_at"].tzinfo is not None

    @pytest.mark.parametrize(
        "missing",
        ["origin_repo", "origin_seq", "event_type", "occurred_at"],
    )
    def test_each_required_field_is_enforced(self, missing: str) -> None:
        payload = event()
        payload.pop(missing)
        _row, error = _normalise_event(ROOM, payload)
        assert error is not None
        assert missing in error["reason"]

    def test_blank_origin_repo_is_rejected(self) -> None:
        _row, error = _normalise_event(ROOM, event(origin_repo="   "))
        assert error == {"reason": "missing_origin_repo"}

    def test_blank_event_type_is_rejected(self) -> None:
        _row, error = _normalise_event(ROOM, event(event_type=""))
        assert error is not None
        assert error["reason"] == "missing_event_type"


class TestSequenceNumbers:
    def test_non_integer_sequence_is_rejected(self) -> None:
        _row, error = _normalise_event(ROOM, event(origin_seq="not-a-number"))
        assert error is not None
        assert error["reason"] == "missing_origin_seq"

    def test_missing_sequence_is_rejected(self) -> None:
        _row, error = _normalise_event(ROOM, event(origin_seq=None))
        assert error is not None
        assert error["reason"] == "missing_origin_seq"

    @pytest.mark.parametrize("seq", [0, -1, -999])
    def test_non_positive_sequence_is_rejected(self, seq: int) -> None:
        """Zero and negatives are refused because the resume arithmetic breaks.

        A client resuming says "give me everything above N". If an event could
        have N <= 0, an event stored at 0 would be excluded from every resume
        query — permanently invisible, and impossible to notice.
        """
        _row, error = _normalise_event(ROOM, event(origin_seq=seq))
        assert error is not None
        assert error["reason"] == "non_positive_origin_seq"
        assert error["origin_seq"] == seq

    def test_string_digits_are_coerced(self) -> None:
        # A JSON producer that stringifies integers is common and harmless.
        row, error = _normalise_event(ROOM, event(origin_seq="42"))
        assert error is None
        assert row["origin_seq"] == 42


class TestPayload:
    def test_non_object_payload_is_rejected(self) -> None:
        _row, error = _normalise_event(ROOM, event(payload=["not", "a", "dict"]))
        assert error is not None
        assert error["reason"] == "payload_not_an_object"

    def test_missing_payload_becomes_an_empty_object(self) -> None:
        row, error = _normalise_event(ROOM, event(payload=None))
        assert error is None
        assert row["payload"] == {}

    def test_oversized_payload_is_rejected_with_its_size(self) -> None:
        big = {"blob": "x" * (MAX_EVENT_PAYLOAD_BYTES + 1024)}
        _row, error = _normalise_event(ROOM, event(payload=big))
        assert error is not None
        assert error["reason"] == "payload_too_large"
        # The size and the limit are both reported, so the caller knows how much
        # to trim rather than guessing.
        assert error["bytes"] > error["limit"]

    def test_unknown_payload_keys_pass_through_untouched(self) -> None:
        """The relay has no business knowing what a payload contains.

        Rejecting unknown keys would make every future event type a relay change,
        which is exactly the coupling the relay exists to avoid.
        """
        row, error = _normalise_event(ROOM, event(payload={"whatever": {"nested": [1, 2, 3]}}))
        assert error is None
        assert row["payload"] == {"whatever": {"nested": [1, 2, 3]}}

    def test_unserialisable_payload_is_rejected(self) -> None:
        # `default=str` in the encoder absorbs most oddities (datetimes, UUIDs,
        # arbitrary objects), so reaching this branch takes something the encoder
        # genuinely cannot express: a cycle.
        circular: dict = {}
        circular["self"] = circular
        _row, error = _normalise_event(ROOM, event(payload=circular))
        assert error is not None
        assert error["reason"] == "payload_not_serialisable"


class TestOptionalFields:
    def test_absent_session_and_lane_become_none(self) -> None:
        payload = event()
        payload.pop("session_id")
        payload.pop("lane_id")
        row, error = _normalise_event(ROOM, payload)
        assert error is None
        assert row["session_id"] is None
        assert row["lane_id"] is None

    def test_blank_session_id_becomes_none_not_empty_string(self) -> None:
        # An empty string would be stored and then never match a filter on
        # `session_id IS NULL`, producing a row that is invisible to both queries.
        row, _error = _normalise_event(ROOM, event(session_id="   "))
        assert row["session_id"] is None

    def test_long_summary_is_truncated(self) -> None:
        row, _error = _normalise_event(ROOM, event(summary="x" * 5000))
        assert len(row["summary"]) == 2000


class TestTimestamps:
    def test_naive_iso_timestamp_is_read_as_utc(self) -> None:
        # A daemon with a slightly different serialiser should still be able to
        # publish. `occurred_at` is informational and is never used for ordering.
        row, error = _normalise_event(ROOM, event(occurred_at="2026-09-15T10:00:00"))
        assert error is None
        assert row["occurred_at"].tzinfo is not None

    def test_zulu_suffix_is_accepted(self) -> None:
        row, error = _normalise_event(ROOM, event(occurred_at="2026-09-15T10:00:00Z"))
        assert error is None
        assert row["occurred_at"].tzinfo is not None

    def test_unparseable_timestamp_is_rejected(self) -> None:
        _row, error = _normalise_event(ROOM, event(occurred_at="last tuesday"))
        assert error is not None
        assert error["reason"] == "missing_or_invalid_occurred_at"

    def test_datetime_object_passes_through(self) -> None:
        row, error = _normalise_event(ROOM, event(occurred_at=datetime.now(UTC)))
        assert error is None
        assert isinstance(row["occurred_at"], datetime)

    def test_received_at_is_the_relay_s_own_clock(self) -> None:
        """`received_at` must not be copied from the payload.

        It is the only timestamp the relay contributes, and it is deliberately
        named so nobody mistakes it for a clock. If it came from the caller, it
        would be attacker-controlled.
        """
        old = datetime.now(UTC) - timedelta(days=365)
        row, _error = _normalise_event(ROOM, event(occurred_at=old.isoformat()))
        assert row["received_at"] > row["occurred_at"]


class TestParseSince:
    def test_empty_input_yields_an_empty_map(self) -> None:
        assert _parse_since(None) == {}
        assert _parse_since("") == {}

    def test_single_pair(self) -> None:
        assert _parse_since("github.com/acme/burrow:42") == {"github.com/acme/burrow": 42}

    def test_multiple_pairs(self) -> None:
        assert _parse_since("repo/a:1,repo/b:2") == {"repo/a": 1, "repo/b": 2}

    def test_whitespace_is_tolerated(self) -> None:
        assert _parse_since(" repo/a:1 , repo/b:2 ") == {"repo/a": 1, "repo/b": 2}

    def test_colons_in_the_repo_are_handled(self) -> None:
        # rpartition, not split: a URL-shaped repo slug contains a colon itself.
        assert _parse_since("github.com:8443/acme/burrow:7") == {"github.com:8443/acme/burrow": 7}

    def test_trailing_empty_pair_is_ignored(self) -> None:
        assert _parse_since("repo/a:1,") == {"repo/a": 1}

    def test_missing_colon_is_a_400(self) -> None:
        with pytest.raises(BadRequestError) as excinfo:
            _parse_since("repo/a-42")
        assert excinfo.value.status_code == 400

    def test_non_integer_sequence_is_a_400(self) -> None:
        with pytest.raises(BadRequestError) as excinfo:
            _parse_since("repo/a:not-a-number")
        assert "sequence" in excinfo.value.message
        assert excinfo.value.status_code == 400
