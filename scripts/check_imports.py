"""Import every module in the workspace and report what breaks.

Why this exists
---------------
``ruff`` and ``mypy`` are static: neither executes the code they judge. That
leaves a whole class of defect that both can miss and that a reader will not
see either — a name used before it is defined, a module-level call into
something that is not there yet, a circular import that only bites at runtime.

This was not hypothetical. A patch that replaced a lambda with a named
``_default_risk_tiers()`` put the function *below* the class that used it as a
field default. Field defaults are evaluated eagerly when the class body runs,
so the module raised ``NameError`` on import and the entire ``core`` package
was unloadable — while ``ruff`` had been run before the patch landed and
``mypy`` reported nothing about it. The cheapest possible check — "does it
still import?" — was the one nobody ran.

What it does
------------
Walks every ``packages/*/src`` root, derives the dotted module name for each
file, and imports it. Then it asserts a floor: a walk that finds nothing, or an
environment where everything is skipped, must not be allowed to report success.

Optional dependencies are not failures. A module that cannot import because
``textual`` or ``litellm`` is not installed in this environment is reported as
*skipped*, not broken — the point is to find defects in our own code, not to
demand every extra be present. A missing ``openburrow.*`` module is different,
and is always a failure.

Usage::

    uv run python scripts/check_imports.py            # summary
    uv run python scripts/check_imports.py --list     # every module
    uv run python scripts/check_imports.py --verbose  # skipped reasons too

Exit code 0 means every module that could be imported, imported.
"""

from __future__ import annotations

import importlib
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: ``packages/*/src`` — each of these is a PEP 420 namespace-package root
#: contributing subpackages to the single shared ``openburrow`` namespace.
SRC_ROOTS: list[Path] = sorted(REPO_ROOT.glob("packages/*/src"))

#: A walk that finds fewer modules than this has broken, and a check that
#: examines nothing passes too. Kept below the real count so adding modules is
#: never a chore, but high enough that a bad glob is caught.
MIN_EXPECTED_MODULES = 90

OK = "ok"
SKIPPED = "skipped"
FAILED = "failed"


@dataclass
class Outcome:
    """What happened when one module was imported."""

    name: str
    path: Path
    status: str
    detail: str = ""

    @property
    def package(self) -> str:
        """The distribution this module came from, for grouping output."""
        parts = self.path.relative_to(REPO_ROOT).parts
        return parts[1] if len(parts) > 1 else "<root>"


@dataclass
class Report:
    outcomes: list[Outcome] = field(default_factory=list)

    def of(self, status: str) -> list[Outcome]:
        return [o for o in self.outcomes if o.status == status]

    @property
    def failed(self) -> list[Outcome]:
        return self.of(FAILED)

    @property
    def skipped(self) -> list[Outcome]:
        return self.of(SKIPPED)

    @property
    def imported(self) -> list[Outcome]:
        return self.of(OK)


