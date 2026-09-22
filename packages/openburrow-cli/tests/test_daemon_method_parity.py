"""The drift alarm: every method the CLI calls has a daemon handler.

For a long time the daemon registered 17 methods and the CLI called 40. Every
one of the missing 23 failed with ``no handler for '<method>'`` while the engine
behind it worked perfectly — complete, tested, and unreachable. Nothing caught
it, because every test in the project tested an engine, and the engines were
fine.

One assertion closes that gap permanently: the set of names the CLI sends minus
the set of names the daemon serves must be empty, checked by reading both sides
rather than by running either. The daemon side comes from
:meth:`~openburrow.daemon.server.Daemon.handler_map`, which is the same mapping
the daemon registers from — so this test cannot disagree with what a running
daemon actually serves.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from openburrow.cli.methods import cli_methods, unresolved_calls
from openburrow.daemon.server import Daemon

pytestmark = pytest.mark.unit

#: The 23 methods that had engine code and no handler, by name. Pinned so that a
#: scanner which silently stops finding anything cannot make this file pass: the
#: first assertion below would go green over an empty set of CLI methods.
PREVIOUSLY_UNREGISTERED: frozenset[str] = frozenset(
    {
        "approvals.list",
        "approvals.respond",
        "brain.add",
        "brain.diff",
        "brain.list",
        "brain.retire",
        "bus.message",
        "claim.create",
        "claim.list",
        "claim.release",
        "governance.audit",
        "governance.delegation_chain",
        "handoff.execute",
        "handoff.offer",
        "lessons.list",
        "lessons.retire",
        "plan.add_step",
        "plan.diff",
        "plan.get",
        "plan.remove_step",
        "plan.take",
        "reel.export",
        "report.generate",
    }
)


def build_daemon() -> Daemon:
    """A daemon with stub configuration, for its request surface only.

    ``Daemon.__init__`` stores its arguments and creates one event: it opens no
    database, binds no socket and starts no task. So the request surface can be
    read here without a daemon running, which is the whole reason it is exposed
    as a mapping.
    """
    config = SimpleNamespace(
        paths=SimpleNamespace(repo_root=Path()),
        settings=SimpleNamespace(),
    )
    return Daemon(config)  # type: ignore[arg-type]


def registered_methods() -> set[str]:
    return set(build_daemon().handler_map())


def test_cli_methods_are_registered() -> None:
    missing = cli_methods() - registered_methods()
    assert missing == set(), (
        "the CLI calls daemon methods that nothing serves — every command using one "
        f"of these fails with `no handler for ...`: {sorted(missing)}"
    )


def test_the_methods_that_were_missing_are_all_served() -> None:
    """Names, not just a count: this is what a broken scanner would hide."""
    assert registered_methods() >= PREVIOUSLY_UNREGISTERED
    assert cli_methods() >= PREVIOUSLY_UNREGISTERED


def test_no_method_name_is_assembled_at_runtime() -> None:
    """A dynamic name would escape the parity check above without failing it."""
    assert unresolved_calls() == []


def test_the_daemon_serves_the_scan_surface_too() -> None:
    """The check runs in both directions for the surface this pass added.

    A registered method nothing calls is dead weight; the point of listing these
    is that `burrow radar` provably has something behind it.
    """
    assert {"radar.scan", "radar.intents", "radar.status"} <= cli_methods()
    assert {"radar.scan", "radar.intents", "radar.status"} <= registered_methods()
