"""Falsification pass for the stale-lane detection fix.

The defect this guards against is not stage-tied — it was found by *running* the
daemon rather than by reading it, and it is severe enough to deserve its own
harness. A mock lane started, and 16 ms later the supervisor logged

    lane.stopped  reason='orphaned — stale heartbeat'

Two defects met:

1. ``Repository.stale_lanes`` read ``last_heartbeat IS NULL OR last_heartbeat <
   cutoff``. The ``IS NULL`` branch never consulted ``older_than_seconds``, so a
   lane that had not yet written its first heartbeat was stale at *every*
   threshold — including 900s. Young was indistinguishable from dead.
2. ``SessionManager._check_lane`` called ``lane.heartbeat()``, which mutates the
   in-memory object, and never persisted it. Orphan detection reads lanes back
   from records, so the record it read was never maintained.

Either alone is survivable; together they reaped every lane within one
supervision tick of starting. The bug also wrote its own evidence — ``stop_lane``
persists the in-memory lane, so the row ended up holding the heartbeat of the
lane that had just been declared heartless.

The rows below re-implement the replaced behaviour and confirm the new tests
*fail* against it. Three verdicts, as in the stage harnesses:

* ``ok`` — the test fails against the reverted code, which is the point.
* ``VACUOUS`` — the test still passes, so it does not discriminate.
* ``WEAK`` — the revert raised. A revert that raises has tested nothing.

Two rows deliberately revert to a *different* mistake — the naive over-correction
("never report a heartbeat-less lane", "the heartbeat write may touch anything").
A fix that trades one bug for another is still a bug, and the only way to say so
is to falsify in both directions.

Run with ``.venv/Scripts/python.exe scripts/falsify_stale_lanes.py``. Not part of
``make check``: a stale falsification asserts a boundary that no longer exists.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import types
from collections.abc import Callable
from datetime import timedelta
from itertools import count
from pathlib import Path
from typing import Any

from falsify_matcher import replace_anchor, self_check

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "packages"))

from openburrow.core.db.engine import close_all, init_database  # noqa: E402
from openburrow.core.db.models import LaneRow  # noqa: E402
from openburrow.core.models.base import now  # noqa: E402

REPOSITORY_SRC = (
    REPO / "packages" / "openburrow-core" / "src" / "openburrow" / "core" / "db" / "repository.py"
)

SESSIONS_SRC = (
    REPO / "packages" / "openburrow-daemon" / "src" / "openburrow" / "daemon" / "sessions.py"
)

#: The daemon half of the fix: the beat has to reach the record.
HEARTBEAT_WRITE = """        with contextlib.suppress(Exception):
            await self._record_heartbeat(lane)"""

#: Before the fix: ``lane.heartbeat()`` mutated the object and wrote nothing.
NO_HEARTBEAT_WRITE = "        pass"

RECOVERY_SRC = (
    REPO / "packages" / "openburrow-daemon" / "src" / "openburrow" / "daemon" / "recovery.py"
)

#: The branch that breaks the orphan requeue loop. Reverting the condition to a
#: constant false reproduces the old behaviour exactly — the terminal write never
#: happens — while keeping the module importable, so the row measures the branch
#: instead of tripping over a NameError.
ORPHAN_TERMINAL_BRANCH = "            if not stopped:"
ORPHAN_TERMINAL_OFF = "            if False:  # falsification: the old code never wrote the row"

#: The whole query the fix introduced — assignment *and* the ``WHERE`` that
#: consumes it. Anchoring on the assignment alone was the first version of this
#: script, and it was wrong in a way worth recording: the replacement left
#: ``.where(liveness < cutoff)`` in place, so the reverted code compared a
#: *boolean* SQL expression against a datetime. SQLite answered that nonsense
#: query by returning every row, which made two rows report ``ok`` for the right
#: verdict and the wrong reason. A revert has to replace the whole expression,
#: not the half that reads like the fix.
#:
#: Matched token-by-token by :mod:`falsify_matcher`, not byte-for-byte. These two
#: anchors are why that module exists: they were exact, and expired twice for
#: reasons that had nothing to do with the fix — ``ruff format`` collapsed the
#: ``coalesce`` call onto one line, and a later mechanical ``col()`` sweep
#: rewrapped the ``UPDATE``. Both times every row below went WEAK, loudly, which
#: is why it was caught. A gate that has to be repaired after every unrelated
#: refactor is a gate people stop running, so the coupling is gone.
LIVENESS_ANCHOR = """        liveness = func.coalesce(
            col(LaneRow.last_heartbeat), col(LaneRow.started_at), col(LaneRow.created_at)
        )
        result = await self.session.execute(
            select(LaneRow)
            .where(col(LaneRow.status).notin_(["stopped", "crashed"]))
            .where(liveness < cutoff)
        )"""

#: What the code did before: a missing heartbeat is proof of death.
OLD_LIVENESS = """        result = await self.session.execute(
            select(LaneRow)
            .where(LaneRow.status.notin_(["stopped", "crashed"]))
            .where(
                (LaneRow.last_heartbeat.is_(None)) | (LaneRow.last_heartbeat < cutoff)
            )
        )"""

#: The plausible wrong fix: treat a missing heartbeat as an amnesty. A harness
#: that hung before its first beat is exactly what orphan detection is for.
NAIVE_LIVENESS = """        result = await self.session.execute(
            select(LaneRow)
            .where(LaneRow.status.notin_(["stopped", "crashed"]))
            .where(
                (LaneRow.last_heartbeat.is_not(None))
                & (LaneRow.last_heartbeat < cutoff)
            )
        )"""

UPDATE_ANCHOR = """        result = await self.session.execute(
            update(LaneRow).where(col(LaneRow.id) == lane_id).values(last_heartbeat=when or now())
        )
        await self.session.commit()
        return bool(rows_changed(result))"""

#: Before the fix: the write side did nothing, so the record never advanced.
NOOP_UPDATE = "        return True"

#: A heartbeat write that rewrites more than the heartbeat.
GREEDY_UPDATE = """        result = await self.session.execute(
            update(LaneRow)
            .where(LaneRow.id == lane_id)  # type: ignore[arg-type]
            .values(last_heartbeat=when or now(), status="idle")
        )
        await self.session.commit()
        return bool(result.rowcount)"""

_revert_counter = count(1)


def revert(*replacements: tuple[str, str]) -> types.ModuleType:
    """Execute ``repository.py`` with ``replacements`` applied, as a new module.

    A missing anchor is a hard error. The failure mode this harness exists to
    catch is a revert that quietly did not happen, and ``str.replace`` matching
    nothing is exactly that.

    The module is registered in ``sys.modules`` before execution because
    SQLModel/pydantic resolve annotations through ``sys.modules[cls.__module__]``;
    an unregistered module makes them raise, and a revert that raises is reported
    as WEAK rather than as a pass.
    """
    return _revert_source(REPOSITORY_SRC, *replacements)


def revert_sessions(*replacements: tuple[str, str]) -> types.ModuleType:
    """The same, for the daemon's ``sessions.py``.

    The two halves of this bug are independent, so they need separate reverts.
    Fixing only the repository query leaves the record unmaintained, and fixing
    only the write leaves a missing heartbeat reading as death — each is
    falsifiable only against its own file.
    """
    return _revert_source(SESSIONS_SRC, *replacements)


def revert_recovery(*replacements: tuple[str, str]) -> types.ModuleType:
    """The same, for the daemon's ``recovery.py``."""
    return _revert_source(RECOVERY_SRC, *replacements)


