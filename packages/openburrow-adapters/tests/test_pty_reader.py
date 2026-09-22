"""End-to-end tests for the PTY path.

These spawn a real child process on a real pseudo-terminal and drive it through
the adapter. They are marked ``integration`` because they touch the process
table.

Why they exist: the PTY path was *declared* — ``use_pty=True`` is hardcoded in
``build_spawn_spec``, both ``ptyprocess`` and ``pywinpty`` are dependencies
behind platform markers — but the Windows branch was never written, and the read
blocked the event loop on the platform where it was. Nothing in the test suite
had ever spawned a harness, so none of that was visible. A dependency list and a
boolean are not evidence that a code path runs.

The stand-in harness is ``pty_harness_stub.py``; see its docstring for why the
real binaries are not used.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from openburrow.adapters.harnesses.claude_code import ClaudeCodeAdapter
from openburrow.adapters.harnesses.generic import GenericCliAdapter
from openburrow.core.config.settings import Settings
from openburrow.core.models import Lane

pytestmark = pytest.mark.integration

STUB = Path(__file__).parent / "pty_harness_stub.py"
READ_TIMEOUT_S = 30.0


class ClaudePtyProbe(ClaudeCodeAdapter):
    """Claude Code's real frame mapping, driven by the stand-in harness.

    The Claude adapter rather than the generic base, because the frame mapping
    under test *is* the Claude one — the base would map the same frames to
    ``system``/``assistant`` and never broadcast them, which is the bug the
    override exists to fix. Running the real adapter end to end over a real PTY
    is the closest thing to running the real binary without an API key.

    ``structured_args`` is cleared so the stand-in does not receive flags it does
    not understand; ``binary`` points at the interpreter rather than ``claude``.
    """

    binary = sys.executable
    base_args: tuple[str, ...] = ("-u", str(STUB))
    structured_args = ()


def make_adapter(lane: Lane, *extra: str) -> GenericCliAdapter:
    adapter = ClaudePtyProbe(Settings(), lane=lane)
    adapter.base_args = ("-u", str(STUB), *extra)
    return adapter


def make_lane(tmp_path: Path) -> Lane:
    return Lane(
        name="pty-probe",
        harness="claude-code",
        session_id="sess_pty",
        worktree_path=str(tmp_path),
    )


async def drain(adapter: GenericCliAdapter) -> list:
    """Collect output until the child exits, with a hard timeout.

    The timeout is not decoration: if the reader regresses, the failure mode is
    a hang, and a hanging test that reports nothing is worse than a failing one.
    """
    outputs = []
    async with asyncio.timeout(READ_TIMEOUT_S):
        async for output in adapter.read_output():
            outputs.append(output)
    return outputs


class TestPtySpawn:
    async def test_pty_backend_is_actually_used(self, tmp_path: Path) -> None:
        """The regression guard: a silent fallback to a pipe looks identical.

        ``use_pty=True`` was requested for every harness, but on Windows the
        backend import always failed, so ``_start_plain`` ran instead and the
        harness saw a pipe. Nothing surfaced — the process worked, just degraded
        in the exact way the PTY exists to prevent. Asserting the backend is a
        PTY is the only thing that distinguishes the two.
        """
        lane = make_lane(tmp_path)
        adapter = make_adapter(lane)
        await adapter.start(lane)
        try:
            assert adapter._pty is not None, "fell back to a plain pipe"
            module = type(adapter._pty).__module__
            assert module.startswith(("winpty", "ptyprocess")), module
        finally:
            await adapter.stop(force=True)

    async def test_round_trip_through_a_real_pty(self, tmp_path: Path) -> None:
        lane = make_lane(tmp_path)
        adapter = make_adapter(lane)
        await adapter.start(lane)
        await adapter.send_prompt(lane, "hello")
        try:
            outputs = await drain(adapter)
        finally:
            await adapter.stop(force=True)

        kinds = [o.kind for o in outputs]
        texts = [o.text for o in outputs]

        assert "status" in kinds, kinds
        assert "result" in kinds, kinds
        assert "ready" in texts, texts
        assert any("echo: hello" in t for t in texts), texts
        assert any(o.terminal for o in outputs)

    async def test_pty_frames_are_parsed_as_json(self, tmp_path: Path) -> None:
        """Frames must survive the PTY's refusal to align reads to lines.

        The invariant is not "every output is structured" — a PTY echoes what is
        written to it, so the prompt itself comes back as a plain ``text`` output.
        That echo is real terminal behaviour rather than harness output, and
        suppressing it would mean guessing which lines are ours. The invariant
        that matters is that no *raw* frame reaches a consumer: a line that
        starts with ``{`` is an unparsed frame, and a consumer receiving one sees
        the wire format instead of the message.
        """
        lane = make_lane(tmp_path)
        adapter = make_adapter(lane)
        await adapter.start(lane)
        await adapter.send_prompt(lane, "hello")
        try:
            outputs = await drain(adapter)
        finally:
            await adapter.stop(force=True)

        assert outputs, "the PTY stream produced no output at all"

        structured = [o for o in outputs if o.structured]
        assert len(structured) >= 4, [o.kind for o in outputs]
        assert not any(o.text.lstrip().startswith("{") for o in outputs), (
            "a raw JSON frame reached the bus: "
            + repr([o.text for o in outputs if o.text.lstrip().startswith("{")])
        )

    async def test_pty_output_carries_no_escape_sequences(self, tmp_path: Path) -> None:
        """A real terminal emits capability queries before any real output."""
        lane = make_lane(tmp_path)
        adapter = make_adapter(lane)
        await adapter.start(lane)
        await adapter.send_prompt(lane, "hello")
        try:
            outputs = await drain(adapter)
        finally:
            await adapter.stop(force=True)

        for output in outputs:
            assert "\x1b" not in output.text, repr(output.text)
            assert "\r" not in output.text, repr(output.text)

    async def test_usage_is_recorded_from_pty_frames(self, tmp_path: Path) -> None:
        lane = make_lane(tmp_path)
        adapter = make_adapter(lane)
        await adapter.start(lane)
        await adapter.send_prompt(lane, "hello")
        try:
            await drain(adapter)
        finally:
            await adapter.stop(force=True)

        # 3/2 ready, 1/1 echo, 5/4 result.
        assert lane.tokens_in == 9
        assert lane.tokens_out == 7
        # Cumulative, so it must not be summed into the accumulating counter.
        assert lane.cost_usd == 0.0

    async def test_session_id_is_captured_from_the_init_frame(self, tmp_path: Path) -> None:
        lane = make_lane(tmp_path)
        adapter = make_adapter(lane)
        await adapter.start(lane)
        await adapter.send_prompt(lane, "hello")
        try:
            await drain(adapter)
        finally:
            await adapter.stop(force=True)

        assert lane.metadata["harness_session_id"] == "sess_pty_stub"


class TestReaderDoesNotBlock:
    async def test_event_loop_keeps_running_while_reading(self, tmp_path: Path) -> None:
        """The blocking-read regression.

        ``pty.read`` is blocking in both backends. Called directly from the async
        generator it parked the daemon's whole event loop — one idle harness
        stopped every other lane from being scheduled. The failure is invisible
        from outside: the loop is alive, it simply never advances, and no
        exception is raised.

        A heartbeat on the same loop is what makes it visible. With a blocking
        read, the heartbeat cannot tick while a read is in flight.
        """
        lane = make_lane(tmp_path)
        adapter = make_adapter(lane)
        await adapter.start(lane)

        ticks = 0

        async def heartbeat() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        beat = asyncio.create_task(heartbeat())
        try:
            await adapter.send_prompt(lane, "hello")
            outputs = await drain(adapter)
        finally:
            beat.cancel()
            await adapter.stop(force=True)

        assert outputs
        assert ticks > 5, f"the event loop was starved while reading ({ticks} ticks)"


class TestShutdown:
    async def test_force_stop_ends_a_running_child(self, tmp_path: Path) -> None:
        """``stop(force=True)`` used ``signal.SIGKILL``, which Windows lacks.

        The resulting ``AttributeError`` was swallowed by the shutdown ``try``
        and the PTY reference was dropped anyway, so the harness kept running
        while ``stop`` reported success — a leak that only shows up as descriptor
        exhaustion days later.
        """
        lane = make_lane(tmp_path)
        adapter = make_adapter(lane, "long")
        await adapter.start(lane)
        assert adapter.is_running

        await adapter.stop(force=True)

        assert not adapter.is_running
        assert adapter._pty is None, "the PTY handle was not released"

    async def test_stop_is_idempotent(self, tmp_path: Path) -> None:
        lane = make_lane(tmp_path)
        adapter = make_adapter(lane, "long")
        await adapter.start(lane)

        await adapter.stop(force=True)
        await adapter.stop(force=True)

        assert not adapter.is_running
