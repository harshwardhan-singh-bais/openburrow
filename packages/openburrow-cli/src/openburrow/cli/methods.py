"""The daemon methods the CLI calls, read out of the CLI's own source.

For a long time this project had a wiring gap nobody could see: the daemon
registered 17 methods and the CLI called 40. The other 23 had complete, tested
engine code behind them and no handler at all, so `burrow governance audit`
computed the accountability report correctly and then failed with
``no handler for 'governance.audit'``. Every test passed, because every test
tested an engine, and the engines worked.

The only way to see it was to run the command and read the error back. This
module exists so that a test can ask the same question the daemon asks —
*which method names does the CLI send?* — and compare it against
:meth:`openburrow.daemon.server.Daemon.handler_map`, without a socket, a
repository, or a single live lane.

**Read by parsing, not by running.** Importing the CLI and calling things would
need a daemon and a repo before a single method name could be collected, and the
question here is a question about the source text: the names are string literals
in ``call(...)`` expressions.

**A name that cannot be read is reported, not skipped.** Only string literals can
be checked statically. A method name assembled at runtime would silently escape a
parity check that quietly ignored it, so :func:`unresolved_calls` returns those
call sites by ``file:line`` and the drift test requires the list to be empty —
adding a dynamic method name therefore fails the test rather than the user's
command.
"""

from __future__ import annotations

import ast
from pathlib import Path

#: The directory this module lives in, which is the whole CLI surface.
CLI_PACKAGE_ROOT = Path(__file__).resolve().parent


def _iter_sources(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


def _scan(root: Path) -> tuple[set[str], list[str]]:
    methods: set[str] = set()
    unresolved: list[str] = []

    for path in _iter_sources(root):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr != "call":
                continue
            name = node.args[0] if node.args else None
            if isinstance(name, ast.Constant) and isinstance(name.value, str):
                methods.add(name.value)
            else:
                unresolved.append(f"{path.name}:{node.lineno}")

    return methods, unresolved


def cli_methods(root: Path | None = None) -> set[str]:
    """Every daemon method name the CLI asks for.

    ``root`` exists so the scanner can be pointed at a fixture directory in
    tests; in normal use the whole package is scanned.
    """
    return _scan(root or CLI_PACKAGE_ROOT)[0]


def unresolved_calls(root: Path | None = None) -> list[str]:
    """Call sites whose method name is not a string literal, as ``file:line``."""
    return _scan(root or CLI_PACKAGE_ROOT)[1]


__all__ = ["CLI_PACKAGE_ROOT", "cli_methods", "unresolved_calls"]
