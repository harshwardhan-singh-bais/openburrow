#!/usr/bin/env python3
"""Fail CI when the TypeScript enum mirrors drift from the Python originals.

Why this exists
---------------
``apps/web/src/types/openburrow.ts`` is hand-written. That is a deliberate choice
with a cost: the generated alternative would be correct and unreadable, and the
parts of that file that matter are the *relationships* — which states are
terminal, which transitions are legal — not the string literals.

The cost is that nothing stops the two from diverging. And they did: the first
run of this script found ``PERFORMATIVE_REPLIES`` in TypeScript claiming that
``reject`` and ``withdraw`` are dead ends, while the Python it mirrors says they
are answered by ``propose`` and ``inform``. That is not a cosmetic difference —
it would have disabled the "counter this" action in the frontend in exactly the
case where it is most useful, and no test would have caught it because both
languages were internally consistent.

So this script is the enforcement. It parses the arrays out of the TypeScript
with a regex rather than with a TypeScript parser, because the shapes it reads
are fixed and a Node toolchain dependency would make the check unavailable in
exactly the environments that most need it (a Python-only CI job, a pre-commit
hook on a machine without node_modules).

Usage
-----
    python scripts/check_enum_parity.py            # check, exit 1 on drift
    python scripts/check_enum_parity.py --verbose  # print every comparison

Exit codes
----------
    0   every mirror matches
    1   at least one mirror has drifted
    2   the check could not run (missing file, unparseable TypeScript)
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any, NoReturn

REPO_ROOT = Path(__file__).resolve().parent.parent
TS_TYPES = REPO_ROOT / "apps" / "web" / "src" / "types" / "openburrow.ts"


def cannot_run(message: str) -> NoReturn:
    """Report a harness-level failure and exit 2.

    Distinct from drift on purpose: "the two implementations disagree" and "this
    check could not run" need different responses from whoever reads the output,
    and collapsing both into exit 1 makes a missing toolchain look like a code
    defect. `raise SystemExit("message")` exits 1, which is why this is a helper
    rather than a bare raise.
    """
    print(message, file=sys.stderr)
    raise SystemExit(2)


# ---------------------------------------------------------------------------
# Reading the TypeScript
# ---------------------------------------------------------------------------


def read_typescript() -> str:
    if not TS_TYPES.is_file():
        cannot_run(f"error: {TS_TYPES} not found; run this from the repo root")
    return TS_TYPES.read_text(encoding="utf-8")


def strip_comments(source: str) -> str:
    """Remove comments so a commented-out member cannot be read as a real one.

    Deliberately naive about strings — a `//` inside a string literal would be
    mis-handled — because the only strings in these declarations are the enum
    members themselves, and none of them contain a slash.
    """
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", "", source)


def extract_string_array(source: str, name: str) -> list[str] | None:
    """`export const NAME = ["a", "b"] as const;` → `["a", "b"]`."""
    pattern = re.compile(
        r"export\s+const\s+" + re.escape(name) + r"\s*=\s*\[(.*?)\]\s*as\s+const",
        re.DOTALL,
    )
    match = pattern.search(source)
    if match is None:
        return None
    return re.findall(r'"([^"]*)"', match.group(1))


def _balanced_object(source: str, start: int) -> str | None:
    """The `{...}` body starting at or after `start`, brace-balanced."""
    open_at = source.find("{", start)
    if open_at < 0:
        return None
    depth = 0
    for index in range(open_at, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[open_at + 1 : index]
    return None


def extract_record(source: str, name: str) -> dict[str, list[str]] | None:
    """`export const NAME: Record<K, readonly K[]> = { key: ["a"], ... }`."""
    match = re.search(r"export\s+const\s+" + re.escape(name) + r"\s*:", source)
    if match is None:
        return None
    body = _balanced_object(source, match.end())
    if body is None:
        return None

    record: dict[str, list[str]] = {}
    # Keys may be bare identifiers or quoted; members are always quoted strings.
    for entry in re.finditer(r'"?([A-Za-z_][A-Za-z0-9_]*)"?\s*:\s*\[(.*?)\]', body, re.DOTALL):
        record[entry.group(1)] = re.findall(r'"([^"]*)"', entry.group(2))
    return record


def extract_string_set(source: str, name: str) -> set[str] | None:
    """A `readonly K[]` that is not `as const`, e.g. `TERMINAL_TASK_STATES`."""
    pattern = re.compile(
        r"export\s+const\s+" + re.escape(name) + r"\s*:\s*readonly[^=]*=\s*\[(.*?)\]",
        re.DOTALL,
    )
    match = pattern.search(source)
    if match is None:
        return None
    return set(re.findall(r'"([^"]*)"', match.group(1)))


# ---------------------------------------------------------------------------
# Reading the Python
# ---------------------------------------------------------------------------


def load_python_enums() -> dict[str, Any]:
    """Import the real enums.

    No `sys.path` surgery: the packages are installed editable by `uv sync`, so
    the import either works or the environment is not set up — and saying that
    clearly is better than silently falling back to a file-path guess that might
    read a *different* copy of the module.
    """
    try:
        # Imported here rather than at module scope so that a missing package
        # produces the sentence below instead of a bare ImportError traceback at
        # load time. A check whose failure mode is "it crashed" is a check people
        # remove from CI.
        from openburrow.core.models import enums
    except ImportError as exc:  # pragma: no cover - environment dependent
        cannot_run(
            f"error: could not import openburrow.core.models.enums ({exc}).\n"
            "       Run `uv sync` first — this check compares against the installed package."
        )
    return {
        "TaskState": [member.value for member in enums.TaskState],
        "TERMINAL_TASK_STATES": {member.value for member in enums.TERMINAL_TASK_STATES},
        "BLOCKING_TASK_STATES": {member.value for member in enums.BLOCKING_TASK_STATES},
        "TASK_TRANSITIONS": {
            source.value: sorted(target.value for target in targets)
            for source, targets in enums.TASK_TRANSITIONS.items()
        },
        "Performative": [member.value for member in enums.Performative],
        "PERFORMATIVE_REPLIES": {
            source.value: sorted(target.value for target in targets)
            for source, targets in enums.PERFORMATIVE_REPLIES.items()
        },
        "LaneStatus": [member.value for member in enums.LaneStatus],
        "SessionStatus": [member.value for member in enums.SessionStatus],
        "LaneRole": [member.value for member in enums.LaneRole],
        "StepStatus": [member.value for member in enums.StepStatus],
        "DelegationStatus": [member.value for member in enums.DelegationStatus],
        "ApprovalStatus": [member.value for member in enums.ApprovalStatus],
    }


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


class Drift:
    def __init__(self, name: str, detail: str) -> None:
        self.name = name
        self.detail = detail

    def __str__(self) -> str:
        return f"{self.name}: {self.detail}"


def compare_sequence(name: str, python: list[str], typescript: list[str] | None) -> Drift | None:
    if typescript is None:
        return Drift(name, "not found in the TypeScript (was it renamed or removed?)")
    if python != typescript:
        missing = [item for item in python if item not in typescript]
        extra = [item for item in typescript if item not in python]
        parts = []
        if missing:
            parts.append(f"missing from TypeScript: {missing}")
        if extra:
            parts.append(f"present in TypeScript but not in Python: {extra}")
        if not parts:
            # Same members, different order. Worth reporting: the arrays are
            # rendered in order in the UI's filter chips.
            parts.append(
                f"same members, different order\n      py: {python}\n      ts: {typescript}"
            )
        return Drift(name, "; ".join(parts))
    return None


def compare_set(name: str, python: set[str], typescript: set[str] | None) -> Drift | None:
    if typescript is None:
        return Drift(name, "not found in the TypeScript (was it renamed or removed?)")
    if python != typescript:
        missing = sorted(python - typescript)
        extra = sorted(typescript - python)
        parts = []
        if missing:
            parts.append(f"missing from TypeScript: {missing}")
        if extra:
            parts.append(f"present in TypeScript but not in Python: {extra}")
        return Drift(name, "; ".join(parts))
    return None


def compare_mapping(
    name: str,
    python: dict[str, list[str]],
    typescript: dict[str, list[str]] | None,
) -> list[Drift]:
    if typescript is None:
        return [Drift(name, "not found in the TypeScript (was it renamed or removed?)")]

    drifts: list[Drift] = []
    for key, expected in python.items():
        actual = typescript.get(key)
        if actual is None:
            drifts.append(Drift(f"{name}.{key}", "no such key in the TypeScript"))
            continue
        if sorted(expected) != sorted(actual):
            missing = sorted(set(expected) - set(actual))
            extra = sorted(set(actual) - set(expected))
            parts = []
            if missing:
                parts.append(f"missing: {missing}")
            if extra:
                parts.append(f"extra: {extra}")
            drifts.append(Drift(f"{name}.{key}", "; ".join(parts)))

    for key in typescript:
        if key not in python:
            drifts.append(Drift(f"{name}.{key}", "present in TypeScript but not in Python"))

    return drifts


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--verbose", action="store_true", help="print every comparison, not just drift"
    )
    args = parser.parse_args(argv)

    source = strip_comments(read_typescript())
    python = load_python_enums()

    drifts: list[Drift] = []
    checked = 0

    # --- sequences --------------------------------------------------------
    sequence_pairs = [
        ("TASK_STATES", python["TaskState"]),
        ("PERFORMATIVES", python["Performative"]),
        ("LANE_STATUSES", python["LaneStatus"]),
        ("SESSION_STATUSES", python["SessionStatus"]),
        ("LANE_ROLES", python["LaneRole"]),
        ("STEP_STATUSES", python["StepStatus"]),
        ("DELEGATION_STATUSES", python["DelegationStatus"]),
        ("APPROVAL_STATUSES", python["ApprovalStatus"]),
    ]
    for ts_name, expected in sequence_pairs:
        checked += 1
        drift = compare_sequence(ts_name, expected, extract_string_array(source, ts_name))
        if drift:
            drifts.append(drift)
        elif args.verbose:
            print(f"  ok  {ts_name} ({len(expected)} members)")

    # --- sets -------------------------------------------------------------
    set_pairs = [
        ("TERMINAL_TASK_STATES", python["TERMINAL_TASK_STATES"]),
        ("BLOCKING_TASK_STATES", python["BLOCKING_TASK_STATES"]),
    ]
    for ts_name, expected in set_pairs:
        checked += 1
        drift = compare_set(ts_name, expected, extract_string_set(source, ts_name))
        if drift:
            drifts.append(drift)
        elif args.verbose:
            print(f"  ok  {ts_name} ({len(expected)} members)")

    # --- mappings ---------------------------------------------------------
    mapping_pairs = [
        ("TASK_TRANSITIONS", python["TASK_TRANSITIONS"]),
        ("PERFORMATIVE_REPLIES", python["PERFORMATIVE_REPLIES"]),
    ]
    for ts_name, expected in mapping_pairs:
        checked += 1
        found = compare_mapping(ts_name, expected, extract_record(source, ts_name))
        if found:
            drifts.extend(found)
        elif args.verbose:
            print(f"  ok  {ts_name} ({len(expected)} keys)")

    if drifts:
        print(
            f"enum parity: {len(drifts)} drift(s) across {checked} declarations\n", file=sys.stderr
        )
        for drift in drifts:
            print(f"  ✗ {drift}", file=sys.stderr)
        print(
            "\nFix apps/web/src/types/openburrow.ts to match "
            "packages/openburrow-core/src/openburrow/core/models/enums.py.\n"
            "If the Python changed intentionally, mirror it; the Python is the origin.",
            file=sys.stderr,
        )
        return 1

    print(f"enum parity: {checked} declarations match")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
