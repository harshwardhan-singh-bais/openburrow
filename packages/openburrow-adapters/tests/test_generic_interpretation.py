"""Interpretation-path tests for the generic CLI adapter.

Stage 10 of the roadmap claims the adapter is verified by "each interpretation
path is exercised with fixture output". These are those exercises, against the
corpus in ``fixtures/``.

Two of them are regression tests for false positives that destroyed real work
rather than merely mislabelling it — a diff containing the digits ``429``, and
prose that discusses rate limiting. Both used to be classified as terminal
provider errors, and a terminal error discards the lane's output and ends the
stream. See the fixtures README for why those two cases are the interesting
ones.

The Claude Code tests cover a different failure, and a quieter one: frames that
mapped to a ``kind`` the bus does not broadcast. Nothing raised, nothing was
logged, and the lane's replies simply never arrived.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openburrow.adapters.base import _ChunkAssembler, _strip_ansi
from openburrow.adapters.harnesses.claude_code import ClaudeCodeAdapter
from openburrow.adapters.harnesses.generic import GenericCliAdapter
from openburrow.core.config.settings import Settings
from openburrow.core.models import Lane

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    """Read a fixture verbatim. Line endings are part of what is under test."""
    return (FIXTURES / name).read_text(encoding="utf-8")


def make_lane(**overrides: object) -> Lane:
    return Lane(name="probe", harness="generic-cli", session_id="sess_probe", **overrides)


class StructuredAdapter(GenericCliAdapter):
    """Generic adapter with structured mode on, so the JSON path is reachable.

    ``has_structured_mode`` is what gates the first branch of ``_interpret``, and
    the base class leaves it False — so a test that only instantiated
    ``GenericCliAdapter`` would silently exercise the fallback parser five times
    and never touch the JSON mapper at all.
    """

    name = "structured-probe"
    binary = "python"
    has_structured_mode = True


def ansi_wrap(text: str) -> str:
    """Wrap every line in SGR colour codes, the way a colour TUI emits a diff.

    Built here rather than stored as a fixture: escape bytes are invisible in a
    diff view, so a stored fixture cannot be reviewed for whether it kept them.
    """
    return "".join(f"\x1b[3{i % 8}m{line}\x1b[0m\n" for i, line in enumerate(text.splitlines()))


# --- the five documented paths --------------------------------------------


class TestInterpretationPaths:
    """One test per row of the order table in ``_interpret``'s docstring."""

    def test_diff_is_recognised(self) -> None:
        adapter = GenericCliAdapter(Settings())
        outputs = adapter._interpret(fixture("diff_plain.txt"))

        assert len(outputs) == 1
        assert outputs[0].kind == "diff"
        assert len(outputs[0].artifacts) == 1

    def test_plan_numbered_is_recognised(self) -> None:
        adapter = GenericCliAdapter(Settings())
        outputs = adapter._interpret(fixture("plan_numbered.txt"))

        assert [o.kind for o in outputs] == ["plan"]
        assert len(outputs[0].data["steps"]) == 3

    def test_plan_bulleted_is_recognised(self) -> None:
        adapter = GenericCliAdapter(Settings())
        outputs = adapter._interpret(fixture("plan_bulleted.txt"))

        assert [o.kind for o in outputs] == ["plan"]
        assert len(outputs[0].data["steps"]) == 3

    def test_usage_limit_notice_is_a_terminal_error(self) -> None:
        adapter = GenericCliAdapter(Settings())
        outputs = adapter._interpret(fixture("rate_limit_notice.txt"))

        assert [o.kind for o in outputs] == ["error"]
        assert outputs[0].terminal is True
        assert outputs[0].data["usage_limit"]

    def test_plain_prose_falls_back_to_text(self) -> None:
        adapter = GenericCliAdapter(Settings())
        outputs = adapter._interpret(fixture("prose_plain.txt"))

        assert [o.kind for o in outputs] == ["text"]

    def test_blank_input_yields_nothing(self) -> None:
        """A PTY read that returns only whitespace is not an output."""
        adapter = GenericCliAdapter(Settings())
        assert adapter._interpret("   \n\r\n  ") == []

    def test_two_list_lines_are_prose_not_a_plan(self) -> None:
        """The plan threshold is three; two is a sentence that contains a list."""
        adapter = GenericCliAdapter(Settings())
        outputs = adapter._interpret("1. first thing\n2. second thing")

        assert [o.kind for o in outputs] == ["text"]


