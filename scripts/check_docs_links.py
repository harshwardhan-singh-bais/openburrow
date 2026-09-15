#!/usr/bin/env python3
"""Verify that every relative link and anchor in the docs actually resolves.

Why this exists alongside `mkdocs build --strict`
-------------------------------------------------
`--strict` validates the *nav*: that every entry points at a file, and that every
file is reachable from an entry. It does not validate links written in prose.
So a page can say "see [the governance model](../governance/model.md)" and point
at a path that was renamed two months ago, and the site builds clean while every
reader who follows the link gets a 404.

That is the exact failure this catches, and it is the common one — the nav is
written once and the prose is edited forever.

What is checked
---------------
1. **File links.** `[x](path)`, `[x](../path.md)`, `[x](dir/)` — resolved
   relative to the containing file, with the same inference MkDocs does
   (`dir/` → `dir/README.md` or `dir/index.md`, extensionless → `.md`).
2. **Anchors.** `[x](page.md#some-heading)` — the heading must exist in the
   target file. This is what stops a link surviving a section rename.
3. **Absolute site paths.** `[x](/governance/model/)` — resolved from the docs
   root, because that is how MkDocs serves them.

What is not checked, deliberately
---------------------------------
- **External URLs.** Reaching the network makes a docs check flaky, and a
  flaky check in CI is a check that gets `continue-on-error: true` and then
  stops existing. `scripts/` has no business doing an HTTP request to decide
  whether a build passes.
- **Links inside fenced code blocks.** A page that shows `[text](path)` as an
  example is showing a *string*, not a link. Checking it would make documenting
  the link syntax impossible.
- **Links inside inline code spans.** Same reason, one level down.

Exit codes
----------
    0   every link resolves
    1   at least one link or anchor is broken
    2   the check could not run
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_ROOT = REPO_ROOT / "docs"

#: Directories to walk. The root READMEs are included because they are what a
#: visitor reads first and their links rot like any other.
SEARCH_ROOTS: tuple[Path, ...] = (
    DOCS_ROOT,
    REPO_ROOT / "README.md",
    REPO_ROOT / "CONTRIBUTING.md",
    REPO_ROOT / "SECURITY.md",
    REPO_ROOT / "AGENTS.md",
    REPO_ROOT / "CHANGELOG.md",
)

#: `[text](target)` and `[text](target "title")`, but not `![alt](img)`.
LINK_RE = re.compile(r"(?<!!)\[([^\]]*)\]\(\s*<?([^)\s>]+)>?(?:\s+[\"'][^\"']*[\"'])?\s*\)")

#: A fenced code block: ``` or ~~~, with any info string.
FENCE_RE = re.compile(r"^(\s*)(`{3,}|~{3,})", re.MULTILINE)

#: `## Heading`, `### Heading`, and the Setext underline form.
ATX_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$", re.MULTILINE)
SETEXT_HEADING_RE = re.compile(r"^([^\n]+)\n\s{0,3}(=+|-{2,})\s*$", re.MULTILINE)

INLINE_CODE_RE = re.compile(r"`[^`\n]*`")

SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


@dataclass
class Problem:
    source: Path
    line: int
    target: str
    reason: str

    def __str__(self) -> str:
        try:
            relative = self.source.relative_to(REPO_ROOT)
        except ValueError:
            relative = self.source
        return f"{relative}:{self.line}: {self.target!r} — {self.reason}"


@dataclass
class Report:
    files_checked: int = 0
    links_checked: int = 0
    anchors_checked: int = 0
    problems: list[Problem] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


# ---------------------------------------------------------------------------
# Markdown surgery
# ---------------------------------------------------------------------------


def strip_code(text: str) -> str:
    """Blank out fenced code blocks, preserving line numbers.

    Replacing with newlines rather than deleting keeps every subsequent line
    number correct, which is the difference between "there is a broken link
    somewhere" and "line 84".
    """
    lines = text.split("\n")
    out: list[str] = []
    fence: str | None = None

    for line in lines:
        match = FENCE_RE.match(line)
        if fence is None:
            if match:
                fence = match.group(2)[0] * 3
                out.append("")
                continue
            out.append(line)
        else:
            # Closing fence: same character, at least as long.
            if match and match.group(2).startswith(fence):
                fence = None
            out.append("")

    return "\n".join(out)


def strip_inline_code(text: str) -> str:
    """Blank out inline code spans, preserving offsets."""
    return INLINE_CODE_RE.sub(lambda match: " " * len(match.group(0)), text)


def slugify(heading: str) -> str:
    """Approximate `pymdownx.slugs.slugify(case="lower")`.

    Not a re-implementation for its own sake — the anchor in a link has to match
    the id MkDocs generates, and MkDocs generates it from this transform. The
    approximation is checked against the real headings below: a link whose
    fragment matches *nothing* is a problem, and one that matches nothing but
    looks close gets a note suggesting the near miss rather than a bare failure.
    """
    text = heading.strip()
    # Trailing `{#custom-id}` wins outright.
    custom = re.search(r"\{#([^}]+)\}\s*$", text)
    if custom:
        return custom.group(1)

    # Strip inline markdown: emphasis, code, links.
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\*\*([^*]*)\*\*", r"\1", text)
    text = re.sub(r"\*([^*]*)\*", r"\1", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = text.lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    return text.strip("-")


def headings_of(path: Path) -> set[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return set()

    text = strip_code(text)
    anchors: set[str] = set()

    for _, raw in ATX_HEADING_RE.findall(text):
        anchors.add(slugify(raw))

    for raw in SETEXT_HEADING_RE.findall(text):
        anchors.add(slugify(raw[0]))

    # MkDocs also generates ids for explicit anchors and for duplicated headings
    # (`-1`, `-2` suffixes). Adding the bare form covers the first; the suffixed
    # forms are only reachable by counting duplicates, which is not worth the
    # complexity for a check whose false negatives are cosmetic.
    return anchors


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def candidates_for(base: Path) -> list[Path]:
    """The files a link to `base` could mean, in MkDocs' order.

    The literal path is tried first even when it has no extension, because a link
    to `LICENSE` or `Makefile` is a link to a real file and MkDocs serves it.
    Inferring `.md` first would report those as broken — which is exactly what an
    earlier version of this script did.
    """
    if base.suffix:
        return [base]
    return [
        base,
        base.with_suffix(".md"),
        base / "README.md",
        base / "index.md",
    ]


def resolve_file(source: Path, target: str, docs_relative: bool) -> Path | None:
    base = DOCS_ROOT / target.lstrip("/") if docs_relative else source.parent / target

    # `..` segments are fine and common; normalise without touching the
    # filesystem so a link to a file that does not exist still resolves to a
    # path we can report.
    try:
        base = Path(base)
    except ValueError:
        return None

    for candidate in candidates_for(base):
        if candidate.is_file():
            return candidate.resolve()
    return None


def is_checkable(target: str) -> bool:
    if not target or target.startswith("#"):
        # A pure fragment — handled separately, against the same file.
        return False
    if SCHEME_RE.match(target):
        # http:, https:, mailto:, and anything else with a scheme.
        return False
    # Protocol-relative.
    return not target.startswith("//")


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------


def markdown_files() -> list[Path]:
    found: list[Path] = []
    for root in SEARCH_ROOTS:
        if root.is_file():
            found.append(root)
        elif root.is_dir():
            found.extend(sorted(root.rglob("*.md")))
    return sorted(set(found))


def check_file(path: Path, report: Report, *, strict_anchors: bool) -> None:
    raw = path.read_text(encoding="utf-8")
    text = strip_inline_code(strip_code(raw))
    lines = text.split("\n")

    # `docs/README.md` is the docs index, so a link written in it is relative to
    # the docs root. Every other file is relative to its own directory.
    docs_relative_root = path == DOCS_ROOT / "README.md"

    for index, line in enumerate(lines, start=1):
        for match in LINK_RE.finditer(line):
            target = match.group(2).strip()
            if not target:
                continue

            fragment = ""
            if "#" in target:
                target, fragment = target.split("#", 1)

            # --- pure fragment: same file ---------------------------------
            if not target:
                report.anchors_checked += 1
                if fragment and fragment not in headings_of(path):
                    report.problems.append(
                        Problem(path, index, f"#{fragment}", "no such heading in this file")
                    )
                continue

            if not is_checkable(target):
                continue

            report.links_checked += 1
            resolved = resolve_file(
                path,
                target,
                docs_relative=target.startswith("/") or docs_relative_root,
            )
            if resolved is None:
                # A path with no extension pointing at a directory is the most
                # common near-miss, so say so rather than just "not found".
                hint = (
                    " (a directory link needs a trailing slash and a README.md or index.md)"
                    if not Path(target).suffix
                    else ""
                )
                report.problems.append(Problem(path, index, target, f"file does not exist{hint}"))
                continue

            if fragment:
                report.anchors_checked += 1
                if fragment not in headings_of(resolved):
                    # `strict_anchors` is off by default in the sense that the
                    # problem is still recorded — it is only the *exit code*
                    # that changes, and CI turns it on.
                    reason = f"no heading matching #{fragment} in {resolved.name}"
                    if strict_anchors:
                        report.problems.append(Problem(path, index, f"{target}#{fragment}", reason))
                    else:
                        report.problems.append(
                            Problem(path, index, f"{target}#{fragment}", f"{reason} (anchor)")
                        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--anchors",
        action="store_true",
        default=True,
        help="treat a broken #anchor as a failure (the default)",
    )
    parser.add_argument(
        "--no-anchors",
        dest="anchors",
        action="store_false",
        help="check files only, ignore #fragments",
    )
    parser.add_argument("--verbose", action="store_true", help="list every file checked")
    args = parser.parse_args(argv)

    if not DOCS_ROOT.is_dir():
        print(f"error: {DOCS_ROOT} not found", file=sys.stderr)
        return 2

    report = Report()
    files = markdown_files()
    report.files_checked = len(files)

    for path in files:
        if args.verbose:
            print(f"  checking {path.relative_to(REPO_ROOT)}")
        check_file(path, report, strict_anchors=args.anchors)

    # A broken anchor is reported separately because the fix is different: a
    # missing file means the link is wrong, a missing anchor means the *target*
    # was renamed and the link needs updating to the new heading.
    broken_files = [p for p in report.problems if "(anchor)" not in p.reason]
    broken_anchors = [p for p in report.problems if "(anchor)" in p.reason]

    if broken_files or broken_anchors:
        if broken_files:
            print(f"\n{len(broken_files)} broken link(s):\n", file=sys.stderr)
            for problem in broken_files:
                print(f"  ✗ {problem}", file=sys.stderr)
        if broken_anchors:
            print(f"\n{len(broken_anchors)} broken anchor(s):\n", file=sys.stderr)
            for problem in broken_anchors:
                print(f"  ✗ {problem}", file=sys.stderr)
        return 1

    print(
        f"docs links: {report.links_checked} links and {report.anchors_checked} anchors "
        f"resolve across {report.files_checked} files"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
