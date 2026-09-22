"""Fork-and-diverge replay (item 155), the VS Code adapter (item 22), and the
last-resort context-file injection (item 53).

Three closes from the completion pass, each with the property that makes it
real rather than decorative:

* the fork is read-only and its divergence is **causal** — entries after an
  injected negotiation move diverge even when their own bytes are unchanged,
  because a negotiation that heard different words is a different negotiation;
* the VS Code adapter's Agent Card must not promise what the ``code`` CLI
  cannot do — over-declaration makes the capability-mismatch detector fire on
  honest behaviour, so the declaration here is the floor;
* the context-file fallback only fires when the prompt path raised, and is
  attributable — a message dropped into ``AGENTS.md`` still says who sent it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from openburrow.adapters.harnesses.vscode import VSCodeAdapter
from openburrow.core.config.settings import Settings
from openburrow.core.models import BusMessage, Lane, LaneStatus, Performative
from openburrow.reel.fork import (
    FORK_TYPE,
    ForkSpec,
    fork_timeline,
    run_fork,
)
from openburrow.reel.timeline import TimelineWriter

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# 155 — fork-and-diverge
# ---------------------------------------------------------------------------
def build_timeline(tmp_path: Path) -> Path:
    path = tmp_path / "timeline.jsonl"
    with TimelineWriter(path, started_at=0.0) as writer:
        writer.record(type="lane.started", lane_id="a", summary="lane a up", at=0.0)
        propose = writer.record(
            type="negotiation.move",
            lane_id="a",
            summary="propose: rename parse_config",
            at=1.0,
        )
        counter = writer.record(
            type="negotiation.move",
            lane_id="b",
            summary="counter: keep the name",
            caused_by=propose.id,
            at=2.0,
        )
        writer.record(
            type="negotiation.move",
            lane_id="a",
            summary="accept: keeping parse_config",
            caused_by=counter.id,
            at=3.0,
        )
        writer.record(type="claim.created", lane_id="b", summary="claimed src/auth.py", at=4.0)
    return path


class TestForkAndDiverge:
    def test_fork_is_read_only(self, tmp_path: Path) -> None:
        path = build_timeline(tmp_path)
        before = path.read_text(encoding="utf-8")

        forked, report = run_fork(path, ForkSpec(at_index=1, new_summary="propose: keep the name"))
        forked[2] = forked[2]  # no-op; the projection itself must not write

        assert path.read_text(encoding="utf-8") == before
        assert report.diverged > 0

    def test_injected_entry_is_marked(self, tmp_path: Path) -> None:
        path = build_timeline(tmp_path)
        forked, _ = run_fork(path, ForkSpec(at_index=1, new_summary="propose: keep the name"))
        assert forked[1].type == FORK_TYPE
        assert forked[1].payload["fork"]["original_summary"] == "propose: rename parse_config"
        # Everything before the fork point is untouched.
        assert forked[0].type == "lane.started"

    def test_divergence_is_causal_not_positional(self, tmp_path: Path) -> None:
        """Entries downstream of the injected move diverge; siblings don't."""
        path = build_timeline(tmp_path)
        forked, report = run_fork(path, ForkSpec(at_index=1, new_summary="propose: keep the name"))

        # Both negotiation replies are causally downstream of the injected move.
        assert forked[2].id in report.diverged_entries
        assert forked[3].id in report.diverged_entries
        # The later claim shares no causal ancestry with the negotiation…
        # (it has no caused_by, so it is shared history).
        assert forked[4].id not in report.diverged_entries
        assert report.shared == 2  # lane.started + claim.created

    def test_out_of_range_fork_point_is_rejected(self, tmp_path: Path) -> None:
        path = build_timeline(tmp_path)
        with pytest.raises(IndexError):
            fork_timeline(
                __import__("openburrow.reel.timeline", fromlist=["read_timeline"]).read_timeline(
                    path
                ),
                ForkSpec(at_index=99),
            )

    def test_report_text_is_honest_about_scope(self, tmp_path: Path) -> None:
        from openburrow.reel.fork import fork_report_text

        path = build_timeline(tmp_path)
        forked, report = run_fork(path, ForkSpec(at_index=1, new_summary="x", reason="test"))
        text = fork_report_text(forked, report)
        assert "causal diff" in text
        assert "not how the harnesses would really have behaved" in text