def _revert_source(path: Path, *replacements: tuple[str, str]) -> types.ModuleType:
    source = path.read_text(encoding="utf-8")
    for old, new in replacements:
        source = replace_anchor(source, old, new, path.name)

    name = f"{path.stem}_reverted_{next(_revert_counter)}"
    module = types.ModuleType(name)
    module.__file__ = str(path)
    module.__package__ = ""
    sys.modules[name] = module
    exec(compile(source, str(path), "exec"), module.__dict__)  # noqa: S102
    return module


def _lane_row(lane_id: str, **overrides: object) -> LaneRow:
    fields: dict[str, object] = {
        "session_id": "sess_falsify",
        "name": lane_id,
        "harness": "mock",
        "status": "idle",
        "owner": "human",
        "started_at": now(),
        "last_heartbeat": now(),
        "idle_timeout_s": 900,
    }
    fields.update(overrides)
    return LaneRow(id=lane_id, **fields)


async def _with_db(module: types.ModuleType, rows: list[LaneRow], body: Callable[..., Any]) -> Any:
    """Run ``body(repo)`` against a throwaway database seeded with ``rows``."""
    with tempfile.TemporaryDirectory() as tmp:
        url = f"sqlite+aiosqlite:///{(Path(tmp) / 'burrow.db').as_posix()}"
        database = await init_database(url)
        try:
            async with database.session() as db_session:
                db_session.add_all(rows)
                await db_session.commit()
            async with database.session() as db_session:
                return await body(module.Repository(db_session))
        finally:
            await close_all()


