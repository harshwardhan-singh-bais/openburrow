"""Fork-and-diverge replay (roadmap item 155).

Replaying a session from step N with one A2A message changed and comparing how
the negotiation diverges is a *counterfactual analysis* over the causal log. The
mechanism is deliberately read-only: the fork is a projection of the recorded
timeline, never a mutation of it. Rewriting the log to ask "what if" would
destroy the one property the log exists to have.

Divergence semantics are causal, not positional: after the injected change, an
entry diverges when it or any entry it transitively depends on was produced by
the injected turn, negotiated in response to it, or carries the fork marker.
Entries before the fork point are identical by construction and reported as
such.

The comparison is also honest about what it cannot know: the fork shows how the
*record* would diverge, not how the harnesses would really have behaved — no
model is re-run here. It is a diff over causality, labelled as one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from openburrow.reel.timeline import TimelineEntry, read_timeline

FORK_TYPE = "fork.injected"


@dataclass(frozen=True, slots=True)
class ForkSpec:
    """What to change, and where."""

    #: Index into the timeline (0-based) of the message being changed. The
    #: change applies AT this entry; everything before it is shared history.
    at_index: int
    #: Replacement summary for the injected message. ``None`` keeps the record's.
    new_summary: str | None = None
    #: Replacement body (carried in payload for the viewer).
    new_body: str | None = None
    #: Why the fork exists — shown in the divergence report.
    reason: str = ""


@dataclass(frozen=True, slots=True)
class DivergenceReport:
    """How the forked timeline differs from the record."""

    forked_at: float
    shared: int
    diverged: int
    #: Entry ids in the fork that differ from the record (or exist only there).
    diverged_entries: list[str] = field(default_factory=list)
    #: Side-by-side pairs for the viewer: (record, fork) summaries.
    comparisons: list[dict[str, str]] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"fork at t={self.forked_at:.2f}s: {self.shared} shared, "
            f"{self.diverged} diverged entries"
        )


def fork_timeline(timeline: list[TimelineEntry], spec: ForkSpec) -> list[TimelineEntry]:
    """Project a forked timeline: the record with one message rewritten.

    Entries keep their ids, times and causal links, so a viewer can show the
    two timelines on the same clock. The injected entry is marked
    ``fork.injected`` — it is the only entry allowed to differ in type.
    """
    if not 0 <= spec.at_index < len(timeline):
        raise IndexError(
            f"fork point {spec.at_index} is outside the timeline (0..{len(timeline) - 1})"
        )
    forked: list[TimelineEntry] = []
    for index, entry in enumerate(timeline):
        if index < spec.at_index:
            forked.append(entry)
            continue
        if index == spec.at_index:
            forked.append(
                TimelineEntry(
                    t=entry.t,
                    seq=entry.seq,
                    type=FORK_TYPE,
                    lane_id=entry.lane_id,
                    summary=spec.new_summary or entry.summary,
                    caused_by=entry.caused_by,
                    id=entry.id,
                    payload={
                        **entry.payload,
                        "fork": {
                            "reason": spec.reason,
                            "original_type": entry.type,
                            "original_summary": entry.summary,
                            "body": spec.new_body or (entry.payload.get("body") or ""),
                        },
                    },
                )
            )
            continue
        forked.append(entry)
    return forked


def _dependency_reach(entries: list[TimelineEntry], roots: set[str]) -> set[str]:
    """Entry ids reachable from ``roots`` through ``caused_by`` in either direction.

    The record encodes causality once — ``entry.caused_by`` points at its
    cause — so a reply to an injected move is a *later* entry whose
    ``caused_by`` names the injected id. Divergence therefore has to walk both
    ways: backwards for anything the injected turn itself depended on (none,
    for an injected root, but correct for a mid-chain fork) and forwards for
    every reply built on it. One traversal over an undirected view of the
    chain, iterative because a long negotiation is a deep chain, and edge-
    bounded so a corrupt cycle in the record degrades to a partial answer
    instead of a hang.
    """
    by_id = {e.id: e for e in entries if e.id}
    children: dict[str, list[str]] = {}
    for entry in entries:
        if entry.id and entry.caused_by in by_id:
            children.setdefault(entry.caused_by, []).append(entry.id)

    reach: set[str] = set()
    frontier = [root for root in roots if root in by_id]
    seen_edges = 0
    limit = 100_000
    while frontier and seen_edges < limit:
        current = frontier.pop()
        if current in reach:
            continue
        reach.add(current)
        entry = by_id[current]
        neighbours: list[str] = list(children.get(current, ()))
        if entry.caused_by:
            neighbours.append(entry.caused_by)
        for neighbour in neighbours:
            if neighbour not in reach:
                frontier.append(neighbour)
                seen_edges += 1
    return reach


def compare_with_record(
    record: list[TimelineEntry], forked: list[TimelineEntry]
) -> DivergenceReport:
    """Diff the fork against the record, causally.

    A forked entry diverges when it (or any ancestor) is the injected turn —
    everything after an injected negotiation move is downstream of the change
    even if its own bytes are unchanged, because a negotiation that heard
    different words is a different negotiation.
    """
    injected_ids = {e.id for e in forked if e.type == FORK_TYPE and e.id}
    reach = _dependency_reach(forked, injected_ids)

    record_by_id = {e.id: e for e in record if e.id}
    comparisons: list[dict[str, str]] = []
    diverged_entries: list[str] = []
    shared = 0

    for entry in forked:
        if entry.id in reach or entry.type == FORK_TYPE:
            diverged_entries.append(entry.id)
            original = record_by_id.get(entry.id)
            if (
                original is not None
                and (original.summary != entry.summary or original.type != entry.type)
            ) or original is not None:
                comparisons.append(
                    {
                        "id": entry.id,
                        "record": f"{original.type}: {original.summary}",
                        "fork": f"{entry.type}: {entry.summary}",
                    }
                )
        else:
            shared += 1

    forked_at = next((e.t for e in forked if e.type == FORK_TYPE), 0.0)
    return DivergenceReport(
        forked_at=forked_at,
        shared=shared,
        diverged=len(diverged_entries),
        diverged_entries=diverged_entries,
        comparisons=comparisons,
    )


def run_fork(timeline_path: Path, spec: ForkSpec) -> tuple[list[TimelineEntry], DivergenceReport]:
    """One-call fork: read, project, compare. The CLI and viewer entry point."""
    record = read_timeline(Path(timeline_path))
    forked = fork_timeline(record, spec)
    return forked, compare_with_record(record, forked)


def fork_report_text(forked: list[TimelineEntry], report: DivergenceReport) -> str:
    """Render the divergence for the terminal.

    ``forked`` is accepted so the signature reads as "show me this fork" and so
    a future renderer can quote fork-only entries; today the comparisons carry
    everything the terminal needs.
    """
    del forked  # see docstring: reserved for fork-only quoting"
    lines = [f"fork-and-diverge — {report.summary()}", ""]
    for comparison in report.comparisons[:20]:
        lines.append(f"  [{comparison['id']}]")
        lines.append(f"    record: {comparison['record'][:110]}")
        lines.append(f"    fork:   {comparison['fork'][:110]}")
    if report.comparisons:
        lines.append("")
    lines.append(
        "This is a causal diff over the recorded log — it shows how the record "
        "would differ, not how the harnesses would really have behaved."
    )
    return "\n".join(lines)


__all__ = [
    "FORK_TYPE",
    "DivergenceReport",
    "ForkSpec",
    "compare_with_record",
    "fork_report_text",
    "fork_timeline",
    "run_fork",
]
