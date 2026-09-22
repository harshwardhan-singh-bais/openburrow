"""Stage 12 tests: Gemini, Aider, Goose and the bring-your-own adapter.

Stage 12 is "the remaining harness adapters". Its stated verification is that
``burrow adapters list`` reports availability without a harness installed and
that a missing optional dependency disables one adapter rather than breaking the
registry — covered in :class:`TestRegistryRobustness`.

The rest of this module covers the defects found while verifying the four
adapters. They are not four unrelated bugs. Three of them are the same bug:

* a flag that answers **"can you?"** being used to answer **"should you?"**, so
  a harness that could not *promise* structure never had its structure *parsed*;
* a detector anchored on nothing, so it matched whatever happened to be nearby;
* a declaration that contradicted the module's own docstring, and a reader that
  contradicted the harness's own documented protocol.

And one that is a different shape: the daemon kept a private copy of a decision
the adapter layer already owned, and the copy had drifted.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from openburrow.adapters import registry as registry_module
from openburrow.adapters.base import HarnessAdapter
from openburrow.adapters.harnesses import gemini as gemini_module, generic as generic_module
from openburrow.adapters.harnesses.aider import AiderAdapter
from openburrow.adapters.harnesses.custom import CustomScriptAdapter
from openburrow.adapters.harnesses.gemini import GeminiAdapter
from openburrow.adapters.harnesses.generic import GenericCliAdapter
from openburrow.adapters.harnesses.goose import GooseAdapter
from openburrow.adapters.registry import AdapterRegistry, build_registry
from openburrow.core.config.settings import Settings
from openburrow.core.errors import ConfigError
from openburrow.core.models import Lane

FIXTURES = Path(__file__).parent / "fixtures"
REPO_ROOT = Path(__file__).resolve().parents[3]

HARNESS_DIR = (
    REPO_ROOT / "packages" / "openburrow-adapters" / "src" / "openburrow" / "adapters" / "harnesses"
)


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def make_lane(harness: str, **overrides: Any) -> Lane:
    return Lane(name="probe", harness=harness, session_id="sess_probe", **overrides)


class _Probe(GenericCliAdapter):
    """A harness that cannot promise structure but does emit JSON."""

    name = "probe"
    has_structured_mode = False
    parses_json_frames = True


class _FakePty:
    """Just enough PTY for the write path: a recorder and a liveness answer."""

    def __init__(self) -> None:
        self.written: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.written.append(data)

    def isalive(self) -> bool:
        return True


class _FakeStdin:
    def __init__(self) -> None:
        self.buffer = b""

    def write(self, data: bytes) -> None:
        self.buffer += data

    async def drain(self) -> None:
        return None


class _FakeProcess:
    def __init__(self) -> None:
        self.stdin = _FakeStdin()


# --- the flag that answered the wrong question -----------------------------


@pytest.mark.unit
class TestParsingIsNotADeclaration:
    """``has_structured_mode`` says "can you?"; ``parses_json_frames`` says "should you?".

    They were one flag. ``CustomScriptAdapter`` documents that its process "may
    emit JSONL on stdout, which is parsed like any structured harness" — but it
    cannot honestly claim structured output, because it knows nothing about the
    command it runs. Its declaration is therefore ``False``, the JSON branch
    never ran, and every frame was classified ``text``: a kind the bus does not
    broadcast. The lane was talking and the bus heard nothing.
    """

    def test_parsing_defaults_to_the_declaration(self) -> None:
        """An adapter that wants one flag still declares one flag."""
        lane = make_lane("probe")

        class _Declared(GenericCliAdapter):
            name = "declared"
            has_structured_mode = True

        class _Undeclared(GenericCliAdapter):
            name = "undeclared"
            has_structured_mode = False

        assert _Declared(Settings(), lane=lane)._parses_json is True
        assert _Undeclared(Settings(), lane=lane)._parses_json is False

    def test_parsing_can_differ_from_the_declaration(self) -> None:
        lane = make_lane("probe")
        adapter = _Probe(Settings(), lane=lane)

        assert adapter.has_structured_mode is False
        assert adapter._parses_json is True

    def test_an_undeclared_harness_still_gets_its_json_parsed(self) -> None:
        """The whole point: parsing happens even though nothing is advertised."""
        lane = make_lane("probe")
        outputs = _Probe(Settings(), lane=lane)._interpret(
            '{"type": "result", "text": "the retry loop is in"}'
        )

        assert len(outputs) == 1
        assert outputs[0].text == "the retry loop is in"
        assert outputs[0].structured is True

    def test_the_declaration_alone_still_silences_json(self) -> None:
        """Explicitly opting out of parsing is still possible."""
        lane = make_lane("probe")

        class _OptedOut(_Probe):
            name = "opted-out"
            parses_json_frames = False

        outputs = _OptedOut(Settings(), lane=lane)._interpret('{"type": "result", "text": "hi"}')

        assert [o.kind for o in outputs] == ["text"]
        assert outputs[0].structured is False

    async def test_line_assembly_follows_the_parsing_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """JSONL needs whole lines; a TUI must not be split.

        The assembler's mode was selected by ``has_structured_mode``, so an
        adapter whose two flags differ got the *wrong* unit — a TUI split into
        one output per line, or JSONL fragmented so that no frame ever parses.
        """
        seen: dict[str, bool] = {}

        async def fake_iter(self: Any, *, line_delimited: bool = False) -> Any:
            seen["line_delimited"] = line_delimited
            if False:  # pragma: no cover - makes this an async generator
                yield ""

        monkeypatch.setattr(HarnessAdapter, "_iter_pty_text", fake_iter)

        adapter = _Probe(Settings(), lane=make_lane("probe"))
        async for _ in adapter._read_pty():
            pass

        assert seen["line_delimited"] is True, "assembly ignored parses_json_frames"

    def test_structured_output_does_not_contradict_the_capability(self) -> None:
        """Two different claims, and they are allowed to differ.

        The capability says "this harness can be relied on to produce
        structure". The output flag says "this frame was structure". Collapsing
        them is the bug, so this pins the distinction rather than tidying it.
        """
        lane = make_lane("probe")
        adapter = _Probe(Settings(), lane=lane)

        outputs = adapter._interpret('{"type": "result", "text": "ok"}')

        assert adapter.capabilities.structured_output is False
        assert outputs[0].structured is True


@pytest.mark.unit
class TestUnmappedFramesAreVisible:
    """A frame that maps to nothing was dropped in silence.

    From the bus's side, a harness with an unrecognised schema is
    indistinguishable from a harness that said nothing — and that ambiguity has
    now cost four adapters.
    """

    def test_the_keys_are_named(self) -> None:
        lane = make_lane("probe")
        output = _Probe(Settings(), lane=lane)._interpret(fixture("unmapped_frame.json"))[0]

        assert output.data[generic_module._UNMAPPED_KEY] == ["meta", "payload", "type"]
        assert output.text == ""

    def test_it_is_still_not_broadcast(self) -> None:
        """Naming the frame does not mean publishing an empty message."""
        lane = make_lane("probe")
        adapter = _Probe(Settings(), lane=lane)
        output = adapter._interpret(fixture("unmapped_frame.json"))[0]

        assert adapter.translate_output(output) is None

    def test_a_mapped_frame_carries_no_diagnostic(self) -> None:
        lane = make_lane("probe")
        output = _Probe(Settings(), lane=lane)._interpret('{"type": "result", "text": "ok"}')[0]

        assert generic_module._UNMAPPED_KEY not in output.data

    def test_response_is_a_known_text_field(self) -> None:
        """``{"type": "result", "response": "..."}`` mapped to an empty body.

        Gemini's documented JSON schema uses ``response`` for the answer, and it
        was not among the candidate fields — so the answer existed in the frame
        and never reached the bus.
        """
        lane = make_lane("probe")
        output = _Probe(Settings(), lane=lane)._interpret('{"type": "result", "response": "done"}')[
            0
        ]

        assert output.text == "done"


# --- the bring-your-own adapter -------------------------------------------


@pytest.mark.unit
class TestCustomAdapter:
    """The weakest *declaration* must not mean the weakest implementation."""

    def test_it_inherits_the_shared_implementation(self) -> None:
        assert issubclass(CustomScriptAdapter, GenericCliAdapter)

    def test_it_no_longer_carries_its_own_interaction_code(self) -> None:
        """A class that does not inherit a fix does not get it.

        This adapter had a fourth copy of every PTY defect found in Stage 10:
        ``\\n`` written to a PTY, a blocking ``read`` inside an async generator,
        and an unconditional ``.decode()``.
        """
        for method in ("send_prompt", "read_output", "_interpret", "_read_pty", "_read_pipe"):
            assert method not in CustomScriptAdapter.__dict__, f"{method} was reimplemented"

    def test_it_does_not_instantiate_an_interpreter_to_borrow(self) -> None:
        """``read_output`` built a fresh ``GenericCliAdapter`` per read loop."""
        source = (HARNESS_DIR / "custom.py").read_text(encoding="utf-8")

        assert "GenericCliAdapter(self.settings" not in source

    def test_the_two_flags_differ(self) -> None:
        lane = make_lane("custom")
        adapter = CustomScriptAdapter(Settings(), lane=lane)

        assert adapter.has_structured_mode is False
        assert adapter.parses_json_frames is True

    def test_jsonl_from_a_custom_harness_reaches_the_bus(self) -> None:
        """The documented contract, which was false.

        The module promises a process "may emit JSONL on stdout, which is parsed
        like any structured harness". With the JSON branch gated on the
        declaration, that promise was never kept.
        """
        lane = make_lane("custom")
        adapter = CustomScriptAdapter(Settings(), lane=lane)
        outputs = adapter._interpret(fixture("jsonl_unadvertised.jsonl"))

        assert len(outputs) == 3
        assert outputs[1].text == "Added the retry loop and a regression test."
        assert outputs[1].structured is True

        broadcastable = [o for o in outputs if adapter.translate_output(o) is not None]
        assert broadcastable, "every frame from a JSONL custom harness was dropped"

    def test_usage_from_a_custom_harness_is_recorded(self) -> None:
        lane = make_lane("custom")
        CustomScriptAdapter(Settings(), lane=lane)._interpret(fixture("jsonl_unadvertised.jsonl"))

        assert lane.tokens_in == 120
        assert lane.tokens_out == 48

    async def test_a_pty_prompt_is_submitted_with_cr(self) -> None:
        """A PTY submits on CR. LF delivers text without ever pressing Enter."""
        lane = make_lane("custom")
        adapter = CustomScriptAdapter(Settings(), lane=lane)
        adapter._pty = _FakePty()

        await adapter.send_prompt(lane, "hello")

        assert adapter._pty.written == [b"hello\r"]

    async def test_a_pipe_prompt_is_submitted_with_lf(self) -> None:
        """No terminal, so nothing translates anything: a pipe wants LF."""
        lane = make_lane("custom")
        adapter = CustomScriptAdapter(Settings(), lane=lane)
        # A stand-in for asyncio.subprocess.Process; the adapter only reads
        # `.stdin.buffer`, which the fake provides. Held in a local so the
        # assertions read the fake rather than the declared `Process | None`.
        fake = _FakeProcess()
        adapter.process = fake  # type: ignore[assignment]

        await adapter.send_prompt(lane, "hello")

        assert fake.stdin.buffer == b"hello\n"

    def test_no_command_is_a_config_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPENBURROW_CUSTOM_ADAPTER_COMMAND", raising=False)
        lane = make_lane("custom")

        with pytest.raises(ConfigError):
            CustomScriptAdapter(Settings(), lane=lane).build_spawn_spec(lane)

    def test_a_pty_is_opt_in_for_a_custom_command(self) -> None:
        """An arbitrary command is not a terminal program by default."""
        lane = make_lane("custom", metadata={"command": ["python", "-c", "pass"]})
        spec = CustomScriptAdapter(Settings(), lane=lane).build_spawn_spec(lane)

        assert spec.command == ["python", "-c", "pass"]
        assert spec.use_pty is False

    def test_the_documented_list_form_is_used_verbatim(self) -> None:
        """The error hint tells users to write a ``command:`` list. It has to work.

        It did not. The list was stringified and shell-split, so the documented
        form produced ``['[python,', '-c,', 'pass]']`` — mangled tokens and a
        binary that does not exist. The branch that handled a list read
        ``command_argv``, a key that appears in no documentation, no example, and
        nowhere else in the repository.
        """
        lane = make_lane("custom", metadata={"command": ["uv", "run", "burrow", "smoke"]})
        spec = CustomScriptAdapter(Settings(), lane=lane).build_spawn_spec(lane)

        assert spec.command == ["uv", "run", "burrow", "smoke"]

    def test_the_string_form_is_shell_split(self) -> None:
        """A string is the other form a YAML lane template can express."""
        lane = make_lane("custom", metadata={"command": "python -m my_harness --json"})
        spec = CustomScriptAdapter(Settings(), lane=lane).build_spawn_spec(lane)

        assert spec.command == ["python", "-m", "my_harness", "--json"]

    def test_the_env_var_is_the_last_resort(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENBURROW_CUSTOM_ADAPTER_COMMAND", "my-harness --flag")
        lane = make_lane("custom")
        spec = CustomScriptAdapter(Settings(), lane=lane).build_spawn_spec(lane)

        assert spec.command == ["my-harness", "--flag"]

    def test_a_blank_command_is_a_config_error(self) -> None:
        """Present but empty is a different failure from absent, and both raise."""
        lane = make_lane("custom", metadata={"command": "   "})

        with pytest.raises(ConfigError):
            CustomScriptAdapter(Settings(), lane=lane).build_spawn_spec(lane)

    def test_a_lane_can_ask_for_a_pty(self) -> None:
        lane = make_lane("custom", metadata={"command": ["my-tui"], "use_pty": True})
        spec = CustomScriptAdapter(Settings(), lane=lane).build_spawn_spec(lane)

        assert spec.use_pty is True


# --- Aider -----------------------------------------------------------------


@pytest.mark.unit
class TestAiderCommitAnchor:
    """The commit detector matched any hex token anywhere in the chunk.

    Aider's output is mostly diffs, and a diff body is arbitrary source code, so
    the pattern found the first hex-looking token in whatever the lane had just
    written — and published it as the lane's git anchor to a checkpoint layer
    whose entire purpose is to *not* guess.
    """

    @staticmethod
    def adapter(lane: Lane) -> AiderAdapter:
        return AiderAdapter(Settings(), lane=lane)

    def test_a_lone_notice_is_broadcast(self) -> None:
        """The notice arrives in its own read, and ``text`` is not broadcast.

        Attaching the commit only to ``diff`` outputs meant the common case
        recorded nothing: detected correctly, then dropped at the last step.
        """
        lane = make_lane("aider")
        adapter = self.adapter(lane)
        outputs = adapter._interpret("Commit 4f2a9c1 fix: ignore comment-only lines\n")

        assert [o.kind for o in outputs] == ["status"]
        assert outputs[0].data["commit"] == "4f2a9c1"
        assert outputs[0].data["commit_message"] == "fix: ignore comment-only lines"
        assert adapter.translate_output(outputs[0]) is not None

    def test_a_notice_alongside_a_diff_attaches_to_the_diff(self) -> None:
        lane = make_lane("aider")
        outputs = self.adapter(lane)._interpret(fixture("aider_diff_and_commit.txt"))

        assert [o.kind for o in outputs] == ["diff"]
        assert outputs[0].data["commit"] == "4f2a9c1"
        assert any(a.name == "commit" for a in outputs[0].artifacts)

    def test_a_hex_token_in_a_diff_body_is_not_a_commit(self) -> None:
        """Three hex-looking tokens in this fixture; none is a revision."""
        lane = make_lane("aider")
        outputs = self.adapter(lane)._interpret(fixture("adversarial_hex_in_diff.txt"))

        assert [o.kind for o in outputs] == ["diff"]
        assert "commit" not in outputs[0].data
        assert not any(a.name == "commit" for a in outputs[0].artifacts)

    def test_the_old_pattern_would_have_matched_this_fixture(self) -> None:
        """Falsification: the fixture is only adversarial if the old bug fires.

        A regression fixture that the old code also passes proves nothing. This
        re-implements the replaced pattern and asserts it *would* have produced a
        commit here, so the test above is a real boundary rather than a tautology.
        """
        import re

        old_pattern = re.compile(r"\b([0-9a-f]{7,40})\b")
        match = old_pattern.search(fixture("adversarial_hex_in_diff.txt"))

        assert match is not None, "the fixture no longer exercises the old bug"
        assert match.group(1) in {"a1b2c3d4e5f6", "deadbeef0", "0123456789abcdef"}

    def test_a_commit_shaped_diff_line_is_not_matched(self) -> None:
        """Structurally impossible, and worth pinning.

        Inside a unified diff every line carries a ``+``, ``-``, or space
        prefix, so no diff line can *begin* with ``Commit``. The fixture adds a
        line that reads exactly like a notice; the anchor cannot see it.
        """
        lane = make_lane("aider")
        text = fixture("adversarial_hex_in_diff.txt")

        assert "+Commit 1234567" in text
        assert not any("commit" in o.data for o in self.adapter(lane)._interpret(text))

    def test_auto_commits_are_not_disabled(self) -> None:
        """The flag turned off the behaviour the module was built around.

        Aider's ``--auto-commits`` defaults to **True**; ``--no-auto-commits``
        therefore switched off exactly the commits whose SHAs this adapter
        exists to record. Nothing else in OpenBurrow produces a commit SHA for a
        lane, so the extraction had no input and the feature could not fire.
        """
        assert "--no-auto-commits" not in AiderAdapter.base_args
        assert "--yes-always" in AiderAdapter.base_args

    def test_the_dead_json_passthrough_is_gone(self) -> None:
        """It forwarded to ``super()`` and was marked "no JSON mode"."""
        assert "_from_json" not in AiderAdapter.__dict__


# --- Goose -----------------------------------------------------------------


@pytest.mark.unit
class TestGoose:
    """The awkward case, kept awkward on purpose — but it has to actually run."""

    @staticmethod
    def adapter(lane: Lane) -> GooseAdapter:
        return GooseAdapter(Settings(), lane=lane)

    def test_the_lane_spawns_an_interactive_session(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``goose run`` is one-shot and ignores stdin without ``-i -``.

        Goose's reference: ``run`` "Execute commands from an instruction file or
        stdin" where stdin means ``-i -``, and it "exits the session
        automatically once the task is complete". ``session`` is "Start or resume
        **interactive chat sessions**" — the only one of the two that fits a
        long-lived lane.
        """
        monkeypatch.delenv("GOOSE_BIN", raising=False)
        lane = make_lane("goose")
        spec = self.adapter(lane).build_spawn_spec(lane)

        assert spec.command == ["goose", "session"]

    def test_structured_output_is_declined(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Declined, not unavailable — the roadmap keeps this the fallback case.

        ``goose run --output-format json|stream-json`` is real, so this is a
        choice. Stage 12's design note asks for Goose to remain "the honest floor
        of what the protocol can do" and warns that special-casing it away "would
        make the protocol look cleaner than it is".
        """
        monkeypatch.delenv("GOOSE_BIN", raising=False)
        lane = make_lane("goose")
        adapter = self.adapter(lane)

        assert adapter.has_structured_mode is False
        assert adapter._parses_json is False
        assert "--output-format" not in adapter.build_spawn_spec(lane).command

    def test_the_fallback_parser_handles_a_session_chunk(self) -> None:
        """Goose is the adapter that exercises this path most, so exercise it."""
        lane = make_lane("goose")
        outputs = self.adapter(lane)._interpret(fixture("goose_tui_chunk.txt"))

        assert [o.kind for o in outputs] == ["plan"]
        assert len(outputs[0].data["steps"]) == 3
        assert outputs[0].structured is False

    def test_a_session_chunk_is_not_parsed_as_json(self) -> None:
        """No structured claim, so a JSON-looking line stays prose."""
        lane = make_lane("goose")
        outputs = self.adapter(lane)._interpret('{"type": "result", "text": "not a frame"}')

        assert [o.kind for o in outputs] == ["text"]
        assert outputs[0].structured is False


# --- Gemini ----------------------------------------------------------------


@pytest.mark.unit
class TestGemini:
    """The declaration was false, and the reader was worse for it."""

    @staticmethod
    def adapter(lane: Lane) -> GeminiAdapter:
        return GeminiAdapter(Settings(), lane=lane)

    def test_structured_output_is_not_claimed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Headless mode never engages under a PTY, so the flag was a lie.

        Gemini's reference: the positional prompt "Defaults to interactive mode
        **in a TTY**", ``-p`` "Forces non-interactive mode", and headless mode is
        triggered by a non-TTY environment **or** ``-p``. ``--output-format`` is
        a headless option. OpenBurrow spawns under a PTY and never passes ``-p``,
        so what runs is the TUI — and the Agent Card advertised JSON.
        """
        monkeypatch.delenv("GEMINI_CLI_BIN", raising=False)
        lane = make_lane("gemini")
        adapter = self.adapter(lane)

        assert adapter.has_structured_mode is False
        assert adapter.capabilities.structured_output is False
        assert adapter._parses_json is False

    def test_no_output_format_flag_is_passed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An inert flag plus a false declaration is worse than neither."""
        monkeypatch.delenv("GEMINI_CLI_BIN", raising=False)
        lane = make_lane("gemini")
        spec = self.adapter(lane).build_spawn_spec(lane)

        assert "--output-format" not in spec.command
        assert spec.command == ["gemini"]

    def test_the_fallback_parser_handles_an_interactive_chunk(self) -> None:
        lane = make_lane("gemini")
        outputs = self.adapter(lane)._interpret(fixture("gemini_tui_chunk.txt"))

        assert [o.kind for o in outputs] == ["text"]
        assert outputs[0].structured is False

    def test_a_json_chunk_is_not_claimed_as_structured(self) -> None:
        """Consistency: the declaration and the reader now agree."""
        lane = make_lane("gemini")
        outputs = self.adapter(lane)._interpret('{"response": "done", "stats": {}}')

        assert outputs[0].structured is False

    def test_the_remaining_work_is_recorded(self) -> None:
        """The dependency is a lane-lifecycle feature, and it is written down.

        Making this work needs ``-p <prompt>``, which is one-shot. That is the
        trade-off ``prompt_as_arg`` already documents as the reason it is inert,
        and it belongs in a stage rather than in a docstring nobody reads.
        """
        doc = gemini_module.__doc__ or ""

        assert "one-shot" in doc
        assert "response" in doc and "stats" in doc


# --- the daemon's copy of a decision it does not own -----------------------


@pytest.mark.unit
class TestBroadcastDecisionHasOneOwner:
    """``translate_output`` is the documented hook and was called by nothing.

    The daemon kept a private set of broadcastable kinds instead, and the two had
    drifted: the adapter broadcasts ``status``, the daemon did not, so a lane's
    state reports were classified correctly and then dropped at the last step.
    """

    @staticmethod
    def source() -> str:
        return (
            REPO_ROOT
            / "packages"
            / "openburrow-daemon"
            / "src"
            / "openburrow"
            / "daemon"
            / "sessions.py"
        ).read_text(encoding="utf-8")

    def test_the_daemon_consults_the_adapter(self) -> None:
        source = self.source()

        assert "adapter.translate_output(output)" in source
        assert 'output.kind in {"plan", "diff", "result", "error"}' not in source

    def test_a_status_frame_is_admitted(self) -> None:
        """The kind the private set was dropping."""
        from openburrow.adapters.base import HarnessOutput

        lane = make_lane("aider")
        adapter = AiderAdapter(Settings(), lane=lane)

        assert adapter.translate_output(HarnessOutput(kind="status", text="Commit 4f2a9c1 x"))

    def test_an_empty_frame_is_still_refused(self) -> None:
        """Widening the set must not mean broadcasting nothing."""
        from openburrow.adapters.base import HarnessOutput

        lane = make_lane("aider")
        adapter = AiderAdapter(Settings(), lane=lane)

        assert adapter.translate_output(HarnessOutput(kind="status", text="")) is None


# --- Stage 12's stated verification ----------------------------------------


@pytest.mark.unit
class TestRegistryRobustness:
    """Stage 12 is verified by the registry surviving what is *not* installed."""

    def test_all_four_adapters_are_registered(self) -> None:
        names = build_registry(Settings()).names()

        for harness in ("gemini", "aider", "goose", "custom"):
            assert harness in names

    def test_availability_is_reported_without_any_harness_installed(self) -> None:
        """A missing binary is a ``False``, not an exception."""
        availability = build_registry(Settings()).available()

        assert set(availability) >= {"gemini", "aider", "goose", "custom"}
        assert all(isinstance(value, bool) for value in availability.values())

    def test_listing_works_without_any_harness_installed(self) -> None:
        rows = build_registry(Settings()).describe_all()

        listed = {row["name"] for row in rows}
        assert {"gemini", "aider", "goose", "custom"} <= listed
        assert all("installed" in row for row in rows)

    def test_a_missing_optional_dependency_disables_one_adapter_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The registry tolerates the failure; it must not propagate it.

        One adapter whose optional dependency is absent should be missing from
        the list. The other eight should still be there, because "the registry
        broke" is a much worse failure than "one harness is unavailable".
        """
        real_import = registry_module._import_target

        def flaky(target: str) -> Any:
            if "goose" in target:
                raise ImportError("simulated missing optional dependency")
            return real_import(target)

        monkeypatch.setattr(registry_module, "_import_target", flaky)
        registry = AdapterRegistry(Settings())
        registry._load_builtins()

        names = registry.names()

        assert "goose" not in names
        assert {"gemini", "aider", "custom", "claude-code", "opencode"} <= set(names)

    def test_aliases_still_resolve_to_the_stage12_adapters(self) -> None:
        registry = build_registry(Settings())

        assert registry.resolve("antigravity") is GeminiAdapter
        assert registry.resolve("script") is CustomScriptAdapter

    def test_every_registered_adapter_declares_capabilities(self) -> None:
        """A capability declaration that raises breaks the Agent Card."""
        for row in build_registry(Settings()).describe_all():
            assert row["capabilities"], f"{row['name']} declared nothing"


@pytest.mark.unit
class TestFixtureCorpusIsComplete:
    def test_every_stage12_fixture_exists(self) -> None:
        expected = {
            "goose_tui_chunk.txt",
            "gemini_tui_chunk.txt",
            "aider_diff_and_commit.txt",
            "adversarial_hex_in_diff.txt",
            "jsonl_unadvertised.jsonl",
            "unmapped_frame.json",
        }

        missing = {name for name in expected if not (FIXTURES / name).exists()}
        assert not missing, f"missing fixtures: {sorted(missing)}"

    def test_the_jsonl_fixture_is_valid_jsonl(self) -> None:
        """A malformed fixture would make the parsing tests vacuous."""
        lines = fixture("jsonl_unadvertised.jsonl").splitlines()

        assert len(lines) == 3
        for line in lines:
            assert isinstance(json.loads(line), dict)