# --- the two adversarial cases --------------------------------------------


class TestExpensiveFalsePositives:
    """Detectors may be noisy. These two were noisy *and* wired to an action."""

    def test_diff_touching_line_429_is_a_diff(self) -> None:
        """A bare 429 is a line number, not an HTTP status.

        The old detector substring-matched ``"429"`` anywhere in a chunk. This
        diff has it in the hunk header and again in the body, so it was
        classified as a terminal rate-limit error and the diff — the entire
        point of the lane's work — was thrown away.
        """
        adapter = GenericCliAdapter(Settings())
        outputs = adapter._interpret(fixture("adversarial_diff_line_429.txt"))

        assert [o.kind for o in outputs] == ["diff"], "the diff was replaced by a usage-limit error"
        assert outputs[0].terminal is False

    def test_prose_about_rate_limiting_is_not_a_limit(self) -> None:
        """The gerund is how a person discusses the concept.

        "I'll add rate limiting to the retry loop" matched the substring
        ``"rate limit"``. It is a plan, and it was being read as the provider
        saying it had run out of budget.
        """
        adapter = GenericCliAdapter(Settings())
        outputs = adapter._interpret(fixture("adversarial_prose_rate_limiting.txt"))

        assert [o.kind for o in outputs] == ["text"]

    def test_http_status_429_is_still_detected(self) -> None:
        """Narrowing the match must not make the real notice unreachable."""
        adapter = GenericCliAdapter(Settings())
        for text in ("HTTP 429", "status code 429", "Error 429: too many requests"):
            assert adapter.detect_usage_limit(text) is not None, text

    def test_bare_429_alone_is_not_a_limit(self) -> None:
        adapter = GenericCliAdapter(Settings())
        assert adapter.detect_usage_limit("took 429 ms to parse") is None
        assert adapter.detect_usage_limit("listening on port 4290") is None


# --- ANSI and line endings -------------------------------------------------


class TestTerminalNoise:
    """A PTY is not a pipe. What it adds must not change the classification."""

    def test_ansi_wrapped_diff_is_still_a_diff(self) -> None:
        """``_DIFF_HEADER`` anchors on ``^``, so SGR codes before a line break it."""
        adapter = GenericCliAdapter(Settings())
        wrapped = ansi_wrap(fixture("diff_plain.txt"))
        assert "\x1b[" in wrapped

        outputs = adapter._interpret(wrapped)

        assert [o.kind for o in outputs] == ["diff"]

    def test_ansi_is_removed_from_emitted_text(self) -> None:
        adapter = GenericCliAdapter(Settings())
        outputs = adapter._interpret(ansi_wrap(fixture("diff_plain.txt")))

        assert "\x1b" not in outputs[0].text
        assert outputs[0].text.startswith("--- a/src/parser.py")

    def test_crlf_is_normalised(self) -> None:
        """PTY line endings are CRLF; a CR inside a diff artifact corrupts it."""
        adapter = GenericCliAdapter(Settings())
        crlf = fixture("diff_plain.txt").replace("\n", "\r\n")

        outputs = adapter._interpret(crlf)

        assert [o.kind for o in outputs] == ["diff"]
        assert "\r" not in outputs[0].text

    def test_strip_ansi_is_a_noop_on_clean_text(self) -> None:
        clean = "--- a/x.py\n+++ b/x.py\n"
        assert _strip_ansi(clean) == clean


# --- chunk reassembly ------------------------------------------------------


