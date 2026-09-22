"""Lesson effect accounting: the counters that decide whether a lesson survives.

Item 217 asks whether lesson propagation is doing anything or is just printing
text. Answering that needs two numbers — how often a lesson was put in front of a
lane, and how often the lane then behaved differently — and the ratio between
them is what ``evict_noise`` retires on. Each of those three pieces was wrong in
a different way, and each test below is written so that it *fails on the code as
it was*, not merely passes on the code as it is.

The three defects, stated as claims:

**A counter mutated in memory and never persisted is an inert mechanism.**
``select_for_injection`` called ``record_injection()`` and saved nothing. Every
read re-fetched the row, so ``injection_count`` was 1 for a lesson injected fifty
times, and ``EVICTION_MIN_SAMPLES`` could never be reached by any lesson that had
not been tampered with. ``test_a_second_selection_continues_the_first_count``
injects twice and asserts the count is 2; it reads 1 on the old code.

**Counting a corroboration as a delivery makes a rate a ratio of two different
things.** A second lane reporting the same lesson is evidence it is *real*; it is
not evidence it was *spent*. The merge used to bump ``injection_count``, so a
lesson injected once and genuinely helpful but re-reported by nine lanes scored
0.1 and was retired for being useless.
``test_the_hit_rate_is_not_diluted_by_corroboration`` is that scenario.

**A detector may be noisy; an action may not.** ``evict_noise`` retired on
``hit_rate == 0.0``, and 0.0 is what ``hit_rate`` returns both when every lane
ignored a lesson and when no lane was ever asked. Retiring on the second reading
is the system acting on a number it invented, and the reason string would have
claimed "helped 0 lane(s)" about lanes that were never consulted.
``test_an_absent_outcome_is_not_treated_as_a_bad_one`` pins the refusal, and the
tests beside it are the controls that prove the refusal did not simply disable
eviction.
"""

from __future__ import annotations

import pytest

from openburrow.brain.lessons import (
    EVICTION_MIN_SAMPLES,
    WIDE_CONFIDENCE_FLOOR,
    LessonCandidate,
    LessonStore,
)
from openburrow.core.db.engine import Database
from openburrow.core.db.repository import Repository
from openburrow.core.models import Lesson, LessonScope

pytestmark = [pytest.mark.integration]

SESSION = "s-1"


def make_store(database: Database) -> LessonStore:
    return LessonStore(database, repo_id="repo_test")


async def promote(
    database: Database, title: str = "the runner is slow on tuesdays", **extra: object
) -> str:
    """Promote one lesson and return its id.

    ``extra`` is splatted so a caller can vary the lane that reported it, which
    is the only thing that distinguishes corroboration from a duplicate.
    """
    store = make_store(database)
    lesson = await store.promote(
        LessonCandidate(
            title=title,
            body="turns out the CI runner is slow on tuesdays",
            remedy="schedule the heavy job on wednesday",
            **extra,  # type: ignore[arg-type]
        ),
        session_id=SESSION,
    )
    return lesson.id


async def read(database: Database, lesson_id: str) -> Lesson:
    """Read a *live* lesson back through a fresh store.

    Fresh, because the defect being tested was that the object handed to the
    caller looked right while the row it came from did not. Asserting on the
    returned object would have passed throughout.
    """
    lessons = await make_store(database).live(session_id=SESSION)
    return next(lesson for lesson in lessons if lesson.id == lesson_id)


async def read_any(database: Database, lesson_id: str) -> Lesson:
    """Read a lesson back regardless of whether it is still live.

    ``live`` filters retired rows out, and a test about retirement has to be able
    to look at one afterwards — otherwise it can only assert that the lesson
    vanished, not *why*, and a store that deleted instead of retiring would pass.
    """
    async with database.session() as session:
        lesson = await Repository(session).get(Lesson, lesson_id)
    assert lesson is not None
    return lesson