def _ids(lanes: Any) -> list[str]:
    return sorted(lane.id for lane in lanes)


ROWS: list[tuple[str, str]] = []


def row(name: str, note: str) -> Callable[[Callable[[], bool]], Callable[[], bool]]:
    def wrap(fn: Callable[[], bool]) -> Callable[[], bool]:
        ROWS.append((name, note))
        return fn

    return wrap


# --- 1. the regression itself ----------------------------------------------
@row(
    "test_a_lane_that_has_not_beaten_yet_is_not_stale",
    "restore `last_heartbeat IS NULL OR last_heartbeat < cutoff`",
)
def check_1() -> bool:
    """Does the assertion still hold on the old code? It must not.

    The assertion is "a just-started lane is not an orphan". The old query
    returned it at every threshold, so the assertion fails — which is ``ok``.
    """
    module = revert((LIVENESS_ANCHOR, OLD_LIVENESS))

    async def body(repo: Any) -> bool:
        for threshold in (1, 5, 60, 900):
            if _ids(await repo.stale_lanes(older_than_seconds=threshold)) != []:
                return False
        return True

    return asyncio.run(_with_db(module, [_lane_row("never_beat", last_heartbeat=None)], body))


# --- 2. the fallback must use started_at, not created_at -------------------
@row(
    "test_the_fallback_prefers_started_at_over_created_at",
    "restore `last_heartbeat IS NULL OR last_heartbeat < cutoff`",
)
def check_2() -> bool:
    """A lane queued an hour ago but started a moment ago is not stale."""
    module = revert((LIVENESS_ANCHOR, OLD_LIVENESS))

    async def body(repo: Any) -> bool:
        return _ids(await repo.stale_lanes(older_than_seconds=60)) == []

    rows = [
        _lane_row(
            "queued_then_started",
            last_heartbeat=None,
            created_at=now() - timedelta(seconds=3600),
            started_at=now(),
        )
    ]
    return asyncio.run(_with_db(module, rows, body))


# --- 3. the naive over-correction ------------------------------------------
@row(
    "test_a_lane_that_started_long_ago_and_never_beat_is_stale",
    "revert to the over-correction: a missing heartbeat is an amnesty",
)
def check_3() -> bool:
    """Falsified in the other direction, deliberately.

    A harness that hung before its first heartbeat is the case orphan detection
    exists for. "Never report a heartbeat-less lane" fixes row 1 by introducing
    this bug, so this row has to fail against it.
    """
    module = revert((LIVENESS_ANCHOR, NAIVE_LIVENESS))

    async def body(repo: Any) -> bool:
        return _ids(await repo.stale_lanes(older_than_seconds=60)) == ["hung_at_start"]

    rows = [
        _lane_row(
            "hung_at_start",
            last_heartbeat=None,
            started_at=now() - timedelta(seconds=600),
        )
    ]
    return asyncio.run(_with_db(module, rows, body))