class TestChunkAssembler:
    """A PTY read is not aligned to anything; JSONL frames are line-delimited."""

    def test_split_frame_is_reassembled_at_every_offset(self) -> None:
        """A 1.7 KB frame against a 4096-byte read lands split, every time.

        Before the assembler, ``_extract_json_objects`` saw a fragment, failed
        the "starts with ``{`` and ends with ``}``" test, and dropped it — so
        the harness looked silent. Sweeping every split offset is the point:
        a test that split at one offset would pass on a lucky choice.
        """
        frame = json.dumps({"type": "assistant", "text": "hello"})
        stream = frame + "\n"

        for offset in range(1, len(stream)):
            adapter = StructuredAdapter(Settings())
            assembler = _ChunkAssembler(line_mode=True)
            units = assembler.feed(stream[:offset]) + assembler.feed(stream[offset:])
            units += assembler.flush()

            assert len(units) == 1, f"offset {offset} produced {units!r}"
            outputs = adapter._interpret(units[0])
            assert [o.kind for o in outputs] == ["assistant"], f"offset {offset}"

    def test_two_frames_in_one_read_are_separated(self) -> None:
        assembler = _ChunkAssembler(line_mode=True)
        units = assembler.feed('{"a":1}\n{"b":2}\n')

        assert units == ['{"a":1}', '{"b":2}']

    def test_unstructured_mode_does_not_split(self) -> None:
        """A diff is not line-delimited; splitting one would make a dozen."""
        assembler = _ChunkAssembler(line_mode=False)
        units = assembler.feed("--- a/x.py\n+++ b/x.py\n")

        assert units == ["--- a/x.py\n+++ b/x.py\n"]

    def test_flush_releases_a_trailing_partial(self) -> None:
        """A harness that dies mid-frame still gets its last line interpreted."""
        assembler = _ChunkAssembler(line_mode=True)
        assembler.feed('{"type":"result"}')

        assert assembler.flush() == ['{"type":"result"}']
        assert assembler.flush() == []

    def test_whitespace_only_units_are_dropped(self) -> None:
        assembler = _ChunkAssembler(line_mode=True)
        assert assembler.feed("\n\n  \n") == []

    def test_overflow_releases_the_buffer(self) -> None:
        """A harness that never emits a newline must not grow the buffer forever."""
        assembler = _ChunkAssembler(line_mode=True, max_buffer=32)
        units = assembler.feed("x" * 64)

        assert units == ["x" * 64]


# --- the structured flag ---------------------------------------------------


class TestStructuredFlag:
    """``structured`` is the honesty flag the metrics layer keys off."""

    def test_json_frames_are_structured(self) -> None:
        adapter = StructuredAdapter(Settings())
        outputs = adapter._interpret(fixture("claude_stream_json.jsonl"))

        assert outputs, "the JSONL stream produced no outputs at all"
        assert all(o.structured for o in outputs)

    def test_regex_derived_outputs_are_not_structured(self) -> None:
        """A diff recovered by pattern-matching is a good guess, not a mapping."""
        adapter = GenericCliAdapter(Settings())
        for name in ("diff_plain.txt", "plan_numbered.txt", "prose_plain.txt"):
            outputs = adapter._interpret(fixture(name))
            assert all(not o.structured for o in outputs), name


# --- Claude Code frame mapping --------------------------------------------


