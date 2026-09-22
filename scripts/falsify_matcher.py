"""Token-based anchor matching, shared by the falsification harnesses.

Each harness reverts code to the shape it had before a fix and then checks that the
tests still fail against it. They work differently on purpose — one patches methods
at runtime, one rewrites a module and executes it, one drives a real supervisor
against a real database, one mutates a file and re-runs a check as a subprocess —
but every one of them has to locate a region of source by quoting it, and that is
the part this module owns.

Why tokens and not bytes
------------------------

A byte-exact anchor expires the first time anything reflows the file it quotes.
That happened twice to the stale-lane harness — once when ``ruff format`` collapsed
an expression onto one line, once when a mechanical sweep rewrapped a statement —
and both times every row went WEAK. It failed loudly, which is why it was caught,
but a gate that has to be repaired after every unrelated refactor is a gate people
stop running.

Matching on significant tokens survives reflow in both directions, and it is a
*stronger* check than text matching: a renamed identifier, a changed literal or a
reordered call all still fail. Layout tokens — newlines, indentation, comments —
are dropped because they carry no meaning. A trailing comma in front of a closing
bracket goes with them, because exploding an argument list is exactly how the
formatter adds one, and ``f(a, b,)`` and ``f(a, b)`` are one expression written two
ways. String literals are compared by their source text, so whitespace *inside* one
stays significant — an anchor that matched ``"a  b"`` against ``"a b"`` would be
the silent no-op this module exists to prevent.

A missing anchor and an ambiguous one are both hard errors. ``str.replace`` takes
the first of several matches and reports nothing, so an anchor that matches twice
would revert a region the row was not written against. A revert that quietly did
nothing is the one failure these harnesses exist to prevent.

Why two modes
-------------

:func:`replace_anchor` needs the tokenizer, so it only reads Python. The endpoint
harness mutates TypeScript and JavaScript as well, and quotes fragments rather than
whole statements: ``("posix/home-with-dotdot", "endpoint")`` sits inside a dict
literal at an indentation its replacement does not carry, so a token match would
start at the line start and drop that indentation, producing a file that does not
compile. :func:`replace_anchor_exact` is the byte-exact mode for those callers. It
gives up reflow tolerance and keeps the two guarantees that actually matter —
exactly one match, and a real change.

Choose by what the anchor *is*, not by the file's language. A whole statement that a
formatter may rewrap wants :func:`replace_anchor`. A mid-line fragment, a line whose
replacement does not repeat its indentation, or anything the tokenizer cannot read
wants :func:`replace_anchor_exact`.
"""

from __future__ import annotations

import textwrap
import tokenize
from io import StringIO

_LAYOUT_TOKENS = frozenset(
    {
        tokenize.COMMENT,
        tokenize.DEDENT,
        tokenize.ENCODING,
        tokenize.ENDMARKER,
        tokenize.INDENT,
        tokenize.NEWLINE,
        tokenize.NL,
    }
)

_CLOSERS = frozenset({")", "]", "}"})


def _tokens(text: str) -> list[tokenize.TokenInfo]:
    """Tokenise ``text``, or raise. Separate so the retry below is a clean second call."""
    try:
        return list(tokenize.generate_tokens(StringIO(text).readline))
    except (SyntaxError, tokenize.TokenError) as exc:
        raise AssertionError(f"revert anchor does not tokenise: {exc}") from exc


def anchor_tokens(text: str) -> list[tokenize.TokenInfo]:
    """The tokens of ``text`` an anchor compares, with layout normalised away.

    A fragment that will not tokenise is an error rather than an empty match, for
    the same reason a missing anchor is: the failure being guarded against is a
    revert that quietly did nothing.

    There is one retry, and it is load-bearing. An anchor is a *slice* of a file, so
    its first line can start deeper than anything that follows it. Tokenised on its
    own, that first line invents an outer indentation level, and the fragment's own
    dedent then refers to a level the tokenizer never saw — ``unindent does not match
    any outer indentation level``. Removing the common leading prefix puts the
    shallowest line at module level, which is what the fragment actually means.

    Only leading whitespace changes, and leading whitespace is dropped from the
    comparison anyway. Inside a multi-line string literal it is content, but stripping
    it can only make an anchor match *less* — a miss raises, so the retry cannot turn
    a miss into a silent hit.
    """
    try:
        tokens = _tokens(text)
    except AssertionError:
        tokens = _tokens(textwrap.dedent(text))

    significant = [token for token in tokens if token.type not in _LAYOUT_TOKENS]
    return [
        token
        for index, token in enumerate(significant)
        if not (
            token.string == ","
            and index + 1 < len(significant)
            and significant[index + 1].string in _CLOSERS
        )
    ]


def _line_starts(text: str) -> list[int]:
    """Absolute offset of the first character of each line, 1-based by index."""
    starts = [0]
    for line in text.splitlines(keepends=True):
        starts.append(starts[-1] + len(line))
    return starts


def _offset(starts: list[int], position: tuple[int, int]) -> int:
    row, col = position
    return starts[row - 1] + col


