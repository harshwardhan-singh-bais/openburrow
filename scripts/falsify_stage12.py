"""Stage 12 falsification pass.

For every new test row, re-implement the behaviour the fix replaced and confirm
the row *fails*. A test that passes against both the old and the new code is not
a boundary, it is decoration.

Every check function below returns True when the corresponding test would pass.
The script asserts each returns **False** under the reverted behaviour. A row
that still returns True is reported as VACUOUS — and the expectation, learned the
hard way in Stages 10 and 11, is that a VACUOUS row usually means this script's
model of the old code is wrong rather than that the test is bad.

A third verdict exists because the first draft of this script got one row wrong:
**WEAK**. A revert that *raises* has not tested the assertion at all, and
counting that as a pass is how a falsification pass becomes theatre. The row that
exposed it swapped only the Aider regex; the old pattern has one capture group,
so the new code raised ``IndexError`` reading ``group(2)`` and the check "failed"
without ever reaching the anchor. Reverting the whole old ``_interpret`` fixed
it. Treat WEAK as a failure of this script, not of the test.

Convention: one file per stage, named ``falsify_stageNN.py``, kept as a snapshot
of the old behaviour at the time of the fix. It is deliberately *not* a permanent
gate — when the code moves on, the reverted behaviour it reconstructs stops being
the behaviour that was replaced, and a stale falsification is worse than none.
Re-run it when touching the stage; write a new one for a new stage.
"""

from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "packages"))

from openburrow.adapters.harnesses import (  # noqa: E402
    aider as aider_module,
    generic as generic_module,
)
from openburrow.adapters.harnesses.aider import AiderAdapter  # noqa: E402
from openburrow.adapters.harnesses.custom import CustomScriptAdapter  # noqa: E402
from openburrow.adapters.harnesses.gemini import GeminiAdapter  # noqa: E402
from openburrow.adapters.harnesses.generic import GenericCliAdapter  # noqa: E402
from openburrow.adapters.harnesses.goose import GooseAdapter  # noqa: E402
from openburrow.core.config.settings import Settings  # noqa: E402
from openburrow.core.models import Lane  # noqa: E402

FIXTURES = REPO / "packages" / "openburrow-adapters" / "tests" / "fixtures"
SESSIONS = REPO / "packages" / "openburrow-daemon" / "src" / "openburrow" / "daemon" / "sessions.py"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def lane_for(harness: str, **overrides: object) -> Lane:
    return Lane(name="probe", harness=harness, session_id="sess_probe", **overrides)


class _Probe(GenericCliAdapter):
    name = "probe"
    has_structured_mode = False
    parses_json_frames = True


class _FakePty:
    def __init__(self) -> None:
        self.written: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.written.append(data)

    def isalive(self) -> bool:
        return True


ROWS: list[tuple[str, str]] = []


def row(name: str, note: str):
    def wrap(fn):
        ROWS.append((name, note))
        return fn

    return wrap


# --- 1. the parsing gate ----------------------------------------------------


@row(
    "TestParsingIsNotADeclaration::test_an_undeclared_harness_still_gets_its_json_parsed",
    "gate `_interpret` on `has_structured_mode` again",
)
def check_1() -> bool:
    original = GenericCliAdapter._parses_json
    GenericCliAdapter._parses_json = property(lambda self: self.has_structured_mode)  # type: ignore[assignment]
    try:
        outputs = _Probe(Settings(), lane=lane_for("probe"))._interpret(
            '{"type": "result", "text": "the retry loop is in"}'
        )
        return len(outputs) == 1 and outputs[0].text == "the retry loop is in"
    finally:
        GenericCliAdapter._parses_json = original  # type: ignore[assignment]


# --- 2. line assembly follows the parsing flag ------------------------------


@row(
    "TestParsingIsNotADeclaration::test_line_assembly_follows_the_parsing_flag",
    "select the assembler mode from `has_structured_mode`",
)
def check_2() -> bool:
    import asyncio

    seen: dict[str, bool] = {}

    async def fake_iter(*_args: object, line_delimited: bool = False):
        seen["line_delimited"] = line_delimited
        if False:
            yield ""

    original_iter = GenericCliAdapter._iter_pty_text
    original_prop = GenericCliAdapter._parses_json
    GenericCliAdapter._iter_pty_text = fake_iter  # type: ignore[assignment]
    # The old code read has_structured_mode at the call site.
    GenericCliAdapter._parses_json = property(lambda self: self.has_structured_mode)  # type: ignore[assignment]
    try:
        adapter = _Probe(Settings(), lane=lane_for("probe"))

        async def drive() -> None:
            async for _ in adapter._read_pty():
                pass

        asyncio.run(drive())
        return seen.get("line_delimited") is True
    finally:
        GenericCliAdapter._iter_pty_text = original_iter  # type: ignore[assignment]
        GenericCliAdapter._parses_json = original_prop  # type: ignore[assignment]


