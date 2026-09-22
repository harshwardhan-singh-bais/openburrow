"""Fail if a documented `burrow` command does not exist.

Why this gate exists
--------------------
Four documents — `README.md`, `docs/cli/README.md`, `docs/web/README.md` and
`docs/deployment/README.md` — instructed readers to run a command surface that
did not exist. Measured against the installed CLI, all of these exit 2:

    burrow watch          burrow report       burrow audit       burrow tui
    burrow policy         burrow brain        burrow lessons     burrow take
    burrow lane start     burrow ask          burrow relay serve

The real names are grouped — `burrow observability watch`,
`burrow governance audit`, `burrow session ask`, `burrow knowledge brain`. Every
one of those wrong forms is a *plausible* name, which is what let them survive:
nothing reads documentation, so nothing noticed that the Quick Start failed on
its third line. `burrow session start --template pair` was invented outright.

The failure mode is the one this project audits for elsewhere: a promise the
project does not keep. A reader who follows the docs gets a usage error and no
way to tell whether the tool is broken or the page is.

How it works
------------
Parse fenced code blocks line by line (toggling on every fence, so a ```mermaid
block cannot swallow the ```bash block after it), take the leading run of
lowercase words after `burrow` as the command path, and check it against the
CLI's real command tree — built by introspecting the Typer app rather than by
spawning a subprocess per command, which keeps this under a second.

Positional arguments are the wrinkle. `burrow completion bash` is valid: `bash`
is an *argument* to `completion`, not a subcommand. So a path that is not itself
a command is accepted when its longest known prefix is a command that takes
arguments — that is, one with no subcommands of its own. `burrow session strat`
is still rejected, because `session` *does* have subcommands, so a second word
after it must be one of them.

Deliberately not checked: flags. A flag list in prose drifts for the same
reason, but flags are per-subcommand and the extraction is ambiguous (`--json`
is global, `--lanes` is not). Commands are the part a reader cannot guess.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Every document that tells a reader what to type.
DOCS = (
    "README.md",
    "docs/README.md",
    "docs/cli/README.md",
    "docs/guides/running.md",
    "docs/web/README.md",
    "docs/deployment/README.md",
    "CONTRIBUTING.md",
)

#: A command word: lowercase, no punctuation. Arguments, flags, placeholders and
#: shell operators all fail this test, which is what ends the command path.
_WORD = re.compile(r"^[a-z][a-z0-9-]*$")

_FENCE = re.compile(r"^\s*```")


def command_tree() -> tuple[set[str], set[str]]:
    """``(every command path, the paths that have subcommands)``."""
    import typer.main

    from openburrow.cli.main import app

    paths: set[str] = set()
    groups: set[str] = set()

    def walk(command: object, prefix: str) -> None:
        children = getattr(command, "commands", {})
        if children and prefix:
            groups.add(prefix)
        for name, sub in children.items():
            path = f"{prefix} {name}".strip()
            paths.add(path)
            walk(sub, path)

    walk(typer.main.get_command(app), "")
    return paths, groups


def resolve(document: str, known: set[str], groups: set[str]) -> str | None:
    """Return a reason the documented command is wrong, or ``None`` if it is fine."""
    if document in known:
        return None

    # Not a command itself: accept it only if a prefix of it is a command that
    # takes positional arguments, and everything after that prefix is argument.
    words = document.split()
    for cut in range(len(words) - 1, 0, -1):
        prefix = " ".join(words[:cut])
        if prefix in known:
            if prefix in groups:
                return f"`{prefix}` takes a subcommand, and `{words[cut]}` is not one"
            return None
    return f"`{words[0]}` is not a command"


def burrow_lines(text: str) -> list[tuple[int, str]]:
    """``(line number, line)`` for every `burrow …` invocation inside a fence.

    Line-by-line rather than one regex over the whole file. An earlier version
    used ``re.findall(r"```(?:bash)?\\n(.*?)```")``, which pairs fences wrongly:
    the closing fence of a ```mermaid block matches, and the next match runs to
    the opening fence of the ```bash block, so that block is consumed as a
    terminator and its contents are never examined. The bug is invisible — the
    check simply reports fewer commands, and passes.
    """
    found: list[tuple[int, str]] = []
    in_fence = False
    for number, raw in enumerate(text.splitlines(), start=1):
        if _FENCE.match(raw):
            in_fence = not in_fence
            continue
        if not in_fence:
            continue
        line = raw.strip()
        if line.startswith("$ "):
            line = line[2:].strip()
        if line.startswith("burrow "):
            found.append((number, line))
    return found


def command_path(line: str) -> str:
    """The command path from an invocation, or ``''`` if there is not one."""
    words: list[str] = []
    for word in line.split()[1:]:
        if not _WORD.match(word):
            break
        words.append(word)
    return " ".join(words)


def main() -> int:
    known, groups = command_tree()
    if not known:  # a gate that examined nothing would pass
        print("could not read the command tree — is the workspace installed?", file=sys.stderr)
        return 2

    print(f"  command tree: {len(known)} paths, {len(groups)} of them groups")
    checked = 0
    failures: list[str] = []

    for relative in DOCS:
        path = REPO_ROOT / relative
        if not path.is_file():
            continue
        bad_here = 0
        for number, line in burrow_lines(path.read_text(encoding="utf-8")):
            command = command_path(line)
            if not command:
                continue
            checked += 1
            problem = resolve(command, known, groups)
            if problem is not None:
                bad_here += 1
                failures.append(f"{relative}:{number}: `burrow {command}` — {problem}")
        status = "ok " if bad_here == 0 else "BAD"
        print(f"  {status} {relative}")

    if failures:
        print()
        for failure in failures:
            print(f"  ! {failure}")
        print(
            f"\n  {len(failures)} documented command(s) do not exist. "
            "Run `burrow --help` and use the grouped name."
        )
        return 1

    print(f"\n  {checked} documented command invocations all resolve")
    return 0


if __name__ == "__main__":
    sys.exit(main())
