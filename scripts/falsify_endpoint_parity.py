"""Falsification pass for ``scripts/check_endpoint_parity.py``.

A gate that cannot be shown to fail is a gate that examines nothing, and this
one has an unusual amount of machinery to get wrong: it runs two languages, it
forces a platform branch through a load hook, and it carries a list of accepted
divergences. Each of those is a place where the check could pass for the wrong
reason.

So the rows below do not only break the *implementations*. Half of them break the
**check itself** — its accepted-divergence list, its scenario floor, its
branch-forcing hook — because those are the mechanisms that would let a real
divergence through while reporting success.

Every row mutates a file on disk, runs the check as a subprocess, and restores
the file. A subprocess is required rather than an import: ``paths.py`` is
imported once per process, so an in-process mutation would be invisible.

Verdicts, as in the stage harnesses:

* ``ok`` — the check failed in the way the row predicted.
* ``VACUOUS`` — the check passed anyway, so the row does not discriminate.
* ``WEAK`` — the mutation could not be applied, or the check failed for a
  *different* reason than the row predicted. A revert that cannot land has
  tested nothing, and a failure with the wrong exit code is a different bug.

Not part of ``make check``: a falsification pass reconstructs behaviour a fix
removed, so it is only meaningful while that fix is the most recent change.

Run with ``.venv/Scripts/python.exe scripts/falsify_endpoint_parity.py``.
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

from falsify_matcher import replace_anchor_exact, self_check

REPO = Path(__file__).resolve().parent.parent
CHECK = REPO / "scripts" / "check_endpoint_parity.py"
BRIDGE = REPO / "apps" / "web" / "src" / "lib" / "daemon-bridge.ts"
PATHS_PY = REPO / "packages" / "openburrow-core" / "src" / "openburrow" / "core" / "paths.py"
HOOK = REPO / "scripts" / "_web_posix_hook.mjs"


def run_check() -> tuple[int, str]:
    """Run the check in a fresh process. Returns (exit code, combined output)."""
    completed = subprocess.run(  # noqa: S603 — fixed argv, shell=False
        [sys.executable, str(CHECK)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(REPO),
        check=False,
    )
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


@contextlib.contextmanager
def mutated(path: Path, old: str, new: str) -> Iterator[None]:
    """Swap one exact region, restore afterwards, and refuse to be a no-op.

    A mutation whose anchor does not match is not a weaker test — it is no test
    at all, and it would report ``ok`` on the strength of a check that was never
    exercised. Raising here turns that into a WEAK verdict, and the refusal itself
    lives in :func:`falsify_matcher.replace_anchor_exact` so that every harness
    gets it rather than each one re-implementing the guard.

    This harness uses the *exact* mode, and the reason is worth stating. Its
    anchors are fragments in two languages: the tokenizer cannot read
    ``daemon-bridge.ts`` or the ``.mjs`` hook at all, and the Python anchors quote
    mid-line regions — ``("posix/home-with-dotdot", "endpoint")`` sits inside a dict
    literal at an indentation the replacement does not repeat — which a token match
    would relocate to the line start and dedent. One mode for the whole function
    beats a per-file split that would make a row's behaviour depend on which side of
    a suffix test it happened to fall.

    Byte-exact on the way out, which took a bug to get right. ``Path.write_text``
    opens in text mode, so on Windows it translates every ``\\n`` to ``\\r\\n``:
    restoring a file that way re-formats it to CRLF, and since
    ``formatter.line_ending`` resolves to ``lf`` the very next ``make check``
    fails on files this harness had touched. ``make falsify`` and ``make check``
    were quietly undoing each other. Reading and writing bytes, and remembering
    the file's own newline style for the mutated copy, keeps both honest.
    """
    raw = path.read_bytes()
    newline = "\r\n" if b"\r\n" in raw else "\n"
    # Normalised for *matching* only: anchors are written with plain ``\n``.
    original = raw.decode("utf-8").replace("\r\n", "\n")

    patched = replace_anchor_exact(original, old, new, path.name)

    try:
        path.write_bytes(patched.replace("\n", newline).encode("utf-8"))
        yield
    finally:
        path.write_bytes(raw)


def expect(exit_code: int, output: str, *, want: int, marker: str = "") -> bool:
    """True when the check *still passed* — i.e. the row failed to discriminate."""
    if exit_code == 0:
        return True
    if exit_code != want:
        raise AssertionError(
            f"expected exit {want}, got {exit_code}; the failure was for a different "
            f"reason than this row predicts. Output tail:\n{output.strip()[-600:]}"
        )
    if marker and marker not in output:
        raise AssertionError(
            f"exit {exit_code} as expected but {marker!r} is absent from the output, so "
            f"it failed somewhere else. Output tail:\n{output.strip()[-600:]}"
        )
    return False


# ---------------------------------------------------------------------------
# Rows that break the implementations
# ---------------------------------------------------------------------------


def check_1() -> bool:
    """The original bug: the pipe name loses its repo digest."""
    with mutated(
        BRIDGE,
        r"return `\\\\.\\pipe\\openburrow-${user}-${repoPipeDigest()}`;",
        r"return `\\\\.\\pipe\\openburrow-${user}`;",
    ):
        code, out = run_check()
    return expect(code, out, want=1, marker="drift")


def check_2() -> bool:
    """`OPENBURROW_HOME=~/x` stops expanding the tilde."""
    with mutated(
        BRIDGE,
        "if (override) return path.resolve(resolveRepoRoot(), expandUser(override));",
        "if (override) return path.resolve(resolveRepoRoot(), override);",
    ):
        code, out = run_check()
    return expect(code, out, want=1, marker="drift")


def check_3() -> bool:
    """Python goes back to `casefold`, which is where the ß divergence came from."""
    with mutated(
        PATHS_PY,
        'hashlib.sha256(str(self.repo_root).lower().encode("utf-8")).hexdigest()[:12]',
        'hashlib.sha256(str(self.repo_root).casefold().encode("utf-8")).hexdigest()[:12]',
    ):
        code, out = run_check()
    return expect(code, out, want=1, marker="case/")


def check_4() -> bool:
    """The POSIX socket override stops being normalised."""
    with mutated(
        BRIDGE,
        "return isWindows ? override : path.normalize(expandUser(override));",
        "return isWindows ? override : expandUser(override);",
    ):
        code, out = run_check()
    return expect(code, out, want=1, marker="drift")


def check_5() -> bool:
    """The duplicate runtime-dir derivation comes back, and drifts.

    Reverting `resolveReelsDir` to its own copy of the derivation is the exact
    shape that produced the pipe-name bug: two copies, nothing comparing them.
    Here the copy is made *wrong* so the drift is observable.
    """
    with mutated(
        BRIDGE,
        'return path.join(resolveRuntimeDir(), "reels");',
        'return path.join(path.join(resolveRepoRoot(), ".openburrow"), "reels");',
    ):
        code, out = run_check()
    return expect(code, out, want=1, marker="reels")


# ---------------------------------------------------------------------------
# Rows that break the check itself
# ---------------------------------------------------------------------------


def check_6() -> bool:
    """An accepted divergence is un-accepted, and must resurface as drift."""
    with mutated(
        CHECK,
        '("posix/home-with-dotdot", "endpoint")',
        '("posix/home-with-dotdot-DISABLED", "endpoint")',
    ):
        code, out = run_check()
    return expect(code, out, want=1, marker="posix/home-with-dotdot")


def check_7() -> bool:
    """A pair that *agrees* is added to the accepted list.

    This is the direction that matters most: if the accepted list could absorb
    an agreement, the check would silently stop checking whatever someone listed.
    """
    with mutated(
        CHECK,
        "        KNOWN_DIVERGENCES[(_scenario, _field_name)] = _CASE_DIVERGENCE_REASON",
        "        KNOWN_DIVERGENCES[(_scenario, _field_name)] = _CASE_DIVERGENCE_REASON\n"
        'KNOWN_DIVERGENCES[("win/default", "endpoint")] = "falsification row"',
    ):
        code, out = run_check()
    return expect(code, out, want=1, marker="AGREES")


def check_8() -> bool:
    """The scenario list collapses, so almost nothing is compared.

    Without the floor assertion this would pass — a check that examines nothing
    passing is the failure mode the whole project keeps auditing for.
    """
    with mutated(CHECK, "for probe in case_probes():", "for probe in case_probes()[:2]:"):
        code, out = run_check()
    return expect(code, out, want=2, marker="comparisons ran")


def check_9() -> bool:
    """The POSIX hook's anchor drifts, so the forced branch is never forced.

    Without the hook's own assertion this would compare the Windows branch
    against itself and report perfect agreement between two identical things.
    """
    with mutated(
        HOOK,
        """const ANCHOR = 'const isWindows = platform() === "win32";';""",
        "const ANCHOR = 'const isWindows = NOT_THE_REAL_LINE;';",
    ):
        code, out = run_check()
    return expect(code, out, want=2, marker="Node probe failed")


ROWS = [
    ("ts/pipe-digest-removed", "the Windows pipe name loses `sha256(repo)[:12]`"),
    ("ts/home-tilde-not-expanded", "`OPENBURROW_HOME=~/x` resolves inside the repo"),
    ("python/casefold-restored", "`paths.pipe_name` goes back to `casefold`"),
    ("ts/socket-override-not-normalised", "a POSIX socket override is returned verbatim"),
    ("ts/reels-derivation-duplicated", "`resolveReelsDir` grows its own runtime-dir copy"),
    ("check/accepted-divergence-removed", "a known divergence is un-pinned and resurfaces"),
    ("check/accepted-divergence-added", "a pair that agrees is added to the accepted list"),
    ("check/scenario-floor", "the scenario list is truncated to almost nothing"),
    ("probe/hook-anchor-drifted", "the POSIX load hook can no longer find its anchor"),
]

CHECKS: list[Callable[[], bool]] = [
    check_1,
    check_2,
    check_3,
    check_4,
    check_5,
    check_6,
    check_7,
    check_8,
    check_9,
]


def main() -> int:
    assert len(CHECKS) == len(ROWS), "row metadata is out of step with the checks"
    self_check()

    # Control first: if the unmutated check does not pass, every verdict below is
    # meaningless, and a run that reported "all rows discriminate" would be
    # describing a broken harness.
    control_code, control_out = run_check()
    if control_code != 0:
        print("control run FAILED — the unmutated check does not pass, so nothing")
        print("below would mean anything. Output:")
        print(control_out.strip()[-2000:])
        return 1
    print("  [control] unmutated check passes\n")

    vacuous: list[str] = []
    weak: list[str] = []
    for (name, note), check in zip(ROWS, CHECKS, strict=True):
        raised = ""
        try:
            still_passes = check()
        except Exception as exc:
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
        print(f"            broke: {note}")
        if raised:
            print(f"            the mutation did not land, or failed differently: {raised}")

    print()
    if vacuous:
        print(f"{len(vacuous)} VACUOUS row(s) — the check does not discriminate:")
        for name in vacuous:
            print(f"  - {name}")
    if weak:
        print(f"{len(weak)} WEAK row(s) — nothing was proven:")
        for name in weak:
            print(f"  - {name}")
    if vacuous or weak:
        return 1

    print(f"all {len(CHECKS)} rows discriminate: the check fails whenever either side moves")
    return 0


if __name__ == "__main__":
    sys.exit(main())