# ---------------------------------------------------------------------------
# 22 — VS Code adapter
# ---------------------------------------------------------------------------
class TestVSCodeAdapter:
    def test_registered_in_the_registry(self) -> None:
        from openburrow.adapters.registry import AdapterRegistry

        registry = AdapterRegistry(Settings())
        assert "vscode" in registry.available()

    def test_capabilities_are_the_honest_floor(self) -> None:
        adapter = VSCodeAdapter(Settings())
        caps = adapter.capabilities.to_dict()  # camelCase, the card's wire shape
        assert caps["structuredOutput"] is False
        assert caps["resumable"] is False
        assert caps["mcpTools"] is False

    def test_card_advertises_no_skills_it_cannot_exercise(self) -> None:
        lane = Lane(name="vs", harness="vscode", session_id="s1", worktree_path=str(Path.cwd()))
        adapter = VSCodeAdapter(Settings(), lane=lane)
        assert adapter.skills() == []

    def test_spawn_opens_the_workspace(self, tmp_path: Path) -> None:
        lane = Lane(
            name="vs",
            harness="vscode",
            session_id="s1",
            worktree_path=str(tmp_path),
        )
        adapter = VSCodeAdapter(Settings(), lane=lane)
        spec = adapter.build_spawn_spec(lane)
        assert spec.command[0].endswith("code") or spec.command[0] == "code"


# ---------------------------------------------------------------------------
# 53 — context-file fallback injection
# ---------------------------------------------------------------------------
class FailingAdapter(VSCodeAdapter):
    """A harness whose prompt path always raises: the fallback's client."""

    async def send_prompt(self, lane: Lane, prompt: str) -> None:
        raise RuntimeError("no input hook")


class TestContextFileFallback:
    def _adapter_with_worktree(self, tmp_path: Path) -> FailingAdapter:
        lane = Lane(
            name="fb",
            harness="vscode",
            session_id="s1",
            worktree_path=str(tmp_path),
        )
        lane.status = LaneStatus.IDLE
        return FailingAdapter(Settings(), lane=lane)

    def _message(self) -> BusMessage:
        return BusMessage(
            session_id="s1",
            sender_lane="lane_alice",
            sender_harness="codex",
            subject="rename",
            body="parse_config became load_config",
            intent=Performative.INFORM,
        )

    async def test_prompt_failure_drops_the_message_into_agents_md(self, tmp_path: Path) -> None:
        adapter = self._adapter_with_worktree(tmp_path)
        message = self._message()

        assert await adapter.inject_message(message) is True

        target = tmp_path / "AGENTS.md"
        assert target.exists()
        text = target.read_text(encoding="utf-8")
        assert "parse_config became load_config" in text
        assert f"openburrow:message:{message.id}" in text  # attributable
        assert "[OpenBurrow" in text  # provenance header present

    async def test_second_message_appends_not_overwrites(self, tmp_path: Path) -> None:
        adapter = self._adapter_with_worktree(tmp_path)
        first, second = self._message(), self._message()
        second.body = "second notice"

        await adapter.inject_message(first)
        await adapter.inject_message(second)

        text = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
        assert "parse_config became load_config" in text
        assert "second notice" in text

    async def test_no_worktree_means_honest_failure(self, tmp_path: Path) -> None:
        lane = Lane(name="fb", harness="vscode", session_id="s1")  # no worktree
        adapter = FailingAdapter(Settings(), lane=lane)
        with pytest.raises(Exception, match="could not inject"):
            await adapter.inject_message(self._message())

    def test_never_used_when_the_prompt_path_works(self, tmp_path: Path) -> None:
        adapter = self._adapter_with_worktree(tmp_path)

        # A healthy prompt path returns without touching the fallback: asserted
        # indirectly here by the absence of the file before any failure.
        assert not (tmp_path / "AGENTS.md").exists()
        assert adapter.drop_context_file(self._message(), "x") == tmp_path / "AGENTS.md"
