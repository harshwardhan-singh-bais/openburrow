"""``FLAG_KINDS`` must equal the set of kinds the governance layer can emit.

This is a source-scanning test, which is unusual and worth justifying.

``GovernanceFlag.kind`` is deliberately an open string, so nothing validates it
at construction time. The alternative to scanning is to make it an enum, but
that would mean a new detector needs a schema migration to raise a flag — a cost
paid on every future detector to catch a mistake made once.

So the list lives in ``FLAG_KINDS`` as documentation, and this file keeps it
honest by reading the sources and comparing. The check has to be bidirectional
because the two directions are different bugs:

* an emitted kind missing from ``FLAG_KINDS`` — a consumer (UI, alerting rule,
  runbook) silently ignores a real flag. This is the one that matters.
* a listed kind nothing emits — a consumer handles a case that cannot happen,
  and a dead branch survives for years because the list said it was live.

Neither direction is caught by any other test in the suite. A detector's own
tests assert the kinds it emits; nothing asserts the *union* matches what is
documented. That union is exactly what a downstream consumer reads.

The scan is a regex over source text rather than an import-and-introspect pass,
because the kinds are produced inside functions whose arguments are not known
statically — running them would require constructing a Lane, a Delegation and a
message for every detector, which is what the detector tests already do. The
regex asks a narrower question ("what string literals are passed to ``kind=``")
and answers it exactly.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from openburrow.core.models import FLAG_KINDS

#: ``unit`` for the fast suite, ``governance`` for CI's mandatory gate — see the
#: note in ``test_authority_scope.py``.
pytestmark = [pytest.mark.unit, pytest.mark.governance]

#: The packages that raise flags. ``openburrow-core`` is excluded on purpose: it
#: defines the model and the classmethod constructors, and its ``kind=`` values
#: are the named constructors (``authority_creep`` and friends) which are already
#: covered by scanning the callers. Including it would double-count them and make
#: a genuine "nothing emits this" finding invisible.
SOURCE_ROOTS = (Path(__file__).resolve().parents[2] / "openburrow-governance" / "src",)

#: Matches ``kind="some_kind"`` and ``kind='some_kind'``, including when the
#: literal is the whole argument. Deliberately does *not* match f-strings or
#: concatenations — a dynamically built kind cannot be verified by this check,
#: and pretending otherwise would be worse than reporting nothing. If one is ever
#: introduced, the ``test_no_dynamically_built_kinds`` guard below fails so the
#: author knows this check no longer covers them.
_KIND_LITERAL = re.compile(r"""\bkind\s*=\s*["']([a-z_]+)["']""")

#: Any ``kind=`` whose argument is not a plain string literal.
_KIND_DYNAMIC = re.compile(r"""\bkind\s*=\s*(?!["'])([^\s,)]+)""")

#: Attribute access ending in ``.kind`` — a value already computed elsewhere, not
#: a new one being built. See ``test_no_dynamically_built_kinds``.
_PASSTHROUGH = re.compile(r"^[\w.]+\.kind$")


def _sources() -> list[Path]:
    files: list[Path] = []
    for root in SOURCE_ROOTS:
        files.extend(sorted(root.rglob("*.py")))
    assert files, f"no sources found under {SOURCE_ROOTS}; the scan would vacuously pass"
    return files


def _emitted_kinds() -> dict[str, set[str]]:
    """Map each emitted kind to the files that emit it."""
    found: dict[str, set[str]] = {}
    for path in _sources():
        text = path.read_text(encoding="utf-8")
        for kind in _KIND_LITERAL.findall(text):
            found.setdefault(kind, set()).add(path.name)
    return found


class TestFlagKindsMatchesSource:
    def test_the_scan_finds_something(self) -> None:
        # A source scan whose regex stops matching is a check that passes by
        # finding nothing. Assert the floor before asserting the equality.
        emitted = _emitted_kinds()
        assert len(emitted) >= 10, (
            f"scan found only {len(emitted)} kinds; the regex is probably broken"
        )

    def test_every_emitted_kind_is_declared(self) -> None:
        emitted = set(_emitted_kinds())
        undeclared = sorted(emitted - FLAG_KINDS)
        assert not undeclared, (
            f"these kinds are emitted but missing from FLAG_KINDS: {undeclared}. "
            "A consumer reading FLAG_KINDS will not know they exist."
        )

    def test_every_declared_kind_is_emitted(self) -> None:
        emitted = set(_emitted_kinds())
        unemitted = sorted(FLAG_KINDS - emitted)
        assert not unemitted, (
            f"these kinds are in FLAG_KINDS but nothing emits them: {unemitted}. "
            "Either a detector was removed, or the kind is spelled differently."
        )

    def test_the_two_sets_are_equal(self) -> None:
        # Stated as one assertion as well as two, because the failure message
        # people actually read is this one.
        assert set(_emitted_kinds()) == set(FLAG_KINDS)

    def test_capability_undeclared_is_declared(self) -> None:
        # Pinned by name. This kind was added to close a blind spot where a lane
        # that declared no skills and then used some was reported clean. If a
        # future refactor removes it, the blind spot returns, and the two set
        # comparisons above would not notice — they only check self-consistency.
        assert "capability_undeclared" in FLAG_KINDS

    def test_no_dynamically_built_kinds(self) -> None:
        # If a kind is ever built at runtime, the scan silently stops covering
        # it and the set comparisons become vacuously true for that kind. Fail
        # loudly so the author of that change knows the guarantee is gone.
        #
        # Plain pass-throughs are allowed and must be: ``Detection.to_flag()``
        # forwards ``self.kind`` and the ledger forwards ``flag.kind``. Neither
        # builds anything — both propagate a kind that originated as a literal
        # somewhere else in this same scanned source, which is where the check
        # already saw it. Flagging them would make the guard fail on correct
        # code, and a guard that fails on correct code gets deleted.
        #
        # Allowing the ``*.kind`` shape does not open a hole: the f-string or
        # concatenation that built the value in the first place still appears as
        # a ``kind=`` argument at its construction site, and still trips this.
        offenders: list[str] = []
        for path in _sources():
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue
                for match in _KIND_DYNAMIC.finditer(line):
                    expr = match.group(1).rstrip(":")
                    if expr in {"str", "kind"}:
                        continue
                    if _PASSTHROUGH.match(expr):
                        continue
                    offenders.append(f"{path.name}:{lineno} kind={expr}")
        assert not offenders, (
            "a flag kind is built dynamically, so the source scan cannot verify it:\n  "
            + "\n  ".join(offenders)
        )