# --- 4. the write side -----------------------------------------------------
@row(
    "test_record_heartbeat_advances_the_stored_heartbeat",
    "restore a no-op `record_heartbeat` (the heartbeat only ever reached memory)",
)
def check_4() -> bool:
    """The detector reads records; a write that does nothing cannot help it."""
    module = revert((UPDATE_ANCHOR, NOOP_UPDATE))

    async def body(repo: Any) -> bool:
        before = _ids(await repo.stale_lanes(older_than_seconds=60)) == ["lane_hb"]
        await repo.record_heartbeat("lane_hb")
        after = _ids(await repo.stale_lanes(older_than_seconds=60)) == []
        return before and after

    rows = [_lane_row("lane_hb", last_heartbeat=now() - timedelta(hours=1))]
    return asyncio.run(_with_db(module, rows, body))


@row(
    "test_record_heartbeat_reports_whether_a_row_matched",
    "restore a `record_heartbeat` that always reports success",
)
def check_5() -> bool:
    module = revert((UPDATE_ANCHOR, NOOP_UPDATE))

    async def body(repo: Any) -> bool:
        return await repo.record_heartbeat("lane_does_not_exist") is False

    return asyncio.run(_with_db(module, [], body))


@row(
    "test_record_heartbeat_does_not_touch_other_columns",
    "make the heartbeat write also set `status`",
)
def check_6() -> bool:
    """A beat twelve times a minute must not clobber concurrent state."""
    module = revert((UPDATE_ANCHOR, GREEDY_UPDATE))

    async def body(repo: Any) -> bool:
        from openburrow.core.models.session import Lane

        await repo.record_heartbeat("lane_keep")
        lane = await repo.get(Lane, "lane_keep")
        return lane is not None and lane.status == "working"

    rows = [_lane_row("lane_keep", status="working")]
    return asyncio.run(_with_db(module, rows, body))


class _NullBus:
    """Swallows events. The row asserts on the database, not on the bus."""

    async def emit(self, **payload: Any) -> None:
        return None


class _LiveAdapter:
    """``is_running`` True, so ``_check_lane`` takes the healthy path."""

    is_running = True
    returncode = None


@row(
    "test_lane_heartbeat_reaches_the_record",
    "remove the heartbeat write — the beat only ever reached memory",
)
def check_7() -> bool:
    """The daemon half, falsified against its own file.

    Reverting the repository query would not touch this row: the record is
    unmaintained regardless of how the query is written, which is exactly why
    the two defects needed separate reverts.
    """
    module = revert_sessions((HEARTBEAT_WRITE, NO_HEARTBEAT_WRITE))

    async def run() -> bool:
        from types import SimpleNamespace

        from openburrow.core.models.session import Lane, LaneRole, LaneStatus

        with tempfile.TemporaryDirectory() as tmp:
            url = f"sqlite+aiosqlite:///{(Path(tmp) / 'burrow.db').as_posix()}"
            database = await init_database(url)
            try:
                lane = Lane(
                    session_id="sess_hb",
                    name="mock-1",
                    harness="mock",
                    role=LaneRole.IMPLEMENTER,
                    status=LaneStatus.IDLE,
                    owner="human",
                    started_at=now(),
                    last_heartbeat=now() - timedelta(hours=1),
                )
                async with database.session() as db_session:
                    await module.Repository(db_session).save(lane)

                config = SimpleNamespace(
                    policy=SimpleNamespace(),
                    paths=SimpleNamespace(repo_root=None),
                    settings=SimpleNamespace(governance_human_id="", governance_human_email=""),
                    adapters=SimpleNamespace(crash_restart="never", max_restarts=0),
                )
                manager = module.SessionManager(
                    config=config, database=database, bus=_NullBus(), registry=None
                )
                await manager._check_lane(module.RunningLane(lane=lane, adapter=_LiveAdapter()))

                async with database.session() as db_session:
                    still_stale = await module.Repository(db_session).stale_lanes(
                        older_than_seconds=60
                    )
                # The assertion is "the pass refreshed the record". If the write
                # is gone the lane is still an orphan, so the assertion fails.
                return still_stale == []
            finally:
                await close_all()

    return asyncio.run(run())


