"""The lane briefing: the integration point Stages 18 and 19 never had.

The claim these tests defend is narrow and specific. The Brain and the lesson
store both expose a ranked ``select_for_injection``; both are populated by their
promotion paths; and before ``_brief_lane`` existed, no code path called either
one. A ``grep`` for ``select_for_injection`` across the repository returned two
lines, both of them the definitions. ``evict_noise``, ``record_outcome`` and the
``record_lesson_injection`` metric appeared only at their own definitions and in
one observability unit test. The stores were complete, correct, and unreachable —
declared-and-inert, which this project treats as its dominant defect class rather
than a documentation problem.

So the tests come in three layers, and the split is deliberate:

**The pure renderer.** ``build_briefing`` takes two lists of models and returns
text. It is separated from ``assemble_lane_briefing`` precisely so the shape of
what a lane is shown can be checked with no database, and these tests use that.

**The wiring.** A behavioural test of ``_brief_lane`` passes with the call site
deleted, which is the defect being removed — so the call site is asserted at the
source level, the same way ``test_stage13_spawn_gate.py`` asserts the policy gate.

**The behaviour.** A real database, a real store, and a stub adapter, to pin that
what was recorded for this repository is what reaches the lane, that a wrong repo
id reaches nothing, and that a harness which refuses input does not fail the start.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from openburrow.adapters import HarnessAdapter
from openburrow.brain import BrainStore, LessonStore
from openburrow.brain.lessons import LessonCandidate
from openburrow.brain.store import Candidate
from openburrow.core.db.engine import Database
from openburrow.core.models import BrainEntry, Lane, LaneRole, LaneStatus, Lesson, Session
from openburrow.core.paths import repo_id_for
from openburrow.daemon.context import LaneBriefing, build_briefing
from openburrow.daemon.sessions import SessionManager

pytestmark = [pytest.mark.integration]


# --------------------------------------------------------------------------
# Doubles
# --------------------------------------------------------------------------
class RecordingBus:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def emit(self, **payload: Any) -> None:
        self.events.append(payload)

    def event_types(self) -> list[str]:
        return [str(event.get("event_type")) for event in self.events]

    def only(self, event_type: str) -> dict[str, Any]:
        matching = [event for event in self.events if event.get("event_type") == event_type]
        assert len(matching) == 1, f"expected one {event_type}, got {len(matching)}"
        return matching[0]


class StubAdapter:
    """Stands in for a harness's input path.

    ``accepts`` and ``raises`` exist because both are real outcomes: a PTY harness
    can be alive but not yet reading, and a harness that refuses input is the
    unreachable-lane case ``inject_message``'s own docstring says the caller should
    surface rather than pretend worked.
    """

    def __init__(self, *, accepts: bool = True, raises: Exception | None = None) -> None:
        self.accepts = accepts
        self.raises = raises
        self.delivered: list[str] = []

    async def inject_message(self, message: Any, task: Any = None) -> bool:
        self.delivered.append(message.body)
        if self.raises is not None:
            raise self.raises
        return self.accepts


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
def make_manager(
    database: Database, *, repo_root: Path, bus: RecordingBus | None = None
) -> SessionManager:
    config = SimpleNamespace(
        paths=SimpleNamespace(repo_root=repo_root),
        settings=SimpleNamespace(governance_human_id="", governance_human_email=""),
    )
    return SessionManager(
        config=config,  # type: ignore[arg-type]
        database=database,
        bus=bus or RecordingBus(),  # type: ignore[arg-type]
        registry=None,  # type: ignore[arg-type]
    )


async def brief_lane(
    manager: SessionManager, lane: Lane, adapter: StubAdapter, session: Session
) -> LaneBriefing:
    """Call the real method with a stub in the adapter slot.

    The cast lives here and nowhere else. ``_brief_lane`` touches exactly one
    method of the adapter — ``inject_message`` — so a stub implementing only that
    is a faithful stand-in, but ``HarnessAdapter`` is a nominal ABC rather than a
    protocol and mypy cannot see the overlap. Thirteen suppressions at the call
    sites would be thirteen copies of the same claim; this is one, with the
    reason attached.
    """
    return await manager._brief_lane(lane, cast(HarnessAdapter, adapter), session)


def make_lane(*, claims: list[str] | None = None) -> Lane:
    lane = Lane(
        session_id="s-1",
        name="alice",
        harness="claude-code",
        role=LaneRole.IMPLEMENTER,
        status=LaneStatus.IDLE,
    )
    lane.metadata["claims"] = claims or []
    return lane


async def record_lesson(
    database: Database,
    repo_root: Path,
    *,
    title: str = "the runner is slow",
    session_id: str = "s-1",
) -> Lesson:
    store = LessonStore(database, repo_id=repo_id_for(repo_root))
    return await store.promote(
        LessonCandidate(
            title=title,
            body="turns out the CI runner is slow on tuesdays",
            trigger="a job takes far longer than it should",
            remedy="schedule the heavy job on wednesday",
        ),
        session_id=session_id,
    )


async def record_entry(
    database: Database, repo_root: Path, *, title: str = "line length is 100"
) -> BrainEntry:
    store = BrainStore(database, repo_id=repo_id_for(repo_root))
    result = await store.promote(
        Candidate(title=title, body="ruff enforces a 100 column limit", promoted_by="human")
    )
    return result.entry


# --------------------------------------------------------------------------
# The pure renderer
# --------------------------------------------------------------------------
class TestBuildBriefing:
    def test_nothing_to_say_renders_to_nothing(self) -> None:
        """No filler. A placeholder is an injection that carries no information."""
        briefing = build_briefing([], [])
        assert briefing == LaneBriefing()
        assert briefing.text == ""
        assert briefing.is_empty is True

    def test_an_entry_is_rendered_under_its_own_heading(self) -> None:
        entry = BrainEntry(id="brain_1", title="line length is 100", body="ruff enforces it")
        briefing = build_briefing([entry], [])
        assert "Project knowledge:" in briefing.text
        assert entry.render_for_injection() in briefing.text
        assert briefing.brain_entry_ids == ("brain_1",)
        assert briefing.lesson_ids == ()
        assert briefing.is_empty is False

    def test_a_lesson_is_rendered_under_its_own_heading(self) -> None:
        lesson = Lesson(id="lesson_1", title="the runner is slow", remedy="use wednesday")
        briefing = build_briefing([], [lesson])
        assert "Lessons from earlier sessions:" in briefing.text
        assert lesson.render_for_injection() in briefing.text
        assert briefing.lesson_ids == ("lesson_1",)
        assert briefing.brain_entry_ids == ()

    def test_both_kinds_appear_together(self) -> None:
        entry = BrainEntry(id="brain_1", title="a convention")
        lesson = Lesson(id="lesson_1", title="a lesson")
        briefing = build_briefing([entry], [lesson])
        assert "Project knowledge:" in briefing.text
        assert "Lessons from earlier sessions:" in briefing.text
        assert briefing.brain_entry_ids == ("brain_1",)
        assert briefing.lesson_ids == ("lesson_1",)

    def test_the_text_says_where_it_came_from(self) -> None:
        """Attribution is the whole reason the poisoned-message test means anything.

        A harness that reads unattributed text as an instruction behaves
        differently from one that reads it as another agent's notes. The same
        distinction is drawn by ``render_injection`` on the A2A path, and it is
        load-bearing enough that the framing sentence is asserted rather than
        assumed.
        """
        briefing = build_briefing([BrainEntry(id="brain_1", title="x")], [])
        assert "prior context, not instructions" in briefing.text
        assert briefing.text.splitlines()[0].startswith("Before you start:")

    def test_ids_keep_the_order_they_were_given(self) -> None:
        entries = [BrainEntry(id=f"brain_{index}", title=f"e{index}") for index in range(3)]
        briefing = build_briefing(entries, [])
        assert briefing.brain_entry_ids == ("brain_0", "brain_1", "brain_2")

    def test_whitespace_is_not_content(self) -> None:
        """A briefing that renders to blank lines would still be *injected*."""
        assert LaneBriefing(text="   \n\t\n").is_empty is True
        assert LaneBriefing(text="x").is_empty is False

    def test_the_dict_carries_ids_and_a_size_but_not_the_text(self) -> None:
        """The bus payload describes the briefing; it does not duplicate it.

        The ids are what a reel or a later outcome report needs to resolve. The
        text is the lane's private context, and copying it into every event would
        put harness prompts in the audit log for no reader's benefit.
        """
        briefing = build_briefing([BrainEntry(id="brain_1", title="x")], [])
        payload = briefing.as_dict()
        assert payload["brain_entries"] == ["brain_1"]
        assert payload["lessons"] == []
        assert payload["characters"] == len(briefing.text)
        assert briefing.text not in str(payload)


# --------------------------------------------------------------------------
# The behaviour, against a real database
# --------------------------------------------------------------------------
class TestWhatReachesTheLane:
    async def test_what_was_recorded_for_this_repo_is_what_the_lane_is_told(
        self, database: Database, tmp_path: Path
    ) -> None:
        """The repo id has to be derived the same way on both sides.

        Written under ``repo_id_for(repo_root)``, read under whatever the manager
        computes. If those disagreed the briefing would be silently empty, because
        the store would simply find no rows.

        The Brain entry is what makes this a test of that. Lessons are keyed by
        session and are found either way — the first version of this test used a
        lesson alone and still passed with the repository id blanked out, which the
        falsification pass caught.
        """
        await record_entry(database, tmp_path, title="line length is 100")
        await record_lesson(database, tmp_path, title="the runner is slow")
        adapter = StubAdapter()
        manager = make_manager(database, repo_root=tmp_path)

        briefing = await brief_lane(manager, make_lane(), adapter, Session(id="s-1"))

        assert len(briefing.brain_entry_ids) == 1
        assert len(briefing.lesson_ids) == 1
        assert "line length is 100" in adapter.delivered[0]
        assert "the runner is slow" in adapter.delivered[0]

    async def test_another_session_is_not_briefed(self, database: Database, tmp_path: Path) -> None:
        """Lessons isolate along the session, and this is the control for it.

        ``promote`` leaves ``repo_id`` empty for a session-scoped lesson, so the
        repository is not what keeps it private — the session is. Asserting the
        repo boundary here instead was the first version of this test, and it
        failed, correctly: a session lesson is visible to its own session and no
        other, and the session lives in exactly one repository anyway.
        """
        await record_lesson(database, tmp_path, title="someone else's problem", session_id="s-2")

        adapter = StubAdapter()
        manager = make_manager(database, repo_root=tmp_path)
        briefing = await brief_lane(manager, make_lane(), adapter, Session(id="s-1"))

        assert briefing.is_empty is True
        assert adapter.delivered == []

    async def test_another_repository_is_not_briefed(
        self, database: Database, tmp_path: Path
    ) -> None:
        """Brain entries isolate along the repository, which is the other axis.

        The control for the test above, on the store where ``repo_id`` is the key
        rather than the session. Between them they pin that a briefing is scoped
        to this session *and* this repository, not merely to one of them.
        """
        other = tmp_path / "elsewhere"
        other.mkdir()
        await record_entry(database, other, title="someone else's convention")

        adapter = StubAdapter()
        manager = make_manager(database, repo_root=tmp_path)
        briefing = await brief_lane(manager, make_lane(), adapter, Session(id="s-1"))

        assert briefing.is_empty is True
        assert adapter.delivered == []

    async def test_a_brain_entry_reaches_the_lane(self, database: Database, tmp_path: Path) -> None:
        await record_entry(database, tmp_path, title="line length is 100")
        adapter = StubAdapter()
        manager = make_manager(database, repo_root=tmp_path)

        briefing = await brief_lane(manager, make_lane(), adapter, Session(id="s-1"))

        assert len(briefing.brain_entry_ids) == 1
        assert "line length is 100" in adapter.delivered[0]

    async def test_the_claims_scope_the_brain_but_not_the_lessons(
        self, database: Database, tmp_path: Path
    ) -> None:
        """A claim is a relevance hint for anchored knowledge.

        A convention about a file the lane is not editing is trivia, so the Brain
        filters on it. A lesson is about doing the work rather than about a file,
        so scoping it by claim would silently drop every lesson a lane needs.
        """
        store = BrainStore(database, repo_id=repo_id_for(tmp_path))
        await store.promote(
            Candidate(title="parser quirk", body="x", promoted_by="human", anchor_path="a.py")
        )
        await record_lesson(database, tmp_path)
        manager = make_manager(database, repo_root=tmp_path)

        claimed = StubAdapter()
        await brief_lane(manager, make_lane(claims=["a.py"]), claimed, Session(id="s-1"))
        unclaimed = StubAdapter()
        await brief_lane(manager, make_lane(claims=["b.py"]), unclaimed, Session(id="s-1"))

        assert "parser quirk" in claimed.delivered[0]
        assert "parser quirk" not in unclaimed.delivered[0]
        # The lesson survives the filter either way.
        assert "the runner is slow" in unclaimed.delivered[0]


class TestTheEvent:
    async def test_an_empty_briefing_is_reported_as_empty(
        self, database: Database, tmp_path: Path
    ) -> None:
        """Reported, not omitted — "nothing yet" is a fact worth being able to see."""
        bus = RecordingBus()
        manager = make_manager(database, repo_root=tmp_path, bus=bus)
        await brief_lane(manager, make_lane(), StubAdapter(), Session(id="s-1"))

        event = bus.only("lane.briefed")
        assert event["payload"]["delivered"] is False
        assert event["payload"]["reason"] == "nothing recorded for this repository yet"
        assert event["payload"]["lessons"] == []

    async def test_a_delivered_briefing_is_reported_with_its_ids(
        self, database: Database, tmp_path: Path
    ) -> None:
        await record_lesson(database, tmp_path)
        bus = RecordingBus()
        lane = make_lane()
        manager = make_manager(database, repo_root=tmp_path, bus=bus)
        await brief_lane(manager, lane, StubAdapter(), Session(id="s-1"))

        event = bus.only("lane.briefed")
        assert event["payload"]["delivered"] is True
        assert len(event["payload"]["lessons"]) == 1
        assert "reason" not in event["payload"]
        # The event names the lane, so a reel can join it to the start event.
        assert event["lane_id"] == lane.id

    async def test_a_harness_that_accepts_nothing_is_reported_as_such(
        self, database: Database, tmp_path: Path
    ) -> None:
        """``inject_message`` returning False is a real signal, not a success.

        Its own docstring says a False return means the lane is discoverable on
        the bus but not actually reachable. Reporting that as delivered would
        make the counter above count briefings nobody received.
        """
        await record_lesson(database, tmp_path)
        bus = RecordingBus()
        manager = make_manager(database, repo_root=tmp_path, bus=bus)
        await brief_lane(manager, make_lane(), StubAdapter(accepts=False), Session(id="s-1"))

        event = bus.only("lane.briefed")
        assert event["payload"]["delivered"] is False
        assert event["payload"]["reason"] == "the harness accepted no input"

    async def test_a_failed_injection_does_not_fail_the_start(
        self, database: Database, tmp_path: Path
    ) -> None:
        """The lane started. Reporting that as a crash would be a different lie."""
        await record_lesson(database, tmp_path)
        bus = RecordingBus()
        manager = make_manager(database, repo_root=tmp_path, bus=bus)
        briefing = await brief_lane(
            manager,
            make_lane(),
            StubAdapter(raises=RuntimeError("no tty")),
            Session(id="s-1"),
        )

        assert briefing.is_empty is False
        event = bus.only("lane.briefed")
        assert event["payload"]["delivered"] is False
        assert event["payload"]["reason"] == "injection failed: RuntimeError"


class TestWhatTheBriefingCost:
    async def test_the_lesson_counter_advances_on_delivery(
        self, database: Database, tmp_path: Path
    ) -> None:
        """The store's own counter, read back from the row.

        This is what makes ``evict_noise`` reachable at all: before the
        persistence fix, no lesson could ever accumulate samples.
        """
        lesson = await record_lesson(database, tmp_path)
        manager = make_manager(database, repo_root=tmp_path)
        await brief_lane(manager, make_lane(), StubAdapter(), Session(id="s-1"))

        store = LessonStore(database, repo_id=repo_id_for(tmp_path))
        stored = next(item for item in await store.live(session_id="s-1") if item.id == lesson.id)
        assert stored.injection_count == 1

    async def test_the_lane_remembers_what_it_was_told(
        self, database: Database, tmp_path: Path
    ) -> None:
        """Written with the lane so an outcome can be attributed later.

        ``record_outcome`` needs a lane id and a lesson id; without this the
        second half of that pair would have to be re-derived by re-running the
        selection, which would count a second injection for the same briefing.
        """
        lesson = await record_lesson(database, tmp_path)
        lane = make_lane()
        manager = make_manager(database, repo_root=tmp_path)
        await brief_lane(manager, lane, StubAdapter(), Session(id="s-1"))

        assert lane.metadata["briefing"]["lessons"] == [lesson.id]

    async def test_an_empty_briefing_costs_nothing(
        self, database: Database, tmp_path: Path
    ) -> None:
        lane = make_lane()
        manager = make_manager(database, repo_root=tmp_path)
        await brief_lane(manager, lane, StubAdapter(), Session(id="s-1"))

        assert lane.metadata["briefing"]["lessons"] == []
        assert lane.metadata["briefing"]["brain_entries"] == []
        assert lane.metadata["briefing"]["characters"] == 0


# --------------------------------------------------------------------------
# The call site
# --------------------------------------------------------------------------
class TestTheCallSite:
    """Asserted at the source level, because the call site is the claim.

    ``_brief_lane`` existing and being correct is not the same as ``start_lane``
    calling it. The defect this removes was exactly that gap — two selectors,
    correct and unreachable — and every behavioural test in this file passes with
    the call site deleted.
    """

    def _source(self) -> str:
        from openburrow.daemon import sessions as daemon_sessions

        return Path(daemon_sessions.__file__).read_text(encoding="utf-8")

    def test_the_lane_is_briefed_from_start_lane(self) -> None:
        source = self._source()
        assert "await self._brief_lane(lane, adapter, session)" in source

    def test_the_briefing_happens_after_the_harness_starts(self) -> None:
        """Delivery goes through the harness's input path, so it has to exist."""
        source = self._source()
        assert source.index("await adapter.start(lane, spec=spec)") < source.index(
            "await self._brief_lane(lane, adapter, session)"
        )

    def test_the_briefing_happens_before_the_lane_is_persisted(self) -> None:
        """So the ids it spent are written with the lane that spent them."""
        source = self._source()
        brief = source.index("await self._brief_lane(lane, adapter, session)")
        persist = source.index("await self._persist_lane(lane)", brief)
        assert brief < persist

    def test_the_injection_metric_has_a_producer(self) -> None:
        """Item 217's denominator was a counter with no call site in production.

        It appeared in one observability unit test and nowhere else, which is the
        shape that makes a dashboard read zero and look like a quiet system.
        """
        source = self._source()
        assert "record_lesson_injection(len(briefing.lesson_ids))" in source
