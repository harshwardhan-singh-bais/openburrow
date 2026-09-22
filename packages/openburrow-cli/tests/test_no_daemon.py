"""``--no-daemon`` says what it means.

The flag used to be decoration. Every command that needed live state dialled the
daemon anyway, and the failure hint printed

    Commands that do not need live lanes can run with --no-daemon.

which cannot be true for the command that printed it — that command is, by
construction, one that needs live lanes. A hint that is false for every command
that shows it is worse than no hint at all: it sends the reader looking for a
bug in the daemon when the answer is that the flag does not apply.

What the flag really covers is the commands that never call
:meth:`CliContext.require_daemon`: ``init``, ``doctor``, ``config``,
``adapters``, ``version``, and ``governance policy test``. Those read local
files or a local config, so the honest thing for a daemon-backed command to do
is stop and say so.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from openburrow.cli.context import CliContext
from openburrow.core.errors import DaemonNotRunningError
from openburrow.daemon.ipc import IpcClient

pytestmark = pytest.mark.unit


async def test_no_daemon_refuses_rather_than_dialling(tmp_path: Path) -> None:
    context = CliContext(no_daemon=True, repo_root=tmp_path)

    with pytest.raises(DaemonNotRunningError) as caught:
        await context.require_daemon()

    error = caught.value
    assert "--no-daemon" in error.message
    assert error.context["no_daemon"] is True
    # The hint must name commands that genuinely are offline, or it is the same
    # false promise in new words.
    assert "policy test" in (error.hint or "")


async def test_a_down_daemon_never_suggests_no_daemon_will_help(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def not_up(self: IpcClient) -> bool:
        return False

    monkeypatch.setattr(IpcClient, "ping", not_up)
    context = CliContext(repo_root=tmp_path)

    with pytest.raises(DaemonNotRunningError) as caught:
        await context.require_daemon()

    hint = caught.value.hint or ""
    assert "burrow daemon start" in hint
    assert "--no-daemon" not in hint
    # And it still names real offline commands, so the hint has an action in it.
    assert "doctor" in hint


async def test_a_successful_check_is_remembered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One ping per invocation, not one per call.

    A command with several client calls must not pay a round trip each time —
    and a refusal is deliberately *not* cached, so a daemon that comes up
    mid-command is noticed by the next call rather than reported as down
    forever.
    """
    calls = 0

    async def up(self: IpcClient) -> bool:
        nonlocal calls
        calls += 1
        return True

    monkeypatch.setattr(IpcClient, "ping", up)
    context = CliContext(repo_root=tmp_path)

    first = await context.require_daemon()
    second = await context.require_daemon()

    assert first is second
    assert calls == 1