# --- 3. the `response` field ------------------------------------------------


@row(
    "TestUnmappedFramesAreVisible::test_response_is_a_known_text_field",
    "drop `response` from the candidate text fields",
)
def check_3() -> bool:
    original = generic_module._TEXT_KEYS
    generic_module._TEXT_KEYS = tuple(k for k in original if k != "response")
    try:
        outputs = _Probe(Settings(), lane=lane_for("probe"))._interpret(
            '{"type": "result", "response": "done"}'
        )
        return outputs[0].text == "done"
    finally:
        generic_module._TEXT_KEYS = original


# --- 4. the unmapped diagnostic ---------------------------------------------


@row(
    "TestUnmappedFramesAreVisible::test_the_keys_are_named",
    "emit the frame without the diagnostic key",
)
def check_4() -> bool:
    original = GenericCliAdapter._from_json

    def without_diagnostic(self, payload):
        output = original(self, payload)
        output.data = {k: v for k, v in output.data.items() if k != generic_module._UNMAPPED_KEY}
        return output

    GenericCliAdapter._from_json = without_diagnostic  # type: ignore[assignment]
    try:
        output = _Probe(Settings(), lane=lane_for("probe"))._interpret(
            fixture("unmapped_frame.json")
        )[0]
        return output.data.get(generic_module._UNMAPPED_KEY) == ["meta", "payload", "type"]
    finally:
        GenericCliAdapter._from_json = original  # type: ignore[assignment]


# --- 5. the Aider commit anchor ---------------------------------------------


@row(
    "TestAiderCommitAnchor::test_a_hex_token_in_a_diff_body_is_not_a_commit",
    "restore the unanchored hex pattern and the diff-only attach",
)
def check_5() -> bool:
    """Revert to the *whole* old implementation, not just the pattern.

    Swapping only the regex makes this row pass for the wrong reason: the old
    pattern has a single group, so the new code raises ``IndexError`` reading
    ``group(2)`` and the check "fails" without ever testing the anchor. A
    falsification that succeeds by raising is not a falsification, so the old
    ``_interpret`` is reinstated too.
    """
    old_pattern = re.compile(r"\b([0-9a-f]{7,40})\b")
    original = AiderAdapter._interpret

    def old_interpret(self, text):
        outputs = GenericCliAdapter._interpret(self, text)
        for output in outputs:
            if output.kind == "diff":
                match = old_pattern.search(text)
                if match:
                    output.data = {**output.data, "commit": match.group(1)}
        return outputs

    AiderAdapter._interpret = old_interpret  # type: ignore[assignment]
    try:
        outputs = AiderAdapter(Settings(), lane=lane_for("aider"))._interpret(
            fixture("adversarial_hex_in_diff.txt")
        )
        return "commit" not in outputs[0].data
    finally:
        AiderAdapter._interpret = original  # type: ignore[assignment]


# --- 6. the lone commit notice ----------------------------------------------


@row(
    "TestAiderCommitAnchor::test_a_lone_notice_is_broadcast",
    "attach the commit to `diff` outputs only",
)
def check_6() -> bool:
    original = AiderAdapter._interpret

    def diff_only(self, text):
        outputs = GenericCliAdapter._interpret(self, text)
        match = aider_module._COMMIT_LINE.search(text)
        if match:
            for output in outputs:
                if output.kind == "diff":
                    output.data = {**output.data, "commit": match.group(1)}
        return outputs

    AiderAdapter._interpret = diff_only  # type: ignore[assignment]
    try:
        outputs = AiderAdapter(Settings(), lane=lane_for("aider"))._interpret(
            "Commit 4f2a9c1 fix: ignore comment-only lines\n"
        )
        return [o.kind for o in outputs] == ["status"] and outputs[0].data.get(
            "commit"
        ) == "4f2a9c1"
    finally:
        AiderAdapter._interpret = original  # type: ignore[assignment]


# --- 7. the Aider spawn flag ------------------------------------------------


@row(
    "TestAiderCommitAnchor::test_auto_commits_are_not_disabled",
    "put `--no-auto-commits` back",
)
def check_7() -> bool:
    original = AiderAdapter.base_args
    AiderAdapter.base_args = ("--no-auto-commits", "--yes-always")
    try:
        return "--no-auto-commits" not in AiderAdapter.base_args
    finally:
        AiderAdapter.base_args = original


# --- 8. the custom adapter's PTY write --------------------------------------


