"""Stage 11 tests: Codex, OpenCode and Crush.

Stage 11 is "Claude Code, Codex, OpenCode, Crush". The Claude Code mapping is
covered in ``test_generic_interpretation.py`` and driven end to end over a real
PTY in ``test_pty_reader.py``; this module covers the other three.

Each of the three had the same class of defect as Claude Code — a frame or a
chunk that produced nothing, with no error — plus one that is specific to
OpenCode and worse: a hook that existed to report degradation and was called by
nothing.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from openburrow.adapters.base import _strip_ansi
from openburrow.adapters.harnesses import codex as codex_module
from openburrow.adapters.harnesses.codex import CodexAdapter
from openburrow.adapters.harnesses.crush import CrushAdapter
from openburrow.adapters.harnesses.opencode import OpenCodeAdapter
from openburrow.core.config.settings import Settings
from openburrow.core.models import Lane

FIXTURES = Path(__file__).parent / "fixtures"
REPO_ROOT = Path(__file__).resolve().parents[3]

SRC_ROOTS = [
    str(REPO_ROOT / "packages" / name / "src")
    for name in (
        "openburrow-core",
        "openburrow-a2a",
        "openburrow-acp",
        "openburrow-adapters",
        "openburrow-daemon",
    )
]


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def make_lane(harness: str, **overrides: object) -> Lane:
    return Lane(name="probe", harness=harness, session_id="sess_probe", **overrides)


def ansi_wrap(text: str) -> str:
    """Wrap every line in SGR codes, the way a styled TUI emits a chunk."""
    return "".join(f"\x1b[3{i % 8}m{line}\x1b[0m\n" for i, line in enumerate(text.splitlines()))


# --- Codex -----------------------------------------------------------------


@pytest.mark.unit
class TestCodexFrames:
    """Codex wraps every event; the base mapper reads the top level."""

    @staticmethod
    def frames() -> list[dict]:
        return [json.loads(line) for line in fixture("codex_assumed_shape.jsonl").splitlines()]

    @staticmethod
    def adapter(lane: Lane) -> CodexAdapter:
        return CodexAdapter(Settings(), lane=lane)

    def test_envelope_is_unwrapped(self) -> None:
        """Without this the frame yields kind ``result`` and empty text."""
        lane = make_lane("codex")
        output = self.adapter(lane)._from_json(self.frames()[0])

        assert output.text == "I will patch the parser."
        assert output.kind == "result"

    def test_agent_message_reaches_the_bus(self) -> None:
        """Kind ``agent_message`` is not broadcast, so the lane was mute."""
        lane = make_lane("codex")
        adapter = self.adapter(lane)

        message = adapter.translate_output(adapter._from_json(self.frames()[0]))

        assert message is not None, "the agent message was not broadcast"
        assert "patch the parser" in message.body

    def test_tool_frame_names_the_command(self) -> None:
        """A command event carries no prose, so it would be dropped."""
        lane = make_lane("codex")
        output = self.adapter(lane)._from_json(self.frames()[1])

        assert output.kind == "tool-call"
        assert output.text == "tool: exec_command_begin"

    def test_reasoning_stays_off_the_bus(self) -> None:
        """Reasoning is chatter; ``text`` is deliberately not broadcast."""
        lane = make_lane("codex")
        adapter = self.adapter(lane)
        output = adapter._from_json(self.frames()[2])

        assert output.kind == "text"
        assert adapter.translate_output(output) is None

    def test_task_complete_is_terminal(self) -> None:
        lane = make_lane("codex")
        output = self.adapter(lane)._from_json(self.frames()[3])

        assert output.kind == "result"
        assert output.terminal is True
        assert "tests pass" in output.text

    def test_envelope_id_is_kept_for_correlation(self) -> None:
        lane = make_lane("codex")
        output = self.adapter(lane)._from_json(self.frames()[0])

        assert output.data["openburrow:envelopeId"] == "0"
        assert output.data["openburrow:frame"] == "agent_message"

    def test_approval_request_is_a_blocking_status(self) -> None:
        lane = make_lane("codex")
        output = self.adapter(lane)._from_json(
            {"id": "9", "msg": {"type": "agent_message", "message": "This requires approval."}}
        )

        assert output.kind == "status"
        assert output.data["openburrow:requiresApproval"] is True

    def test_prose_mentioning_approval_is_not_blocking(self) -> None:
        """A substring test matched an agent describing its own plan.

        "I'll ask for approval before deleting" is a working lane talking, not a
        lane blocked on a human. The detector now requires the request form.
        """
        lane = make_lane("codex")
        output = self.adapter(lane)._from_json(
            {
                "id": "9",
                "msg": {
                    "type": "agent_message",
                    "message": "I'll ask for approval before deleting anything.",
                },
            }
        )

        assert output.kind == "result"
        assert "openburrow:requiresApproval" not in output.data

    def test_the_assumption_is_labelled(self) -> None:
        """The mapping is a hypothesis, and nothing should quietly promote it.

        A fixture written to match the code turns a guess into a passing test,
        and a docstring that reads like a specification turns it into a fact.
        Guarding both means promoting this to "verified" takes a deliberate
        deletion rather than a quiet edit.
        """
        module_doc = codex_module.__doc__ or ""
        assert "assumed, not verified" in module_doc.lower()
        assert (FIXTURES / "codex_assumed_shape.jsonl").exists()
        assert "assumed" in CodexAdapter.__dict__["_from_json"].__doc__.lower()


# --- OpenCode --------------------------------------------------------------


@pytest.mark.unit
class TestOpenCodeServerMode:
    """Server mode is the reference path: a field rename, not a heuristic."""

    @staticmethod
    def messages() -> list[dict]:
        return json.loads(fixture("opencode_server_messages.json"))

    @staticmethod
    def adapter(lane: Lane) -> OpenCodeAdapter:
        return OpenCodeAdapter(Settings(), lane=lane)

    def test_assistant_text(self) -> None:
        lane = make_lane("opencode")
        output = self.adapter(lane)._interpret_server_message(self.messages()[0])

        assert output is not None
        assert output.kind == "text"
        assert output.structured is True
        assert output.text == "I will update the parser."

    def test_nested_usage_keys_are_recorded(self) -> None:
        """``input_tokens``/``output_tokens`` are the keys; the short forms are not."""
        lane = make_lane("opencode")
        self.adapter(lane)._interpret_server_message(self.messages()[0])

        assert lane.tokens_in == 12
        assert lane.tokens_out == 8

    def test_patch_becomes_a_diff_artifact(self) -> None:
        lane = make_lane("opencode")
        output = self.adapter(lane)._interpret_server_message(self.messages()[2])

        assert output is not None
        assert output.kind == "diff"
        assert len(output.artifacts) == 1

    def test_cost_is_recorded_once(self) -> None:
        lane = make_lane("opencode")
        self.adapter(lane)._interpret_server_message(self.messages()[2])

        assert lane.cost_usd == pytest.approx(0.0021)

    def test_non_numeric_cost_does_not_raise(self) -> None:
        """``record_usage`` applies ``max(0.0, cost)``, so a string raises inside the model."""
        lane = make_lane("opencode")
        item = {**self.messages()[3], "cost": "unknown"}

        output = self.adapter(lane)._interpret_server_message(item)

        assert output is not None
        assert lane.cost_usd == 0.0

    def test_completed_message_is_terminal(self) -> None:
        lane = make_lane("opencode")
        output = self.adapter(lane)._interpret_server_message(self.messages()[3])

        assert output is not None
        assert output.terminal is True

    def test_user_message_is_not_lane_output(self) -> None:
        lane = make_lane("opencode")
        assert self.adapter(lane)._interpret_server_message(self.messages()[4]) is None

    def test_null_info_does_not_raise(self) -> None:
        """``item.get("info", {}).get(...)`` only defaults when the key is absent."""
        lane = make_lane("opencode")
        output = self.adapter(lane)._interpret_server_message(
            {
                "id": "m",
                "info": None,
                "role": "assistant",
                "parts": [{"type": "text", "text": "hi"}],
            }
        )

        assert output is not None
        assert output.text == "hi"


@pytest.mark.unit
class TestOpenCodeCapabilities:
    """A degraded adapter must not keep advertising what it lost."""

    def test_declared_capabilities_claim_structured_output(self) -> None:
        lane = make_lane("opencode")
        assert self._adapter(lane).capabilities.structured_output is True

    def test_pty_fallback_reports_degraded_capabilities(self) -> None:
        lane = make_lane("opencode")
        adapter = self._adapter(lane)
        adapter._server_mode = False

        caps = adapter.effective_capabilities()

        assert caps.structured_output is False
        assert caps.resumable is False
        assert caps.supports_interrupt is False

    def test_server_mode_reports_full_capabilities(self) -> None:
        lane = make_lane("opencode")
        adapter = self._adapter(lane)
        adapter._server_mode = True

        assert adapter.effective_capabilities().structured_output is True

    def test_the_daemon_consults_effective_capabilities(self) -> None:
        """The Agent Card was built from ``capabilities``, so the hook was dead.

        ``effective_capabilities`` was defined, documented as the thing the
        governance layer compares against, and called by nothing. This asserts
        the call site, because a behavioural test would need a whole daemon and
        the regression is precisely "somebody changed the call site back".
        """
        source = (
            REPO_ROOT
            / "packages"
            / "openburrow-daemon"
            / "src"
            / "openburrow"
            / "daemon"
            / "sessions.py"
        ).read_text(encoding="utf-8")

        assert "effective_capabilities()" in source, "the Agent Card no longer consults it"
        assert "capabilities=adapter.capabilities," not in source

    @staticmethod
    def _adapter(lane: Lane) -> OpenCodeAdapter:
        return OpenCodeAdapter(Settings(), lane=lane)


@pytest.mark.unit
class TestOpenCodePtyFallback:
    """The degraded path, which is still a path."""

    @staticmethod
    def adapter(lane: Lane) -> OpenCodeAdapter:
        return OpenCodeAdapter(Settings(), lane=lane)

    def test_diff_after_a_banner_is_found(self) -> None:
        """The check was ``startswith``, so only a diff at offset 0 counted.

        A TUI prints a banner before anything useful, so in practice no diff was
        ever recognised on this path.
        """
        lane = make_lane("opencode")
        output = self.adapter(lane)._parse_pty_chunk(fixture("opencode_pty_chunk.txt"))

        assert output is not None
        assert output.kind == "diff"
        assert len(output.artifacts) == 1

    def test_ansi_wrapped_diff_is_found(self) -> None:
        lane = make_lane("opencode")
        output = self.adapter(lane)._parse_pty_chunk(ansi_wrap(fixture("opencode_pty_chunk.txt")))

        assert output is not None
        assert output.kind == "diff"
        assert "\x1b" not in output.text

    def test_pty_fallback_output_is_not_structured(self) -> None:
        """The honesty flag: this path guessed, so it must not claim otherwise."""
        lane = make_lane("opencode")
        output = self.adapter(lane)._parse_pty_chunk(fixture("opencode_pty_chunk.txt"))

        assert output is not None
        assert output.structured is False

    def test_json_chunk_is_structured(self) -> None:
        lane = make_lane("opencode")
        output = self.adapter(lane)._parse_pty_chunk('{"type": "result", "result": "ok"}')

        assert output is not None
        assert output.structured is True

    def test_usage_limit_chunk_is_terminal(self) -> None:
        lane = make_lane("opencode")
        output = self.adapter(lane)._parse_pty_chunk(fixture("rate_limit_notice.txt"))

        assert output is not None
        assert output.kind == "error"
        assert output.terminal is True

    def test_blank_chunk_is_nothing(self) -> None:
        lane = make_lane("opencode")
        assert self.adapter(lane)._parse_pty_chunk("  \n\n ") is None


# --- Crush -----------------------------------------------------------------


@pytest.mark.unit
class TestCrush:
    """Crush's TUI is styled; the diff must survive it."""

    @staticmethod
    def adapter(lane: Lane) -> CrushAdapter:
        return CrushAdapter(Settings(), lane=lane)

    def test_diff_survives_styling(self) -> None:
        lane = make_lane("crush")
        outputs = self.adapter(lane)._interpret(ansi_wrap(fixture("crush_tui_chunk.txt")))

        assert [o.kind for o in outputs] == ["diff"]
        assert "\x1b" not in outputs[0].text

    def test_agents_md_is_noted(self) -> None:
        lane = make_lane("crush")
        outputs = self.adapter(lane)._interpret(fixture("crush_tui_chunk.txt"))

        assert outputs[0].data["openburrow:readAgentsMd"] is True

    def test_agents_md_absent_is_not_noted(self) -> None:
        lane = make_lane("crush")
        outputs = self.adapter(lane)._interpret(fixture("diff_plain.txt"))

        assert "openburrow:readAgentsMd" not in outputs[0].data

    def test_crush_has_no_private_ansi_stripper(self) -> None:
        """Two definitions of "a control sequence" is one too many.

        Crush carried its own ``_strip_ansi`` with a narrower pattern that missed
        the two-character escapes, while the base stripped ANSI with a different
        one. They drift, and the drift shows up as a diff that parses for one
        adapter and not another.
        """
        source = (
            REPO_ROOT
            / "packages"
            / "openburrow-adapters"
            / "src"
            / "openburrow"
            / "adapters"
            / "harnesses"
            / "crush.py"
        ).read_text(encoding="utf-8")

        assert "def _strip_ansi" not in source
        assert "_ANSI_PATTERN" not in source


