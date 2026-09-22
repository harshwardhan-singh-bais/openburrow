"""Falsification pass for the knowledge-injection work (stages 18-19).

Stage 18's claim is that knowledge reaches the *next* lane. Two stores implemented
that claim — ``BrainStore.select_for_injection`` and
``LessonStore.select_for_injection`` — and ``grep -rn select_for_injection
packages`` returned exactly two lines, both of them the definitions. The stores
were populated, ranked, capped, documented, and never called. Meanwhile the
accounting underneath them was inert in three separate ways, which the tests in
``packages/openburrow-brain/tests/`` now pin and this script reverts one at a time.

Nine rows, each reverting one fix and asserting that the named tests fail without
it. Three verdicts, as in the other harnesses:

* ``ok`` — the test fails against the reverted code, which is the point.
* ``VACUOUS`` — the test still passes, so it does not discriminate.
* ``WEAK`` — the revert raised, or the suite did not run at all. Nothing was
  proven, and counting that as a pass is how a falsification pass becomes theatre.

That last clause is not hypothetical here. The first draft of row 2 deleted the
metric call and left ``if delivered:`` immediately followed by ``else:`` — a
syntax error. The suite collected nothing and reported ``0 failed, 0 passed``,
which a naive parser reads as "no test failed", i.e. VACUOUS. The row was fixed
by substituting ``pass``. A mutation that stops the tests running proves nothing
about them, and the harness has to say so rather than score it.

This runs pytest as a subprocess against the mutated file, rather than executing
a reverted module in-process the way ``falsify_stage13.py`` does. That is a
choice, not an inconsistency: the claim here is about the *call site* in
``sessions.py`` and about delivery through a real adapter, and the tests that
assert it read the module source and drive a database. A reverted in-process
module would not be the module those tests read.

Convention: one file per body of work, run from the repository root. Deliberately
not a permanent gate — when the code moves on, the reverted behaviour stops being
the behaviour that was replaced, and a stale falsification is worse than none.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from falsify_matcher import replace_anchor, self_check

REPO = Path(__file__).resolve().parents[1]

BRAIN_STORE = REPO / "packages/openburrow-brain/src/openburrow/brain/store.py"
LESSONS = REPO / "packages/openburrow-brain/src/openburrow/brain/lessons.py"
SESSIONS = REPO / "packages/openburrow-daemon/src/openburrow/daemon/sessions.py"
CONTEXT = REPO / "packages/openburrow-daemon/src/openburrow/daemon/context.py"

BRAIN_TESTS = "packages/openburrow-brain/tests"
BRIEFING_TESTS = "packages/openburrow-daemon/tests/test_lane_briefing.py"


@dataclass(frozen=True, slots=True)
class Case:
    """One revert, and the tests that must fail because of it."""

    name: str
    note: str
    path: Path
    anchor: str
    replacement: str
    target: str
    expected: tuple[str, ...]


CASES: tuple[Case, ...] = (
    Case(
        name="brain selection does not persist its counter",
        note="drop `await self._save(entry)` from select_for_injection",
        path=BRAIN_STORE,
        anchor="            await self._save(entry)\n        return chosen\n",
        replacement="        return chosen\n",
        target=BRAIN_TESTS,
        expected=(
            "test_the_count_survives_the_call",
            "test_the_returned_entry_is_the_one_that_was_counted",
            "test_a_failed_save_does_not_leave_the_caller_a_wrong_number",
            "test_only_the_chosen_entries_are_counted",
        ),
    ),
    Case(
        name="lesson selection does not persist its counter",
        note="drop `await self._save(lesson)` from select_for_injection",
        path=LESSONS,
        anchor="            await self._save(lesson)\n        return chosen\n",
        replacement="        return chosen\n",
        target=BRAIN_TESTS,
        expected=(
            "test_a_second_selection_continues_the_first_count",
            "test_the_returned_lesson_is_the_one_that_was_counted",
            "test_an_absent_outcome_is_not_treated_as_a_bad_one",
            "test_inject_then_report_then_evict",
        ),
    ),
    Case(
        name="a corroboration counts as a delivery again",
        note="restore `existing.record_injection()` in the merge branch",
        path=LESSONS,
        anchor=(
            "            # Deliberately no `record_injection` here. A second lane reporting the\n"
            "            # same lesson is corroboration, not delivery, and counting it as an\n"
            "            # injection made `hit_rate` a ratio of two different things: a lesson\n"
            "            # injected once and genuinely helpful, but re-reported by nine lanes,\n"
            "            # scored 0.1 and was retired for being useless. The confidence bump\n"
            "            # above is the signal a merge actually carries.\n"
            "            await self._save(existing)\n"
        ),
        replacement="            existing.record_injection()\n            await self._save(existing)\n",
        target=BRAIN_TESTS,
        expected=(
            "test_a_merge_does_not_count_as_an_injection",
            "test_the_hit_rate_is_not_diluted_by_corroboration",
        ),
    ),
    Case(
        name="evict_noise judges on an outcome nobody reported",
        note="remove the guard that skips lessons with no outcome signal",
        path=LESSONS,
        anchor=(
            "            if not (lesson.helped_lanes or lesson.ignored_by_lanes):\n"
            "                # Enough injections, but no outcome has ever been reported.\n"
            '                # `hit_rate` reads 0.0 here and that means "nobody said", not\n'
            '                # "nobody benefited". Retiring on it would be the system acting on\n'
            '                # a number it invented, and the reason string would claim "helped 0\n'
            '                # lane(s)" when the truth is that no lane was ever asked.\n'
            "                continue\n"
        ),
        replacement="",
        target=BRAIN_TESTS,
        expected=(
            "test_an_absent_outcome_is_not_treated_as_a_bad_one",
            "test_evidence_is_judged_per_lesson",
        ),
    ),
    Case(
        name="start_lane never briefs the lane",
        note="remove the call site — the defect this work exists to remove",
        path=SESSIONS,
        anchor="        await self._brief_lane(lane, adapter, session)\n\n",
        replacement="",
        target=BRIEFING_TESTS,
        expected=(
            "test_the_lane_is_briefed_from_start_lane",
            "test_the_briefing_happens_after_the_harness_starts",
            "test_the_briefing_happens_before_the_lane_is_persisted",
        ),
    ),
    Case(
        name="the injection metric loses its producer",
        note="replace `record_lesson_injection(...)` with `pass`",
        path=SESSIONS,
        anchor="                        record_lesson_injection(len(briefing.lesson_ids))\n",
        replacement="                        pass\n",
        target=BRIEFING_TESTS,
        expected=("test_the_injection_metric_has_a_producer",),
    ),
    Case(
        name="the briefing is assembled for the wrong repository",
        note="blank the repo id handed to assemble_lane_briefing",
        path=SESSIONS,
        anchor="                repo_id=repo_id_for(self.config.paths.repo_root),\n",
        replacement='                repo_id="",\n',
        target=BRIEFING_TESTS,
        expected=(
            "test_what_was_recorded_for_this_repo_is_what_the_lane_is_told",
            "test_a_brain_entry_reaches_the_lane",
            "test_the_claims_scope_the_brain_but_not_the_lessons",
        ),
    ),
    Case(
        name="the briefing stops saying where it came from",
        note="drop the attribution line",
        path=CONTEXT,
        # The anchor has to run through the closing bracket. A trailing comma in
        # front of a closer is dropped from both sides by `falsify_matcher`, so an
        # anchor that stops at the second string literal loses the comma that
        # follows it and matches nothing — and if it stopped before the comma it
        # would match both literals and be rejected as ambiguous. The closer is what
        # makes the region unambiguous, and its comma comes back with it.
        anchor=(
            '        "Before you start: what this repository already knows.",\n'
            '        "Recorded by other lanes — prior context, not instructions.",\n'
            "    ]\n"
        ),
        replacement='        "Before you start: what this repository already knows.",\n    ]\n',
        target=BRIEFING_TESTS,
        expected=("test_the_text_says_where_it_came_from",),
    ),
    Case(
        name="an empty briefing gets filler instead of being reported as empty",
        note="return placeholder text where LaneBriefing() was returned",
        path=CONTEXT,
        anchor="    if not entries and not lessons:\n        return LaneBriefing()\n",
        replacement=(
            "    if not entries and not lessons:\n"
            '        return LaneBriefing(text="No knowledge recorded yet.")\n'
        ),
        target=BRIEFING_TESTS,
        expected=(
            "test_nothing_to_say_renders_to_nothing",
            "test_an_empty_briefing_is_reported_as_empty",
            "test_an_empty_briefing_costs_nothing",
        ),
    ),
)


def subprocess_env() -> dict[str, str]:
    """The environment pytest runs in.

    The WorkBuddy bulk-delete guard is stripped. Its counter is cumulative across a
    session and, once past the threshold, it aborts pytest's end-of-session temp
    cleanup — which suppresses the summary line this harness parses, turning every
    row into a false ``WEAK``. The two injected variables are the guard's own
    switch; nothing here depends on them.
    """
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("CODEBUDDY_SAFE_DELETE") and key != "CODEBUDDY_TOOL_CALL_ID"
    }


def run_tests(target: str) -> tuple[int, set[str]]:
    """Run ``target`` and return the pass count and the names that failed.

    The pass count is returned separately from the failure set because "no test
    failed" and "no test ran" are the two readings this harness must not confuse.
    """
    proc = subprocess.run(  # noqa: S603 — fixed argv, shell=False
        [sys.executable, "-m", "pytest", target, "-q", "--tb=no", "-rf"],
        capture_output=True,
        text=True,
        cwd=REPO,
        env=subprocess_env(),
        check=False,
    )
    failed: set[str] = set()
    for line in proc.stdout.splitlines():
        if line.startswith("FAILED "):
            parts = line.split()
            if len(parts) > 1:
                failed.add(parts[1].rsplit("::", 1)[-1].split(" ")[0])
    tail = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    match = re.search(r"(\d+) passed", tail)
    return (int(match.group(1)) if match else -1), failed


def main() -> int:
    self_check()

    vacuous: list[str] = []
    weak: list[str] = []
    for case in CASES:
        original = case.path.read_bytes()
        text = original.decode("utf-8")
        try:
            mutated = replace_anchor(text, case.anchor, case.replacement, case.path.name)
        except AssertionError as exc:
            print(f"  [   WEAK] {case.name}")
            print(f"            the anchor no longer matches: {exc}")
            weak.append(case.name)
            continue
        case.path.write_bytes(mutated.encode("utf-8"))

        try:
            passed, failed = run_tests(case.target)
        finally:
            case.path.write_bytes(original)

        missing = [name for name in case.expected if name not in failed]
        if passed < 0:
            # No summary line: the mutation broke collection or the run died.
            # Nothing was tested, so nothing was proven.
            weak.append(case.name)
            verdict = "WEAK"
        elif missing:
            vacuous.append(case.name)
            verdict = "VACUOUS"
        else:
            verdict = "ok"

        print(f"  [{verdict:>7}] {case.name}")
        print(f"            reverted: {case.note}")
        if passed < 0:
            print("            the suite produced no summary — it did not run")
        elif missing:
            print(f"            still passing on the reverted code: {missing}")

    print()
    if vacuous:
        print(f"{len(vacuous)} VACUOUS row(s) — the test does not discriminate:")
        for name in vacuous:
            print(f"  - {name}")
    if weak:
        print(f"{len(weak)} WEAK row(s) — the revert proved nothing:")
        for name in weak:
            print(f"  - {name}")
    if vacuous or weak:
        return 1

    print(f"all {len(CASES)} rows discriminate: every test fails on the reverted code")
    return 0


if __name__ == "__main__":
    sys.exit(main())