@row(
    "test_an_orphan_without_a_process_is_marked_crashed_and_not_retried",
    "never write the terminal row for an orphan this process cannot stop",
)
def check_8() -> bool:
    """The requeue loop, falsified against the branch that breaks it.

    The assertion is the *second* pass returning nothing. Reverting the terminal
    write makes the lane stay non-terminal and stay stale, so the second pass
    finds it again — which is the loop, and it makes the assertion fail.
    """
    module = revert_recovery((ORPHAN_TERMINAL_BRANCH, ORPHAN_TERMINAL_OFF))

    class _Sessions:
        """``stop_lane`` returns False, as it does for a lane from a dead daemon."""

        async def stop_lane(self, lane_id: str, *, reason: str = "") -> bool:
            return False

    class _Bus:
        async def emit(self, **payload: Any) -> None:
            return None

    async def run() -> bool:
        from types import SimpleNamespace

        from openburrow.core.config.repo_config import PolicyConfig
        from openburrow.core.models.session import Lane, LaneStatus

        with tempfile.TemporaryDirectory() as tmp:
            url = f"sqlite+aiosqlite:///{(Path(tmp) / 'burrow.db').as_posix()}"
            database = await init_database(url)
            try:
                lane = Lane(session_id="s1", name="alice", harness="mock", owner="human")
                lane.status = LaneStatus.IDLE
                lane.started_at = now() - timedelta(seconds=600)
                lane.last_heartbeat = now() - timedelta(seconds=600)
                async with database.session() as db_session:
                    await module.Repository(db_session).save(lane)
                    await db_session.commit()

                manager = module.RecoveryManager(
                    SimpleNamespace(
                        settings=SimpleNamespace(
                            recovery_auto_resume=False,
                            recovery_checkpoint_interval_s=300,
                            recovery_dead_letter_after=3,
                            recovery_silent_failure=True,
                            recovery_crash_log_keep=5,
                            recovery_circuit_threshold=5,
                            recovery_circuit_cooldown_s=300,
                            recovery_bulkhead_queue=16,
                        ),
                        policy=PolicyConfig(),
                        paths=SimpleNamespace(repo_root="."),
                    ),
                    database,
                    _Bus(),
                    sessions=_Sessions(),
                )

                first = await manager.requeue_orphans(older_than_s=60)
                second = await manager.requeue_orphans(older_than_s=60)
                # The assertion under test: the first pass reports the orphan,
                # the second must not.
                return [r["lane_id"] for r in first] == [lane.id] and second == []
            finally:
                await close_all()

    return asyncio.run(run())


CHECKS = [check_1, check_2, check_3, check_4, check_5, check_6, check_7, check_8]


def main() -> int:
    self_check()
    assert len(CHECKS) == len(ROWS), "row metadata is out of step with the checks"

    vacuous: list[str] = []
    weak: list[str] = []
    for (name, note), check in zip(ROWS, CHECKS, strict=True):
        raised = ""
        try:
            still_passes = check()
        except Exception as exc:
            # A revert that raises is a verdict (WEAK), not a crash of the run.
            still_passes = False
            raised = f"{type(exc).__name__}: {exc}"

        if raised:
            weak.append(name)
            verdict = "WEAK"
        elif still_passes:
            vacuous.append(name)
            verdict = "VACUOUS"
        else:
            verdict = "ok"

        print(f"  [{verdict:>7}] {name}")
        print(f"            reverted: {note}")
        if raised:
            print(f"            the revert raised instead of asserting: {raised}")

    print()
    if vacuous:
        print(f"{len(vacuous)} VACUOUS row(s) — the test does not discriminate:")
        for name in vacuous:
            print(f"  - {name}")
    if weak:
        print(f"{len(weak)} WEAK row(s) — the revert raised, so nothing was proven:")
        for name in weak:
            print(f"  - {name}")
    if vacuous or weak:
        return 1

    print(f"all {len(CHECKS)} rows discriminate: every test fails on the old code")
    return 0


if __name__ == "__main__":
    sys.exit(main())