class TestClaudeCodeFrames:
    """``kind`` is the harness's vocabulary; the bus speaks a different one."""

    @staticmethod
    def frames() -> list[dict]:
        return [json.loads(line) for line in fixture("claude_stream_json.jsonl").splitlines()]

    @staticmethod
    def adapter(lane: Lane) -> ClaudeCodeAdapter:
        return ClaudeCodeAdapter(Settings(), lane=lane)

    def test_assistant_text_is_prose_not_a_repr(self) -> None:
        """The nested envelope was being stringified into a Python dict literal."""
        lane = make_lane()
        adapter = self.adapter(lane)
        frame = self.frames()[1]

        output = adapter._from_json(frame)

        assert output.text == "I will patch the parser."
        assert "'id':" not in output.text

    def test_assistant_text_reaches_the_bus(self) -> None:
        """Kind ``"assistant"`` is not broadcast, so the lane was silently mute.

        This is the failure the override exists for: no exception, no warning,
        and a reply that never arrived.
        """
        lane = make_lane()
        adapter = self.adapter(lane)
        output = adapter._from_json(self.frames()[1])

        message = adapter.translate_output(output)

        assert message is not None, "the assistant reply was not broadcast"
        assert "patch the parser" in message.body

    def test_nested_usage_is_recorded(self) -> None:
        """Usage sits at ``message.usage``; a top-level lookup returned zero."""
        lane = make_lane()
        adapter = self.adapter(lane)

        adapter._from_json(self.frames()[1])

        assert lane.tokens_in == 10
        assert lane.tokens_out == 7

    def test_result_frame_is_terminal_and_reports_usage(self) -> None:
        lane = make_lane()
        adapter = self.adapter(lane)

        output = adapter._from_json(self.frames()[4])

        assert output.kind == "result"
        assert output.terminal is True
        assert lane.tokens_in == 200
        assert lane.tokens_out == 50

    def test_full_stream_accumulates_usage(self) -> None:
        """Three frames carry usage: 10/7, 18/12, 200/50."""
        lane = make_lane()
        adapter = self.adapter(lane)

        for frame in self.frames():
            adapter._from_json(frame)

        assert lane.tokens_in == 228
        assert lane.tokens_out == 69

    def test_tool_use_frame_names_the_tool(self) -> None:
        lane = make_lane()
        adapter = self.adapter(lane)

        output = adapter._from_json(self.frames()[2])

        assert output.kind == "tool-call"
        assert output.data["openburrow:tools"] == ["Read"]
        assert output.text == "tool_use: Read"

    def test_init_frame_captures_the_session_id(self) -> None:
        """``is_resumable`` is True, so something has to hold the resume handle."""
        lane = make_lane()
        adapter = self.adapter(lane)

        output = adapter._from_json(self.frames()[0])

        assert output.kind == "status"
        assert lane.metadata["harness_session_id"] == "sess_01H8ZQ4K2M"

    def test_init_frame_is_not_broadcast_empty(self) -> None:
        """A message with no body and no artifact carries no information."""
        lane = make_lane()
        adapter = self.adapter(lane)
        output = adapter._from_json(self.frames()[0])

        assert output.text == ""
        assert adapter.translate_output(output) is None

    def test_error_result_frame_is_an_error(self) -> None:
        lane = make_lane()
        adapter = self.adapter(lane)
        frame = {**self.frames()[4], "is_error": True, "result": "API error"}

        output = adapter._from_json(frame)

        assert output.kind == "error"
        assert output.terminal is True

    def test_cumulative_cost_is_not_accumulated(self) -> None:
        """``total_cost_usd`` is cumulative; ``record_usage`` accumulates.

        Feeding a running total into an accumulating counter reports the total
        as the cost of every turn and inflates the figure quadratically.
        """
        lane = make_lane()
        adapter = self.adapter(lane)

        for frame in self.frames():
            adapter._from_json(frame)

        assert lane.cost_usd == 0.0
        assert adapter._from_json(self.frames()[4]).data["total_cost_usd"] == 0.0123

    def test_parse_usage_finds_nested_usage(self) -> None:
        lane = make_lane()
        adapter = self.adapter(lane)

        assert adapter.parse_usage(json.dumps(self.frames()[1])) == {
            "input_tokens": 10,
            "output_tokens": 7,
        }

    def test_parse_usage_reports_nothing_honestly(self) -> None:
        """An empty dict is the honest answer when the harness says nothing."""
        lane = make_lane()
        adapter = self.adapter(lane)

        assert adapter.parse_usage("no json here at all") == {}
