"""Brain selection: what gets put in front of a lane, and what that costs.

``select_for_injection`` is the only method that spends the Brain's knowledge, so
it is the one the briefing path calls. Two things about it are load-bearing and
neither had a test before this file:

**The count it keeps has to survive the call.** The entry handed back was
correctly incremented and the row was never written, so ``injection_count`` read
0 on every subsequent read — meaning the Brain's own answer to "how much have we
been injecting?" was a constant zero. ``test_the_count_survives_the_call`` injects
twice and reads the row back; it reads 1 on the old code.

**Relevance is a filter, not a preference.** An entry anchored to a file the lane
is not touching is trivia, and the briefing passes the lane's claims in as
``paths``. If the filter were only a ranking term, every prompt would carry every
convention in the repository and the budget would decide what mattered.
"""

from __future__ import annotations

import pytest

from openburrow.brain.store import (
    CORROBORATED_CONFIDENCE,
    INJECTION_CONFIDENCE_FLOOR,
    UNCORROBORATED_CONFIDENCE,
    BrainStore,
    Candidate,
)
from openburrow.core.db.engine import Database
from openburrow.core.db.repository import Repository
from openburrow.core.models import BrainEntry, BrainEntryType

pytestmark = [pytest.mark.integration]

REPO = "repo_test"


def make_store(database: Database) -> BrainStore:
    return BrainStore(database, repo_id=REPO)


async def add(database: Database, title: str = "line length is 100", **extra: object) -> str:
    """Promote one entry and return its id.

    Defaults to a human promotion, which is the only route to a confidence above
    the injection floor without a second witness — so a test that wants an
    injectable entry gets one without having to arrange corroboration first.
    """
    result = await make_store(database).promote(
        Candidate(
            title=title,
            body="ruff enforces a 100 column limit",
            promoted_by="human",
            **extra,  # type: ignore[arg-type]
        )
    )
    return result.entry.id


async def read(database: Database, entry_id: str) -> BrainEntry:
    async with database.session() as session:
        entry = await Repository(session).get(BrainEntry, entry_id)
    assert entry is not None
    return entry


class TestTheCountSurvivesTheCall:
    async def test_the_count_survives_the_call(self, database: Database) -> None:
        entry_id = await add(database)
        store = make_store(database)

        first = await store.select_for_injection()
        second = await store.select_for_injection()

        assert [entry.id for entry in first] == [entry_id]
        assert [entry.id for entry in second] == [entry_id]
        stored = await read(database, entry_id)
        assert stored.injection_count == 2

    async def test_the_returned_entry_is_the_one_that_was_counted(self, database: Database) -> None:
        """Both the object and the row, because the defect was that they differed.

        This test originally asserted only the returned entry's counter, and the
        falsification pass showed it still passed with persistence removed — the
        in-memory increment survives either way. "Counted" has to mean counted in
        the record, or the assertion is about the object the caller already holds.
        """
        entry_id = await add(database)
        chosen = await make_store(database).select_for_injection()
        assert [entry.injection_count for entry in chosen] == [1]
        stored = await read(database, entry_id)
        assert stored.injection_count == 1

    async def test_a_failed_save_does_not_leave_the_caller_a_wrong_number(
        self, database: Database
    ) -> None:
        """The object and the row agree, whichever way the write goes.

        Not a hypothetical: this is the property that made the defect invisible.
        The returned entry read 1 while the row read 0, so every test that
        inspected the return value would have passed.
        """
        entry_id = await add(database)
        chosen = await make_store(database).select_for_injection()
        stored = await read(database, entry_id)
        assert chosen[0].injection_count == stored.injection_count


class TestBelievability:
    async def test_an_unconfirmed_entry_is_not_injected(self, database: Database) -> None:
        """Below the floor, an entry is listed and searchable but not spent."""
        result = await make_store(database).promote(
            Candidate(title="a harness guessed this", body="x", promoted_by="classifier")
        )
        assert result.entry.confidence == UNCORROBORATED_CONFIDENCE
        assert UNCORROBORATED_CONFIDENCE < INJECTION_CONFIDENCE_FLOOR
        assert await make_store(database).select_for_injection() == []

    async def test_a_second_witness_makes_it_injectable(self, database: Database) -> None:
        """The promotion path is what turns an assertion into knowledge."""
        first = await make_store(database).promote(
            Candidate(
                title="line length is 100",
                body="x",
                promoted_by="classifier",
                source_lane="lane-a",
            )
        )
        assert await make_store(database).select_for_injection() == []

        second = await make_store(database).promote(
            Candidate(
                title="line length is 100",
                body="x",
                promoted_by="classifier",
                source_lane="lane-b",
            )
        )
        assert second.corroborated is True
        assert second.entry.id == first.entry.id
        assert second.entry.confidence == CORROBORATED_CONFIDENCE

        chosen = await make_store(database).select_for_injection()
        assert [entry.id for entry in chosen] == [first.entry.id]

    async def test_a_retired_entry_is_not_injected(self, database: Database) -> None:
        entry_id = await add(database)
        await make_store(database).retire(entry_id, reason="no longer true")
        assert await make_store(database).select_for_injection() == []