class TestAnInjectionIsCountedAndKept:
    async def test_a_second_selection_continues_the_first_count(self, database: Database) -> None:
        """The counter has to survive the call, or nothing downstream can use it."""
        lesson_id = await promote(database)
        store = make_store(database)

        first = await store.select_for_injection(session_id=SESSION)
        second = await store.select_for_injection(session_id=SESSION)

        assert [lesson.id for lesson in first] == [lesson_id]
        assert [lesson.id for lesson in second] == [lesson_id]
        stored = await read(database, lesson_id)
        assert stored.injection_count == 2

    async def test_the_returned_lesson_is_the_one_that_was_counted(
        self, database: Database
    ) -> None:
        """A caller that renders these is rendering what it spent.

        The count is bumped before the list is returned, so the object in the
        caller's hand already reads 1. Building a copy to return would leave the
        caller holding a lesson whose own counter disagrees with the row.
        """
        lesson_id = await promote(database)
        chosen = await make_store(database).select_for_injection(session_id=SESSION)
        assert [lesson.injection_count for lesson in chosen] == [1]
        stored = await read(database, lesson_id)
        assert stored.injection_count == 1

    async def test_a_lesson_below_the_floor_is_not_counted(self, database: Database) -> None:
        """Not injected means not spent, and not counted.

        A lesson that is stored and never selected has no injections at all —
        which is a different state from "injected and useless", and the reason
        the two counters exist as a pair.
        """
        store = make_store(database)
        await store.promote(
            LessonCandidate(title="too unsure to inject", body="x", confidence=0.1),
            session_id=SESSION,
        )
        chosen = await store.select_for_injection(session_id=SESSION)
        assert chosen == []


class TestACorroborationIsNotADelivery:
    async def test_a_merge_does_not_count_as_an_injection(self, database: Database) -> None:
        lesson_id = await promote(database, source_lane="lane-a")
        await make_store(database).select_for_injection(session_id=SESSION)

        # A second lane hits the same problem and reports the same lesson.
        await make_store(database).promote(
            LessonCandidate(
                title="the runner is slow on tuesdays",
                body="same problem, different lane",
                source_lane="lane-b",
            ),
            session_id=SESSION,
        )

        stored = await read(database, lesson_id)
        assert stored.injection_count == 1

    async def test_a_merge_still_raises_confidence(self, database: Database) -> None:
        """The refusal above must not make corroboration a no-op.

        This is the control for the test before it: dropping the ``_save`` from
        the merge branch would satisfy "does not count as an injection" while
        silently discarding the only signal a merge carries.
        """
        lesson_id = await promote(database, source_lane="lane-a")
        before = await read(database, lesson_id)
        assert before.confidence < WIDE_CONFIDENCE_FLOOR

        await make_store(database).promote(
            LessonCandidate(
                title="the runner is slow on tuesdays",
                body="same problem, different lane",
                source_lane="lane-b",
            ),
            session_id=SESSION,
        )

        after = await read(database, lesson_id)
        assert after.confidence == WIDE_CONFIDENCE_FLOOR

    async def test_a_repeat_from_the_same_lane_is_not_corroboration(
        self, database: Database
    ) -> None:
        """One lane saying the same thing twice is one witness, not two.

        The confidence bump is guarded on the source lane differing, so a lane
        that re-reports its own lesson cannot promote it to repo-wide scope by
        repetition alone.
        """
        lesson_id = await promote(database, source_lane="lane-a")
        await make_store(database).promote(
            LessonCandidate(
                title="the runner is slow on tuesdays",
                body="saying it again",
                source_lane="lane-a",
            ),
            session_id=SESSION,
        )
        after = await read(database, lesson_id)
        assert after.confidence < WIDE_CONFIDENCE_FLOOR

    async def test_the_hit_rate_is_not_diluted_by_corroboration(self, database: Database) -> None:
        """The scenario the defect was found through, at the size it was found at.

        Injected once, helped once, then re-reported by nine other lanes. The old
        accounting scored this 1/10 = 0.1, below ``EVICTION_HIT_RATE``, and
        ``evict_noise`` would have retired a lesson that had a perfect record.
        """
        lesson_id = await promote(database, source_lane="lane-a")
        await make_store(database).select_for_injection(session_id=SESSION)
        await make_store(database).record_outcome(lesson_id, lane_id="lane-a", helped=True)

        for index in range(9):
            await make_store(database).promote(
                LessonCandidate(
                    title="the runner is slow on tuesdays",
                    body="same problem again",
                    source_lane=f"lane-{index}",
                ),
                session_id=SESSION,
            )

        stored = await read(database, lesson_id)
        assert stored.injection_count == 1
        assert stored.hit_rate == 1.0

        report = await make_store(database).evict_noise(session_id=SESSION)
        assert report.retired == []