def module_name(path: Path, src_root: Path) -> str:
    """Derive the importable dotted name for a file under a source root.

    ``__init__.py`` names the package itself, not a submodule, so it loses its
    last segment — otherwise ``.../core/__init__.py`` would be imported as
    ``openburrow.core.__init__``, which is a module object nobody has.
    """
    relative = path.relative_to(src_root).with_suffix("")
    parts = list(relative.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def discover() -> list[tuple[str, Path]]:
    """Every importable module in the workspace, as (name, path)."""
    found: list[tuple[str, Path]] = []
    for root in SRC_ROOTS:
        for path in sorted(root.rglob("*.py")):
            found.append((module_name(path, root), path))
    return found


def missing_module_of(exc: BaseException) -> str | None:
    """The module name an ``ModuleNotFoundError`` could not find, if that is what it is."""
    if isinstance(exc, ModuleNotFoundError):
        return exc.name or "<unnamed>"
    return None


def classify(module: str, path: Path, exc: BaseException) -> Outcome:
    """Decide whether an import failure is our defect or a missing extra.

    A missing *third-party* module means this environment lacks an optional
    extra; a missing ``openburrow.*`` module means we shipped an import that
    points at nothing. Only the second is a defect, and conflating the two
    would make the check either uselessly noisy or quietly permissive.
    """
    missing = missing_module_of(exc)
    if missing is not None and not missing.startswith("openburrow"):
        return Outcome(module, path, SKIPPED, f"optional dependency not installed: {missing}")
    return Outcome(module, path, FAILED, f"{type(exc).__name__}: {exc}")


def import_all(modules: list[tuple[str, Path]]) -> Report:
    """Import each module, recording the outcome rather than stopping at the first."""
    report = Report()
    for name, path in modules:
        try:
            importlib.import_module(name)
        except KeyboardInterrupt:
            raise
        # Broad on purpose: a module that calls `sys.exit()` at import time, or
        # raises a bare `BaseException`, is exactly the kind of thing this check
        # exists to surface. Narrowing this to `Exception` would let `SystemExit`
        # escape and kill the run, hiding every module after it.
        except BaseException as exc:
            outcome = classify(name, path, exc)
            if outcome.status == FAILED:
                outcome.detail += "\n" + "".join(
                    traceback.format_exception(type(exc), exc, exc.__traceback__)
                )
            report.outcomes.append(outcome)
        else:
            report.outcomes.append(Outcome(name, path, OK))
    return report


def render(report: Report, *, show_all: bool, verbose: bool) -> None:
    """Print the report, grouped by distribution."""
    by_package: dict[str, list[Outcome]] = {}
    for outcome in report.outcomes:
        by_package.setdefault(outcome.package, []).append(outcome)

    for package in sorted(by_package):
        outcomes = by_package[package]
        bad = [o for o in outcomes if o.status == FAILED]
        skipped = [o for o in outcomes if o.status == SKIPPED]
        marker = "FAIL" if bad else " ok "
        summary = f"{len(outcomes) - len(bad) - len(skipped)}/{len(outcomes)} imported"
        if skipped:
            summary += f", {len(skipped)} skipped"
        print(f"[{marker}] {package:<28} {summary}")

        if show_all or bad:
            for outcome in outcomes:
                if outcome.status == OK and not show_all:
                    continue
                if outcome.status == SKIPPED and not (show_all and verbose):
                    continue
                print(f"         {outcome.status:<8} {outcome.name}")
        if verbose:
            for outcome in skipped:
                print(f"         skip     {outcome.name}  ({outcome.detail})")


def main(argv: list[str]) -> int:
    show_all = "--list" in argv
    verbose = "--verbose" in argv or show_all

    print("OpenBurrow import check")
    print("=" * 60)
    print(f"source roots: {len(SRC_ROOTS)}")

    modules = discover()

    # Assert the floor before asserting anything else. A glob that stops
    # matching, or a refactor that moves the roots, would otherwise make this
    # check pass by finding nothing — which is precisely the failure mode this
    # script was written to catch elsewhere.
    if len(modules) < MIN_EXPECTED_MODULES:
        print(
            f"\nFAIL: discovery found only {len(modules)} modules, expected at least "
            f"{MIN_EXPECTED_MODULES}. The walk is broken, not the code."
        )
        return 2

    report = import_all(modules)

    if not report.imported:
        print("\nFAIL: no module imported successfully — every result was skipped or failed.")
        print("      A check where nothing was examined is not a passing check.")
        return 2

    print()
    render(report, show_all=show_all, verbose=verbose)

    print()
    print("=" * 60)
    print(
        f"{len(report.imported)} imported, "
        f"{len(report.skipped)} skipped, "
        f"{len(report.failed)} failed"
    )

    if report.failed:
        print("\nFailures:\n")
        for outcome in report.failed:
            print(f"--- {outcome.name}  ({outcome.path.relative_to(REPO_ROOT)})")
            print(outcome.detail)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
