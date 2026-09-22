#!/usr/bin/env python3
"""Fail CI when the TypeScript daemon endpoint drifts from the Python original.

Why this exists
---------------
``apps/web/src/lib/daemon-bridge.ts`` re-derives the daemon's control-plane
endpoint by hand, because the browser cannot dial a named pipe and the route
handlers are the bridge. That mirror has now been wrong twice, and both times
the failure was completely silent: the daemon listened on one endpoint, the
dashboard dialled another, and the only symptom was "daemon unreachable" with
nothing anywhere naming the endpoint.

The two defects:

1. The Windows pipe name omitted the ``sha256(repo_root)[:12]`` suffix, so the
   dashboard dialled a pipe no daemon had ever created — on every machine, every
   time.
2. ``OPENBURROW_HOME`` was passed to ``path.resolve`` without expanding a leading
   ``~``, so ``~/obhome`` resolved inside the repo instead of in the home
   directory.

Neither was caught by a comment asserting compatibility, and neither could have
been. The endpoint is a *computed value* — a hash of a path — so unlike the enum
mirrors in ``check_enum_parity.py`` it cannot be read out of the TypeScript with
a regex. This script therefore does the only thing that can work: it **runs both
implementations over the same environment and compares the resulting strings**.

That is a deliberate difference from ``check_enum_parity.py``, which avoids Node
on purpose. Its inputs are literals; these are not.

How both branches get exercised
-------------------------------
``resolveDaemonEndpoint`` picks its transport from a module-scope constant, so
the POSIX branch — the one Docker and Linux run — would otherwise never execute
on a Windows host. ``scripts/_web_posix_hook.mjs`` rewrites that one line in the
*loaded* source via a ``module.register()`` load hook, and the probe asserts the
rewrite applied before comparing anything. The Python side is forced the same way
by patching ``paths.is_windows``. Both sides then compute their POSIX logic over
the same Windows path primitives, so the comparison is like-for-like about
structure without pretending to be a Linux box.

Usage
-----
    python scripts/check_endpoint_parity.py               # check, exit 1 on drift
    python scripts/check_endpoint_parity.py --verbose      # print every comparison
    python scripts/check_endpoint_parity.py --unicode-wide # also scan all of Unicode

Exit codes
----------
    0   every scenario agrees
    1   at least one scenario has drifted
    2   the check could not run (no Node, unreadable bridge, probe failure)
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import NoReturn

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "scripts" / "_web_endpoint_probe.mjs"
BRIDGE = REPO_ROOT / "apps" / "web" / "src" / "lib" / "daemon-bridge.ts"

#: Every variable either implementation reads. Cleared between scenarios so a
#: value from one cannot leak into the next and make a divergence look like
#: agreement.
MANAGED_ENV = (
    "OPENBURROW_REPO_ROOT",
    "OPENBURROW_HOME",
    "OPENBURROW_GLOBAL_HOME",
    "OPENBURROW_DAEMON_SOCKET",
)

#: Fields compared for every scenario.
FIELDS = ("repo_root", "endpoint", "reels", "global")


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
# Scenarios
# ---------------------------------------------------------------------------


@dataclass
class Scenario:
    name: str
    posix: bool = False
    repo: str | None = None
    env: dict[str, str | None] = field(default_factory=dict)


#: Divergences that are known, understood, and accepted. Keyed by
#: ``(scenario, field)``.
#:
#: These are asserted to **still diverge**. If one of them starts agreeing, the
#: check fails and someone has to delete the entry deliberately — the same
#: discipline as the ``*_assumed_*`` fixtures elsewhere in this repo. A
#: limitation that quietly disappears is a limitation nobody re-examined.
KNOWN_DIVERGENCES: dict[tuple[str, str], str] = {
    ("posix/home-with-dotdot", "endpoint"): (
        "Python builds a relative OPENBURROW_HOME with `root / runtime` and does "
        "not normalise it, while `path.resolve` on the TypeScript side does. "
        "`a/../b` therefore names a different (but equivalent) directory. Only "
        "reachable from a pathological config, and `..` in a runtime dir is not a "
        "shape worth making the two sides agree on."
    ),
    ("posix/home-with-dotdot", "reels"): "same root cause as the endpoint above",
}

_CASE_DIVERGENCE_REASON = (
    "Python's `Path.resolve()` returns the on-disk canonical casing on Windows, "
    "while `path.resolve` in Node is purely lexical and preserves whatever it was "
    "given — so a repo root of `C:\\USERS\\...` comes back canonicalised in Python "
    "and unchanged in TypeScript. Harmless where it occurs: Windows filesystems "
    "are case-insensitive so both strings name the same directory, and on POSIX "
    "`resolve()` preserves case, so the two already agree there. The load-bearing "
    "part — the pipe digest — lowercases before hashing and is unaffected, which "
    "is why only `repo_root` and `reels` appear here and `endpoint` does not. "
    "Deliberately not 'fixed' by resolving through the filesystem on this side: "
    "that would make `node:fs` an eager import, and this module keeps it lazy on "
    "purpose (see `existsSyncSafe`)."
)
for _scenario in ("win/repo-upper-case", "win/repo-lower-case"):
    for _field_name in ("repo_root", "reels"):
        KNOWN_DIVERGENCES[(_scenario, _field_name)] = _CASE_DIVERGENCE_REASON


#: Capital sigma, small sigma and final sigma. The final-sigma distinction is a
#: case-mapping test case worth having, but RUF001 flags a literal small sigma as
#: an ambiguous character — and a noqa directive on that line does not survive
#: ``ruff format``, which reflows the list one item per line and leaves the
#: directive attached to the wrong entry. Building them from codepoints is
#: unambiguous and formatter-proof.
GREEK_SIGMAS = [chr(0x03A3), chr(0x03C3), chr(0x03C2)]


def case_probes() -> list[str]:
    """The repo-path components used to exercise the pipe digest's case mapping.

    Letters are exhaustive rather than sampled, because case mapping is the whole
    question. Punctuation is there as a control: it must pass through untouched.
    The non-ASCII set is the realistic set — the characters that appear in real
    names and place names — plus ``ß``, which is the one that actually diverged.
    """
    letters = [chr(c) for c in range(ord("A"), ord("Z") + 1)]
    letters += [chr(c) for c in range(ord("a"), ord("z") + 1)]
    digits = list("0123456789")
    punctuation = list("-_.+()[]#&!$%',;=@~")
    non_ascii = [
        "ß",
        "ẞ",
        "straße",
        "STRAßE",
        "Straße",
        "Ü",
        "ü",
        "MÜNCHEN",
        "münchen",
        "Ä",
        "ä",
        "Ö",
        "ö",
        "É",
        "é",
        "Ñ",
        "ñ",
        "Å",
        "å",
        "Ø",
        "ø",
        "Æ",
        "æ",
        "Œ",
        "œ",
        "İstanbul",
        "istanbul",
        *GREEK_SIGMAS,
    ]
    return letters + digits + punctuation + non_ascii


def build_scenarios(repo: str) -> list[Scenario]:
    scenarios: list[Scenario] = []

    # --- the Windows branch, which is the live one on this host -------------
    scenarios += [
        Scenario("win/default"),
        Scenario("win/repo-trailing-slash", repo=repo + "/"),
        Scenario("win/repo-dot-segments", repo=repo + "/../" + Path(repo).name),
        Scenario("win/repo-upper-case", repo=repo.upper()),
        Scenario("win/repo-lower-case", repo=repo.lower()),
        Scenario("win/socket-override", env={"OPENBURROW_DAEMON_SOCKET": r"\\.\pipe\custom-name"}),
        Scenario("win/socket-override-tilde", env={"OPENBURROW_DAEMON_SOCKET": "~/custom.sock"}),
        Scenario("win/home-relative", env={"OPENBURROW_HOME": "custom-runtime"}),
        Scenario("win/home-absolute", env={"OPENBURROW_HOME": "C:/abs/runtime"}),
        Scenario("win/home-tilde", env={"OPENBURROW_HOME": "~/obhome"}),
        Scenario("win/home-whitespace", env={"OPENBURROW_HOME": "   "}),
        Scenario("win/global-absolute", env={"OPENBURROW_GLOBAL_HOME": "C:/global/home"}),
        Scenario("win/global-tilde", env={"OPENBURROW_GLOBAL_HOME": "~/globalhome"}),
    ]

    # --- the POSIX branch, forced, because Docker and Linux run it ----------
    scenarios += [
        Scenario("posix/default", posix=True),
        Scenario("posix/repo-tilde", posix=True, repo="~/some-repo"),
        Scenario("posix/home-relative", posix=True, env={"OPENBURROW_HOME": "custom-runtime"}),
        Scenario("posix/home-nested", posix=True, env={"OPENBURROW_HOME": "nested/deep"}),
        Scenario("posix/home-absolute", posix=True, env={"OPENBURROW_HOME": "C:/abs/runtime"}),
        Scenario("posix/home-tilde", posix=True, env={"OPENBURROW_HOME": "~/obhome"}),
        Scenario("posix/home-dot", posix=True, env={"OPENBURROW_HOME": "."}),
        Scenario("posix/home-with-dotdot", posix=True, env={"OPENBURROW_HOME": "a/../b"}),
        Scenario("posix/home-whitespace", posix=True, env={"OPENBURROW_HOME": "   "}),
        Scenario(
            "posix/socket-override", posix=True, env={"OPENBURROW_DAEMON_SOCKET": "custom.sock"}
        ),
        Scenario(
            "posix/socket-override-tilde",
            posix=True,
            env={"OPENBURROW_DAEMON_SOCKET": "~/custom.sock"},
        ),
        Scenario(
            "posix/socket-override-path",
            posix=True,
            env={"OPENBURROW_DAEMON_SOCKET": "C:/sockets/burrow.sock"},
        ),
        Scenario("posix/global-tilde", posix=True, env={"OPENBURROW_GLOBAL_HOME": "~/globalhome"}),
    ]

    # --- the pipe digest's case mapping, through the real derivation --------
    for probe in case_probes():
        scenarios.append(Scenario(f"case/{probe}", repo=f"{repo}/caseprobe-{probe}"))

    return scenarios


# ---------------------------------------------------------------------------
# The Python half
# ---------------------------------------------------------------------------


def python_observations(scenarios: list[Scenario]) -> dict[str, dict[str, str]]:
    """Compute each scenario the way the CLI and daemon do.

    Imports happen here rather than at module scope so that a missing package
    produces a sentence instead of an ImportError traceback at load time.
    """
    try:
        from openburrow.core import paths as paths_module
    except ImportError as exc:  # pragma: no cover - environment dependent
        cannot_run(
            f"error: could not import openburrow.core.paths ({exc}).\n"
            "       Run `uv sync` first — this check compares against the installed package."
        )

    real_is_windows = paths_module.is_windows
    saved_env = {key: os.environ.get(key) for key in MANAGED_ENV}

    observations: dict[str, dict[str, str]] = {}
    try:
        for scenario in scenarios:
            for key in MANAGED_ENV:
                os.environ.pop(key, None)

            repo = scenario.repo or (scenario.env.get("OPENBURROW_REPO_ROOT") or "")
            os.environ["OPENBURROW_REPO_ROOT"] = repo

            for key, value in scenario.env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

            # `global_home` (and its siblings) are `lru_cache`d, so a scenario
            # varying OPENBURROW_GLOBAL_HOME would otherwise silently reuse the
            # first answer for every later scenario.
            paths_module.global_home.cache_clear()
            paths_module.cache_home.cache_clear()
            paths_module.config_home.cache_clear()
            paths_module.state_home.cache_clear()

            paths_module.is_windows = (lambda: False) if scenario.posix else real_is_windows

            paths = paths_module.BurrowPaths.for_repo(repo)
            observations[scenario.name] = {
                "repo_root": str(paths.repo_root),
                "endpoint": paths.ipc_endpoint,
                "reels": str(paths.reels_dir),
                "global": str(paths.global_dir),
            }
    finally:
        paths_module.is_windows = real_is_windows
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    return observations


def assert_the_posix_patch_works(repo: str) -> None:
    """Positive control for the forced branch, on the Python side.

    Without this, a typo that left `is_windows` unpatched would compare the
    Windows branch against the Windows branch and report perfect agreement.
    """
    from openburrow.core import paths as paths_module

    real_is_windows = paths_module.is_windows
    saved = os.environ.get("OPENBURROW_REPO_ROOT")
    os.environ["OPENBURROW_REPO_ROOT"] = repo
    try:
        paths_module.is_windows = real_is_windows
        natural = paths_module.BurrowPaths.for_repo(repo).ipc_endpoint
        paths_module.is_windows = lambda: False  # type: ignore[assignment]
        forced = paths_module.BurrowPaths.for_repo(repo).ipc_endpoint
    finally:
        paths_module.is_windows = real_is_windows
        if saved is None:
            os.environ.pop("OPENBURROW_REPO_ROOT", None)
        else:
            os.environ["OPENBURROW_REPO_ROOT"] = saved

    if not natural.startswith("\\\\.\\pipe\\"):
        cannot_run(f"error: the natural branch did not produce a pipe name: {natural!r}")
    if forced.endswith("burrow.sock") is False:
        cannot_run(f"error: patching is_windows did not switch transports: {forced!r}")


# ---------------------------------------------------------------------------
# The TypeScript half
# ---------------------------------------------------------------------------


def find_node() -> str:
    override = os.environ.get("OPENBURROW_NODE", "").strip()
    if override:
        return override
    found = shutil.which("node")
    if found:
        return found
    cannot_run(
        "error: no `node` on PATH, so the TypeScript half cannot be executed.\n"
        "       This check is one of the few here that needs Node, because the\n"
        "       endpoint is a hash and cannot be read out of the source. Set\n"
        "       OPENBURROW_NODE to a node binary, or run `make parity` alone\n"
        "       (which is Node-free) if you only need the enum mirrors."
    )


def typescript_observations(scenarios: list[Scenario], repo: str) -> dict[str, dict[str, str]]:
    node = find_node()

    request = {
        "repo": repo,
        "scenarios": [
            {"name": s.name, "posix": s.posix, "repo": s.repo, "env": s.env} for s in scenarios
        ],
    }

    # A clean environment for the child: the probe sets what each scenario needs,
    # and inheriting a stray OPENBURROW_* would make the two sides disagree about
    # their inputs rather than about their logic.
    child_env = {k: v for k, v in os.environ.items() if k not in MANAGED_ENV}

    completed = subprocess.run(  # noqa: S603 — fixed argv, shell=False
        [node, str(PROBE)],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(REPO_ROOT),
        env=child_env,
        check=False,
    )

    if completed.returncode != 0:
        cannot_run(
            f"error: the Node probe failed (exit {completed.returncode}).\n"
            f"       stderr:\n{completed.stderr.strip()[-2000:]}"
        )

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        cannot_run(
            f"error: the Node probe did not print JSON ({exc}).\n"
            f"       stdout began: {completed.stdout[:400]!r}\n"
            f"       stderr: {completed.stderr.strip()[-800:]}"
        )

    return {row["name"]: row for row in payload["observations"]}


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


class Drift:
    def __init__(self, name: str, field_name: str, python: str, typescript: str) -> None:
        self.name = name
        self.field = field_name
        self.python = python
        self.typescript = typescript

    def __str__(self) -> str:
        return f"{self.name} [{self.field}]\n      py: {self.python}\n      ts: {self.typescript}"


def compare(
    scenarios: list[Scenario],
    python: dict[str, dict[str, str]],
    typescript: dict[str, dict[str, str]],
    *,
    verbose: bool,
) -> tuple[list[Drift], list[str]]:
    drifts: list[Drift] = []
    unexpected_agreements: list[str] = []
    examined = 0

    for scenario in scenarios:
        py_row = python.get(scenario.name)
        ts_row = typescript.get(scenario.name)

        if py_row is None or ts_row is None:
            missing = "python" if py_row is None else "typescript"
            drifts.append(Drift(scenario.name, "*", "present", f"MISSING from the {missing} side"))
            continue

        for field_name in FIELDS:
            examined += 1
            py_value = str(py_row[field_name])
            ts_value = str(ts_row[field_name])

            # `{"threw": ...}` on one side and a string on the other is drift,
            # not agreement: one implementation raises where the other answers.
            if py_value == ts_value:
                if (scenario.name, field_name) in KNOWN_DIVERGENCES:
                    unexpected_agreements.append(
                        f"{scenario.name} [{field_name}] now AGREES "
                        f"({py_value!r}). It is listed in KNOWN_DIVERGENCES — "
                        "delete that entry, deliberately."
                    )
                elif verbose:
                    print(f"  ok  {scenario.name} [{field_name}] = {py_value}")
                continue

            if (scenario.name, field_name) in KNOWN_DIVERGENCES:
                if verbose:
                    print(f"  ~   {scenario.name} [{field_name}] known divergence")
                continue

            drifts.append(Drift(scenario.name, field_name, py_value, ts_value))

    # A gate that examined nothing must not pass. `--verbose` aside, a scenario
    # list that collapsed to a handful of comparisons means the builder is broken.
    if examined < 200:
        cannot_run(f"error: only {examined} comparisons ran; the scenario list looks truncated.")

    return drifts, unexpected_agreements


def unicode_wide_report(repo: str) -> None:
    """Measure, for the record, how far the two case operations diverge.

    Not an assertion: the counts move as CPython's and V8's Unicode tables are
    updated, and a bound would fail for the wrong reason. It is printed so the
    numbers quoted in `BurrowPaths.pipe_name` and in the bridge's docstring stay
    reproducible rather than becoming folklore.
    """
    print("\nunicode-wide case-mapping scan (informational, not asserted)")

    python_lower = {
        cp: chr(cp).lower()
        for cp in range(0x110000)
        if not (0xD800 <= cp <= 0xDFFF) and chr(cp).lower() != chr(cp)
    }
    python_casefold = {
        cp: chr(cp).casefold()
        for cp in range(0x110000)
        if not (0xD800 <= cp <= 0xDFFF) and chr(cp).casefold() != chr(cp)
    }

    script = (
        "const out = {};"
        "for (let cp = 0; cp < 0x110000; cp++) {"
        "  if (cp >= 0xd800 && cp <= 0xdfff) continue;"
        "  const ch = String.fromCodePoint(cp); const lo = ch.toLowerCase();"
        "  if (lo !== ch) out[cp] = lo;"
        "}"
        "process.stdout.write(JSON.stringify(out));"
    )
    completed = subprocess.run(  # noqa: S603 — fixed argv, shell=False
        [find_node(), "-e", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if completed.returncode != 0:
        print(f"  (skipped — node failed: {completed.stderr.strip()[:200]})")
        return

    js_lower = {int(k): v for k, v in json.loads(completed.stdout).items()}
    keys = set(python_lower) | set(python_casefold) | set(js_lower)

    lower_diff = [cp for cp in keys if python_lower.get(cp) != js_lower.get(cp)]
    casefold_diff = [cp for cp in keys if python_casefold.get(cp) != js_lower.get(cp)]

    print(f"  codepoints where Python .lower()    != JS toLowerCase(): {len(lower_diff)}")
    print(f"  codepoints where Python .casefold() != JS toLowerCase(): {len(casefold_diff)}")
    print("  (the .lower() residue is Unicode-version skew: V8 maps codepoints")
    print("   CPython's bundled tables do not yet know. ASCII is identical either way.)")
    _ = repo


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--verbose", action="store_true", help="print every comparison")
    parser.add_argument(
        "--unicode-wide",
        action="store_true",
        help="also measure the full-Unicode case-mapping divergence (slow, informational)",
    )
    args = parser.parse_args(argv)

    if not BRIDGE.is_file():
        cannot_run(f"error: {BRIDGE} not found; run this from the repo root")
    if not PROBE.is_file():
        cannot_run(f"error: {PROBE} not found")

    repo = str(REPO_ROOT)
    scenarios = build_scenarios(repo)

    assert_the_posix_patch_works(repo)

    python = python_observations(scenarios)
    typescript = typescript_observations(scenarios, repo)

    drifts, unexpected_agreements = compare(scenarios, python, typescript, verbose=args.verbose)

    if args.unicode_wide:
        unicode_wide_report(repo)

    if drifts or unexpected_agreements:
        print(
            f"endpoint parity: {len(drifts)} drift(s) and "
            f"{len(unexpected_agreements)} stale known-divergence(s) "
            f"across {len(scenarios)} scenarios\n",
            file=sys.stderr,
        )
        for drift in drifts:
            print(f"  ✗ {drift}", file=sys.stderr)
        for note in unexpected_agreements:
            print(f"  ✗ {note}", file=sys.stderr)
        print(
            "\nFix apps/web/src/lib/daemon-bridge.ts to match "
            "packages/openburrow-core/src/openburrow/core/paths.py.\n"
            "If the Python changed intentionally, mirror it; the Python is the origin.\n"
            "If a divergence is understood and accepted, add it to KNOWN_DIVERGENCES\n"
            "with the reason — do not simply relax the comparison.",
            file=sys.stderr,
        )
        return 1

    known = len(KNOWN_DIVERGENCES)
    suffix = f" ({known} known divergence(s) still present)" if known else ""
    print(f"endpoint parity: {len(scenarios)} scenarios x {len(FIELDS)} fields agree{suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