def replace_anchor(source: str, old: str, new: str, label: str) -> str:
    """Replace the single region ``old`` describes in ``source``, or raise.

    ``label`` names the file in the error message — a bare name, not a path, so
    that a caller matching against an in-memory string has nothing to invent.

    The replacement is inserted at the start of the matched region's first line
    when only indentation precedes it, so ``new`` carries its own indentation
    exactly as the anchor does.
    """
    wanted = [(token.type, token.string) for token in anchor_tokens(old)]
    if not wanted:
        raise AssertionError(f"revert anchor is empty after tokenising: {old[:70]!r}")

    found = anchor_tokens(source)
    size = len(wanted)
    hits = [
        index
        for index in range(len(found) - size + 1)
        if [(token.type, token.string) for token in found[index : index + size]] == wanted
    ]
    if len(hits) != 1:
        raise AssertionError(
            f"revert anchor matched {len(hits)} times in {label}, want 1: {old[:70]!r}"
        )

    window = found[hits[0] : hits[0] + size]
    starts = _line_starts(source)
    first = _offset(starts, window[0].start)
    last = _offset(starts, window[-1].end)

    # Start at the beginning of the line when only indentation precedes the first
    # token, so that the replacement's own indentation is what lands.
    line_start = starts[window[0].start[0] - 1]
    start = line_start if not source[line_start:first].strip() else first

    # Swallow a trailing comment on the last line. The reverted statement is
    # replaced whole, and a stale suppression left behind would read as describing
    # code that is no longer there.
    remainder = source[last : starts[window[-1].end[0]]]
    if remainder.strip() and not remainder.lstrip().startswith("#"):
        end = last
    else:
        end = last + len(remainder.rstrip())

    return source[:start] + new + source[end:]


def replace_anchor_exact(source: str, old: str, new: str, label: str) -> str:
    """Replace the single occurrence of ``old`` in ``source``, or raise.

    The byte-exact counterpart to :func:`replace_anchor`, for anchors the tokenizer
    cannot read and for fragments that must keep the whitespace around them. Both
    refusals are deliberate: a missing anchor means the revert did not happen, and an
    unchanged result means ``new`` said nothing new — either way the row would report
    on a check that was never exercised, which is the one thing a falsification pass
    must not do.
    """
    occurrences = source.count(old)
    if occurrences != 1:
        raise AssertionError(
            f"revert anchor matched {occurrences} times in {label}, want 1: {old[:70]!r}"
        )
    patched = source.replace(old, new, 1)
    if patched == source:
        raise AssertionError(f"the mutation produced no change in {label}: {old[:70]!r}")
    return patched


def self_check() -> None:
    """Prove the matcher can still fail, before trusting the rows it feeds.

    Every verdict in every harness is a claim about code that was *reverted*, and
    the revert is only as good as the match that found it. A matcher that matched
    anything would make every row read ``ok`` while reverting nothing — a
    falsification pass turned into theatre. So the properties that matter are
    pinned here: reflow in either direction is tolerated, and a changed token, a
    changed string literal or an ambiguous anchor is not.
    """
    probe = "<self-check>"
    flat = "        alpha = one(Alpha.x, Alpha.y)\n        beta = await run(select(Alpha))"
    wrapped = (
        "        alpha = one(\n"
        "            Alpha.x,\n"
        "            Alpha.y,\n"
        "        )\n"
        "        beta = await run(\n"
        "            select(Alpha)\n"
        "        )\n"
    )

    def rejects(source: str, anchor: str) -> bool:
        try:
            replace_anchor(source, anchor, "", probe)
        except AssertionError:
            return True
        return False

    def rejects_exact(source: str, anchor: str, replacement: str) -> bool:
        try:
            replace_anchor_exact(source, anchor, replacement, probe)
        except AssertionError:
            return True
        return False

    # Collapsing and wrapping are the same edit as far as an anchor is concerned —
    # including the trailing comma the formatter adds to the exploded form.
    # Asserting the whole region was consumed proves the comma was inside the match
    # rather than left behind in the reverted code.
    expanded = replace_anchor(wrapped, flat, "REPLACED", probe)
    assert expanded.strip() == "REPLACED", f"the match left text behind: {expanded!r}"
    collapsed = replace_anchor(flat + "\n", wrapped, "REPLACED", probe)
    assert "REPLACED" in collapsed, "a collapsed expression was rejected"
    # A changed token is a change.
    assert rejects(wrapped, flat.replace("Alpha.y", "Alpha.z")), "changed token matched"
    # Whitespace inside a literal is content, not layout.
    assert rejects('f("a  b")', 'f("a b")'), "literal whitespace was not exact"
    assert not rejects('f("a b")', 'f("a b")'), "an exact literal was rejected"
    # Ambiguity is an error, not a coin flip.
    assert rejects("dup = 1\ndup = 1\n", "dup = 1"), "ambiguous anchor matched"

    # The exact mode is what the endpoint harness depends on, so it is held to the
    # same standard rather than trusted: a real mutation goes through, and a missing,
    # ambiguous or no-op one does not.
    assert not rejects_exact("a = 1\n", "a = 1", "a = 2"), "exact: a real mutation was rejected"
    assert rejects_exact("dup = 1\ndup = 1\n", "dup = 1", "x = 0"), "exact: ambiguous matched"
    assert rejects_exact("a = 1\n", "b = 1", "x = 0"), "exact: a missing anchor matched"
    assert rejects_exact("a = 1\n", "a = 1", "a = 1"), "exact: a no-op was accepted"
    print(
        "  matcher self-check: reflow both ways tolerated; token, literal and ambiguity "
        "rejected; exact mode refuses missing, ambiguous and no-op"
    )


__all__ = ["anchor_tokens", "replace_anchor", "replace_anchor_exact", "self_check"]
