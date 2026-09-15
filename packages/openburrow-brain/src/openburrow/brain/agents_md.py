"""AGENTS.md interop — read it, honour it, and write back without clobbering it.

``AGENTS.md`` is not ours. It is a convention that Claude Code, Codex, Cursor,
Crush and others already read, and the fastest way to make OpenBurrow useless
would be to invent a competing file and ask everyone to write their conventions
twice.

So we do three things and no more:

* **Read it.** Anything a human wrote in ``AGENTS.md`` enters the Brain as
  human-promoted, which is the highest confidence the store hands out. A person
  typed it; that beats two agents agreeing.
* **Write to a fenced block.** Exported entries go between two HTML comment
  markers. Everything outside those markers is left byte-for-byte alone, so a
  team can keep hand-written prose, badges, and structure in the same file. The
  markers are HTML comments specifically because every Markdown renderer hides
  them — the managed block is invisible in a GitHub preview, which is where
  people actually read this file.
* **Never rewrite the whole file.** If the markers are missing we append them.
  If they are present we replace only what is between them. A tool that
  reformats your hand-written documentation when it syncs is a tool you stop
  running.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from openburrow.brain.store import Candidate
from openburrow.core.logging import get_logger
from openburrow.core.models import BrainEntry, BrainEntryType

log = get_logger(__name__)

BEGIN_MARKER = "<!-- openburrow:brain:begin -->"
END_MARKER = "<!-- openburrow:brain:end -->"

#: Heading text → entry type. Heading level is ignored: what a section is *about*
#: matters more than how deeply it is nested, and requiring a specific depth
#: would make the parser fail on files that are perfectly readable to a human.
_TYPE_BY_HEADING: dict[str, BrainEntryType] = {
    "decision": BrainEntryType.DECISION,
    "decisions": BrainEntryType.DECISION,
    "architecture decision": BrainEntryType.DECISION,
    "gotcha": BrainEntryType.GOTCHA,
    "gotchas": BrainEntryType.GOTCHA,
    "pitfall": BrainEntryType.GOTCHA,
    "pitfalls": BrainEntryType.GOTCHA,
    "convention": BrainEntryType.CONVENTION,
    "conventions": BrainEntryType.CONVENTION,
    "style": BrainEntryType.CONVENTION,
}

#: Headings we deliberately skip: they are instructions to the agent, not facts
#: about the repository, and turning them into Brain entries would duplicate what
#: the harness already reads on every run.
_SKIP_HEADINGS = frozenset({"setup", "commands", "testing", "development", "install", "usage"})

#: A path mentioned in backticks at the start of a bullet, e.g. ``- `src/api/x.py`: …``
_ANCHOR_RE = re.compile(r"`([A-Za-z0-9_./-]+\.[A-Za-z0-9]+)`")


@dataclass(frozen=True, slots=True)
class AgentsMdSection:
    """One heading and its body, as written by a human."""

    heading: str
    body: str
    level: int


def parse_sections(text: str) -> list[AgentsMdSection]:
    """Split an AGENTS.md into heading-delimited sections.

    A tiny hand-rolled parser rather than a Markdown library, because the only
    structure we need is "which heading owns which lines" and a full parser would
    drag in a dependency to answer a question that is one regex wide.
    """
    sections: list[AgentsMdSection] = []
    heading = ""
    level = 0
    buffer: list[str] = []

    for line in text.splitlines():
        match = re.match(r"^(#{1,6})\s+(.*)$", line)
        if match:
            if heading or buffer:
                sections.append(AgentsMdSection(heading, "\n".join(buffer).strip(), level))
            heading = match.group(2).strip()
            level = len(match.group(1))
            buffer = []
        else:
            buffer.append(line)

    if heading or buffer:
        sections.append(AgentsMdSection(heading, "\n".join(buffer).strip(), level))
    return sections


def extract_candidates(text: str, *, source_lane: str = "agents-md") -> list[Candidate]:
    """Turn human-written AGENTS.md content into Brain candidates.

    Bullets are the unit of extraction, not sections. A section like
    "## Conventions" typically holds several independent rules, and collapsing
    them into one entry would make staleness useless — one rule going out of date
    would invalidate the whole section.
    """
    candidates: list[Candidate] = []
    for section in parse_sections(text):
        heading = section.heading.casefold()
        if not heading or heading in _SKIP_HEADINGS:
            continue
        entry_type = _TYPE_BY_HEADING.get(heading, BrainEntryType.CONVENTION)

        for bullet in _bullets(section.body):
            title, body = _split_title(bullet)
            if not title:
                continue
            candidates.append(
                Candidate(
                    title=title,
                    body=body,
                    entry_type=entry_type,
                    anchor_path=_first_path(bullet),
                    source_lane=source_lane,
                    promoted_by="agents-md-seed",
                    # A human wrote this file, so it starts above the injection
                    # floor. The store still lets a later drift sweep mark it
                    # stale, because being human-written does not stop the code
                    # it describes from changing.
                    tags=["agents-md", heading],
                )
            )
    if candidates:
        log.info("brain.agents_md.extracted", candidates=len(candidates))
    return candidates


def render_block(entries: list[BrainEntry]) -> str:
    """Render the managed block for the given entries."""
    lines = [BEGIN_MARKER, "", "<!-- Generated by OpenBurrow. Edit outside these markers. -->", ""]
    for entry in sorted(entries, key=lambda e: (str(e.entry_type), e.title)):
        lines.append(entry.to_agents_md())
        lines.append("")
    lines.append(END_MARKER)
    return "\n".join(lines)


def merge_into(existing: str, entries: list[BrainEntry]) -> str:
    """Return ``existing`` with the managed block created or replaced.

    Everything outside the markers survives verbatim, including trailing
    whitespace and the absence of a trailing newline — a sync that also reformats
    is a sync that shows up as a huge diff in code review and gets reverted.
    """
    block = render_block(entries)
    start = existing.find(BEGIN_MARKER)
    end = existing.find(END_MARKER)

    if start != -1 and end != -1 and end > start:
        end += len(END_MARKER)
        return existing[:start] + block + existing[end:]

    if not existing.strip():
        return block + "\n"

    separator = "\n\n" if not existing.endswith("\n\n") else ""
    return existing + separator + block + "\n"


def read(path: Path) -> str:
    """Read AGENTS.md, returning ``""`` when it does not exist.

    A missing AGENTS.md is the normal state of a new repository, not an error.
    """
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except OSError as exc:
        log.warning("brain.agents_md.read_failed", path=str(path), error=str(exc))
        return ""


def write(path: Path, content: str) -> None:
    """Write AGENTS.md, creating parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    log.info("brain.agents_md.written", path=str(path), bytes=len(content))