class TestEvictionRefusesToJudgeAnAbsentSignal:
    async def test_an_absent_outcome_is_not_treated_as_a_bad_one(self, database: Database) -> None:
        """Enough samples, zero outcomes — retained, and for the right reason.

        ``hit_rate`` reads 0.0 here. It reads 0.0 for a lesson every lane
        ignored, too, and the two are not the same fact. Only the second is
        evidence, so only the second may retire anything.
        """
        lesson_id = await promote(database)
        store = make_store(database)
        for _ in range(EVICTION_MIN_SAMPLES):
            await store.select_for_injection(session_id=SESSION)

        stored = await read(database, lesson_id)
        assert stored.injection_count == EVICTION_MIN_SAMPLES
        assert stored.hit_rate == 0.0

        report = await store.evict_noise(session_id=SESSION)
        assert report.retired == []
        assert report.retained == 1

    async def test_a_reported_bad_outcome_is_evicted(self, database: Database) -> None:
        """The control: with evidence, the same lesson *is* retired.

        Without this, ``evict_noise`` could return an empty report
        unconditionally and every other test in this class would still pass.
        """
        lesson_id = await promote(database)
        store = make_store(database)
        for _ in range(EVICTION_MIN_SAMPLES):
            await store.select_for_injection(session_id=SESSION)
        await store.record_outcome(lesson_id, lane_id="lane-a", helped=False)

        report = await store.evict_noise(session_id=SESSION)
        assert report.retired == [lesson_id]
        assert report.retained == 0

        retired = await read_any(database, lesson_id)
        assert retired.is_live is False
        assert "injected 5x" in retired.retired_reason

    async def test_a_reported_good_outcome_is_kept(self, database: Database) -> None:
        """The other control: evidence of usefulness protects a lesson."""
        lesson_id = await promote(database)
        store = make_store(database)
        for _ in range(EVICTION_MIN_SAMPLES):
            await store.select_for_injection(session_id=SESSION)
        await store.record_outcome(lesson_id, lane_id="lane-a", helped=True)

        report = await store.evict_noise(session_id=SESSION)
        assert report.retired == []

    async def test_too_few_samples_is_never_evicted(self, database: Database) -> None:
        """One bad outcome is not enough evidence to discard a lesson.

        A lesson that did not apply to one task is not a lesson that never
        applies. Retiring on a single sample is how a store loses the good ones
        first, because the good ones are the ones being applied to hard problems.
        """
        lesson_id = await promote(database)
        store = make_store(database)
        await store.select_for_injection(session_id=SESSION)
        await store.record_outcome(lesson_id, lane_id="lane-a", helped=False)

        report = await store.evict_noise(session_id=SESSION)
        assert report.retired == []

    async def test_evidence_is_judged_per_lesson(self, database: Database) -> None:
        """One noisy lesson does not take a quiet one down with it."""
        noisy = await promote(database, title="this one is noise")
        quiet = await promote(database, title="this one was never judged")
        store = make_store(database)
        for _ in range(EVICTION_MIN_SAMPLES):
            await store.select_for_injection(session_id=SESSION)
        await store.record_outcome(noisy, lane_id="lane-a", helped=False)

        report = await store.evict_noise(session_id=SESSION)
        assert report.retired == [noisy]
        assert report.retained == 1
        survivor = await read(database, quiet)
        assert survivor.is_live is True


class TestTheLoopClosesThroughThePublicApi:
    async def test_inject_then_report_then_evict(self, database: Database) -> None:
        """The whole cycle, with no private method touched.

        This is the test that says the mechanism is reachable: every step is a
        public store call, and the lesson goes from promoted to injected to
        judged to retired without anything reaching into the row.
        """
        lesson_id = await promote(database, source_lane="lane-a")
        store = make_store(database)

        for _ in range(EVICTION_MIN_SAMPLES):
            chosen = await store.select_for_injection(session_id=SESSION)
            assert [lesson.id for lesson in chosen] == [lesson_id]

        # The lane did not report a benefit, which is the conservative default.
        await store.record_outcome(lesson_id, lane_id="lane-b", helped=False)

        report = await store.evict_noise(session_id=SESSION)
        assert report.retired == [lesson_id]

        # And it is gone from the injectable set, not merely marked.
        assert await store.select_for_injection(session_id=SESSION) == []

    async def test_a_retired_lesson_stops_being_selected(self, database: Database) -> None:
        """Retirement has to have an effect on the only thing that reads this.

        A retirement flag that ``select_for_injection`` ignores would leave the
        lesson in every future prompt while every report said it was gone.
        """
        lesson_id = await promote(database)
        store = make_store(database)
        await store.retire(lesson_id, reason="manual")

        assert await store.select_for_injection(session_id=SESSION) == []


class TestScopeDecidesWhetherTheCounterIsShared:
    async def test_a_session_lesson_is_not_attributed_to_a_repo(self, database: Database) -> None:
        """Session scope stays out of ``repo_id``, which is what keeps it private.

        ``live_lessons`` unions session-scoped and repo-scoped rows, so a session
        lesson carrying a repo id would be injected into every other session in
        the same repository — the opposite of what "session" means.
        """
        lesson_id = await promote(database)
        stored = await read(database, lesson_id)
        assert stored.scope == LessonScope.SESSION
        assert stored.repo_id == ""
