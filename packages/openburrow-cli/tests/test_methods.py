"""The method scanner, tested on its own terms.

A drift alarm is only worth having if it cannot pass by accident. The scanner
reads string literals out of ``call(...)`` expressions, so the two ways it could
quietly stop working are (1) finding nothing at all and (2) skipping a call site
whose method name it cannot read. Both are asserted here, against fixture
directories rather than against the package, so the assertion is about the
scanner and not about today's source tree.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from openburrow.cli.methods import cli_methods, unresolved_calls

pytestmark = pytest.mark.unit


def write_module(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text(body, encoding="utf-8")
    return path


def test_a_literal_call_is_collected(tmp_path: Path) -> None:
    write_module(tmp_path, "mod.py", 'await client.call("session.list", open_only=True)\n')
    assert cli_methods(tmp_path) == {"session.list"}


def test_a_call_split_across_lines_is_collected(tmp_path: Path) -> None:
    write_module(
        tmp_path,
        "mod.py",
        "result = await client.call(\n    'brain.diff',\n    session=session_id,\n)\n",
    )
    assert cli_methods(tmp_path) == {"brain.diff"}


def test_an_unrelated_method_named_call_is_not_mistaken_for_a_daemon_call(tmp_path: Path) -> None:
    write_module(tmp_path, "mod.py", "handler.call_soon(print)\nreader.readline()\n")
    assert cli_methods(tmp_path) == set()
    assert unresolved_calls(tmp_path) == []


def test_a_name_built_at_runtime_is_reported_rather_than_ignored(tmp_path: Path) -> None:
    path = write_module(tmp_path, "mod.py", "\n\nawait client.call(f'{prefix}.list')\n")
    assert cli_methods(tmp_path) == set()
    assert unresolved_calls(tmp_path) == [f"{path.name}:3"]


def test_the_real_package_yields_the_whole_surface() -> None:
    """The scanner over the CLI as it actually is.

    ``len >= 40`` is pinned deliberately: ``update.md`` records 40 methods at the
    point the daemon served only 17 of them, so a scanner that suddenly returns
    two has broken rather than found a smaller CLI.
    """
    methods = cli_methods()

    assert unresolved_calls() == []
    assert len(methods) >= 40, sorted(methods)
    assert {
        "session.create",
        "bus.tail",
        "governance.audit",
        "plan.add_step",
        "reel.export",
        "radar.scan",
    } <= methods