def _bullets(body: str) -> list[str]:
    """Collect top-level bullet lines, joining their continuation lines."""
    bullets: list[str] = []
    current: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith(("- ", "* ", "+ ")):
            if current:
                bullets.append(" ".join(current))
            current = [stripped[2:].strip()]
        elif current and stripped and not stripped.startswith("#"):
            current.append(stripped)
        elif current and not stripped:
            bullets.append(" ".join(current))
            current = []
    if current:
        bullets.append(" ".join(current))
    return [b for b in bullets if b]


def _split_title(bullet: str) -> tuple[str, str]:
    """Split a bullet into a short title and the remaining body.

    The title is what a lane sees when the entry is listed and what duplicate
    detection matches on, so it needs to be the *claim*, not the first line of a
    paragraph. Convention in AGENTS.md is bold-lead, so we honour that first.
    """
    bold = re.match(r"^\*\*(.+?)\*\*[:\s—-]*(.*)$", bullet)
    if bold:
        return bold.group(1).strip(), bold.group(2).strip()

    head, separator, tail = bullet.partition(":")
    if separator and 0 < len(head) <= 90:
        return head.strip(), tail.strip()
    if separator and len(head) > 90:
        return bullet.strip()[:90], bullet.strip()
    return bullet.strip(), ""


def _first_path(bullet: str) -> str:
    """First file-like token in the bullet, used as the anchor.

    Best-effort on purpose: an entry with no anchor is valid (it is a general
    convention and cannot drift), whereas guessing an anchor wrongly would make
    the entry look stale the moment an unrelated file changes.
    """
    match = _ANCHOR_RE.search(bullet)
    return match.group(1) if match else ""


__all__ = [
    "BEGIN_MARKER",
    "END_MARKER",
    "AgentsMdSection",
    "extract_candidates",
    "merge_into",
    "parse_sections",
    "read",
    "render_block",
    "write",
]