@pytest.mark.unit
class TestAnsiStrippingIsShared:
    def test_two_character_escapes_are_handled(self) -> None:
        """The pattern Crush used missed these; the shared one does not."""
        assert _strip_ansi("\x1bM--- a/x.py\n") == "--- a/x.py\n"

    def test_csi_and_osc_are_handled(self) -> None:
        assert _strip_ansi("\x1b[31mred\x1b[0m") == "red"
        assert _strip_ansi("\x1b]0;title\x07text") == "text"


# --- cross-process stability ----------------------------------------------


@pytest.mark.integration
class TestStablePortDerivation:
    """``hash()`` on a str is randomised per process, so it is not a stable id."""

    @staticmethod
    def _port_from_fresh_interpreter(lane_id: str) -> int:
        code = (
            "import sys\n"
            f"sys.path[:0] = {SRC_ROOTS!r}\n"
            "from openburrow.adapters.harnesses.opencode import OpenCodeAdapter\n"
            "from openburrow.core.config.settings import Settings\n"
            "from openburrow.core.models import Lane\n"
            "lane = Lane(name='probe', harness='opencode', session_id='s', "
            f"id={lane_id!r})\n"
            "print(OpenCodeAdapter(Settings())._server_port_for(lane))\n"
        )
        result = subprocess.run(  # noqa: S603 - argv is built here from constants, no shell
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        )
        return int(result.stdout.strip())

    def test_port_is_identical_in_every_process(self) -> None:
        """A restarted lane must reclaim its port, or clients reconnect to nothing.

        Four separate interpreters, because a single process cannot observe
        this: ``hash()`` is stable *within* a run and different across runs, so
        the bug is invisible to any test that does not cross a process boundary.
        """
        lane_id = "lane_01H8ZQ4K2MSTABLE"
        ports = {self._port_from_fresh_interpreter(lane_id) for _ in range(4)}

        assert len(ports) == 1, f"the port changed between processes: {sorted(ports)}"

    def test_different_lanes_get_different_ports(self) -> None:
        first = self._port_from_fresh_interpreter("lane_alpha")
        second = self._port_from_fresh_interpreter("lane_beta")

        assert first != second