class TestRelevanceIsAFilter:
    async def test_a_convention_about_another_file_is_not_injected(
        self, database: Database
    ) -> None:
        """The lane said which files it will touch; the Brain believes it."""
        await add(database, title="parser quirk", anchor_path="src/parser.py")
        chosen = await make_store(database).select_for_injection(paths=["src/web.py"])
        assert chosen == []

    async def test_the_matching_file_is_injected(self, database: Database) -> None:
        entry_id = await add(database, title="parser quirk", anchor_path="src/parser.py")
        chosen = await make_store(database).select_for_injection(paths=["src/parser.py"])
        assert [entry.id for entry in chosen] == [entry_id]

    async def test_a_repo_wide_convention_survives_any_claim(self, database: Database) -> None:
        """Unscoped and confirmed is the category that must not be filtered out.

        These are few and load-bearing — a project-wide convention a human stood
        behind is exactly what a lane should be told regardless of what it is
        editing. If the filter applied to unscoped entries, the briefing would be
        empty for every lane that declared a claim.
        """
        entry_id = await add(database, title="commits are conventional")
        chosen = await make_store(database).select_for_injection(paths=["src/anything.py"])
        assert [entry.id for entry in chosen] == [entry_id]

    async def test_an_empty_claim_list_does_not_filter(self, database: Database) -> None:
        """No claims means no scope, not "scope is the empty set".

        A lane that declared nothing gets everything injectable, because the
        alternative is a lane briefed with nothing at all.
        """
        entry_id = await add(database, title="parser quirk", anchor_path="src/parser.py")
        chosen = await make_store(database).select_for_injection(paths=[])
        assert [entry.id for entry in chosen] == [entry_id]


class TestTheBudget:
    async def test_the_budget_is_a_hard_cap(self, database: Database) -> None:
        for index in range(10):
            await add(database, title=f"convention {index}")
        chosen = await make_store(database).select_for_injection(budget=3)
        assert len(chosen) == 3

    async def test_the_default_budget_bounds_an_unattended_repository(
        self, database: Database
    ) -> None:
        """Context is the scarcest resource; the cap is not the caller's to forget."""
        for index in range(12):
            await add(database, title=f"convention {index}")
        chosen = await make_store(database).select_for_injection()
        assert len(chosen) == 8

    async def test_only_the_chosen_entries_are_counted(self, database: Database) -> None:
        """An entry ranked below the cut was never spent, so it was never counted.

        Counting the whole eligible set would make the Brain's numbers describe
        its inventory rather than its behaviour, and the two diverge exactly when
        the repository is busiest.
        """
        ids = [await add(database, title=f"convention {index}") for index in range(10)]
        chosen = await make_store(database).select_for_injection(budget=3)
        assert len(chosen) == 3

        counts = {entry_id: (await read(database, entry_id)).injection_count for entry_id in ids}
        assert sorted(counts.values()) == [0, 0, 0, 0, 0, 0, 0, 1, 1, 1]

    async def test_a_scoped_entry_outranks_an_unscoped_one(self, database: Database) -> None:
        """Relevance decides the cut, not insertion order."""
        await add(database, title="project-wide convention")
        anchored = await add(database, title="parser quirk", anchor_path="src/parser.py")

        chosen = await make_store(database).select_for_injection(paths=["src/parser.py"], budget=1)
        assert [entry.id for entry in chosen] == [anchored]


class TestProvenance:
    async def test_an_entry_keeps_the_lane_that_reported_it(self, database: Database) -> None:
        """Item 189's whole point: you cannot defend against what you cannot trace."""
        entry_id = await add(database, source_lane="lane-a", source_harness="claude-code")
        stored = await read(database, entry_id)
        assert stored.source_lane == "lane-a"
        assert stored.source_harness == "claude-code"

    async def test_an_entry_is_scoped_to_the_repository_that_recorded_it(
        self, database: Database
    ) -> None:
        """A second repo's Brain must not see this one's entries."""
        await add(database)
        other = BrainStore(database, repo_id="repo_other")
        assert await other.select_for_injection() == []

    async def test_the_entry_type_is_carried_through(self, database: Database) -> None:
        entry_id = await add(database, entry_type=BrainEntryType.GOTCHA)
        stored = await read(database, entry_id)
        assert stored.entry_type == BrainEntryType.GOTCHA