@row(
    "TestCustomAdapter::test_a_pty_prompt_is_submitted_with_cr",
    "restore the adapter's own `send_prompt` writing LF",
)
def check_8() -> bool:
    import asyncio

    original = CustomScriptAdapter.send_prompt

    async def old_send_prompt(self, _lane, prompt):
        payload = (prompt + "\n").encode("utf-8")
        if self._pty is not None:
            self._pty.write(payload)
            return
        raise RuntimeError("not running")

    CustomScriptAdapter.send_prompt = old_send_prompt  # type: ignore[assignment]
    try:
        adapter = CustomScriptAdapter(Settings(), lane=lane_for("custom"))
        adapter._pty = _FakePty()
        asyncio.run(adapter.send_prompt(adapter.lane, "hello"))
        return adapter._pty.written == [b"hello\r"]
    finally:
        CustomScriptAdapter.send_prompt = original  # type: ignore[assignment]


# --- 9. the custom adapter's documented list form ---------------------------


@row(
    "TestCustomAdapter::test_the_documented_list_form_is_used_verbatim",
    "stringify and shell-split whatever `command` holds",
)
def check_9() -> bool:
    lane = lane_for("custom", metadata={"command": ["uv", "run", "burrow", "smoke"]})
    raw = lane.metadata.get("command")
    argv = shlex.split(str(raw))
    return argv == ["uv", "run", "burrow", "smoke"]


# --- 10. the daemon's broadcast decision ------------------------------------


@row(
    "TestBroadcastDecisionHasOneOwner::test_the_daemon_consults_the_adapter",
    "restore the daemon's private kind set",
)
def check_10() -> bool:
    source = SESSIONS.read_text(encoding="utf-8")
    reverted = source.replace(
        "if adapter.translate_output(output) is None:",
        'if output.kind in {"plan", "diff", "result", "error"}:',
    )
    return "adapter.translate_output(output)" in reverted


# --- 11. the Gemini declaration --------------------------------------------


@row(
    "TestGemini::test_structured_output_is_not_claimed",
    "restore `has_structured_mode = True`",
)
def check_11() -> bool:
    original = GeminiAdapter.has_structured_mode
    GeminiAdapter.has_structured_mode = True
    try:
        adapter = GeminiAdapter(Settings(), lane=lane_for("gemini"))
        return adapter.capabilities.structured_output is False
    finally:
        GeminiAdapter.has_structured_mode = original


# --- 12. the Goose subcommand ----------------------------------------------


@row(
    "TestGoose::test_the_lane_spawns_an_interactive_session",
    "restore `base_args = ('run',)`",
)
def check_12() -> bool:
    import os

    original = GooseAdapter.base_args
    GooseAdapter.base_args = ("run",)
    saved = os.environ.pop("GOOSE_BIN", None)
    try:
        lane = lane_for("goose")
        return GooseAdapter(Settings(), lane=lane).build_spawn_spec(lane).command == [
            "goose",
            "session",
        ]
    finally:
        GooseAdapter.base_args = original
        if saved is not None:
            os.environ["GOOSE_BIN"] = saved


# --- 13. the status frame the private set dropped ---------------------------


@row(
    "TestBroadcastDecisionHasOneOwner::test_a_status_frame_is_admitted",
    "restore the daemon's set as the only filter",
)
def check_13() -> bool:
    allowed = {"plan", "diff", "result", "error"}
    return "status" in allowed


CHECKS = [
    check_1,
    check_2,
    check_3,
    check_4,
    check_5,
    check_6,
    check_7,
    check_8,
    check_9,
    check_10,
    check_11,
    check_12,
    check_13,
]


def main() -> int:
    assert len(CHECKS) == len(ROWS), "row metadata is out of step with the checks"

    vacuous: list[str] = []
    weak: list[str] = []
    for (name, note), check in zip(ROWS, CHECKS, strict=True):
        raised = ""
        try:
            still_passes = check()
        except Exception as exc:
            # A revert that raises has not tested the assertion — the check
            # "failed" for a reason unrelated to what the test claims to cover.
            # Reported separately, because treating it as a pass is how a
            # falsification pass becomes theatre.
            still_passes = False
            raised = f"{type(exc).__name__}: {exc}"

        if raised:
            weak.append(name)
            verdict = "WEAK"
        elif still_passes:
            vacuous.append(name)
            verdict = "VACUOUS"
        else:
            verdict = "ok"

        print(f"  [{verdict:>7}] {name}")
        print(f"            reverted: {note}")
        if raised:
            print(f"            the revert raised instead of asserting: {raised}")

    print()
    if vacuous:
        print(f"{len(vacuous)} VACUOUS row(s) — the test does not discriminate:")
        for name in vacuous:
            print(f"  - {name}")
    if weak:
        print(f"{len(weak)} WEAK row(s) — the revert raised, so nothing was proven:")
        for name in weak:
            print(f"  - {name}")
    if vacuous or weak:
        return 1

    print(f"all {len(CHECKS)} rows discriminate: every test fails on the old code")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
