"""End-to-end checks that spawn the harness binary actually installed here.

Marked ``e2e``, so they are skipped unless ``OPENBURROW_E2E=1`` — the hook lives
in ``packages/conftest.py``. Nothing else in this suite spawns a real harness; the
``mock`` adapter exists so that the rest of the tests do not have to.

Why the file exists
-------------------

Every other adapter test hands the adapter a fixture shaped like the harness's
output, and a fixture can only prove that the adapter agrees with the fixture.
That is the class of test that missed ``ClaudeCodeAdapter``'s missing
``--verbose``: every fixture agreed with the declaration, and the installed
binary rejected the argument outright —

    Error: When using --print, --output-format=stream-json requires --verbose

— so the lane would have started, printed that one line, and published nothing.
The adapter's own docstring names that failure mode as the reason its
``_from_json`` override exists; this route went around the override entirely.

What is covered, and what is not
--------------------------------

Covered: the argv the adapter builds is one the installed binary accepts, and the
frames that binary really emits are classified by the adapter's mapper and become
bus-visible messages.

Not covered: the adapter's ``start()`` / ``read_output()`` lane path. That is a
documented open item rather than a convenience. ``--output-format stream-json``
puts Claude Code into *print* mode, which reads stdin and runs until **EOF**,
while ``GenericCliAdapter.send_prompt`` writes a prompt and deliberately never
closes stdin — a lane is long-lived and writes prompts in over the session. So
print mode cannot terminate through the lane path. This module drives the command
as a subprocess, where ``input=`` supplies the EOF. A version that reached into
the adapter and closed the pipe itself would be demonstrating a lane lifecycle
this project does not yet have, which is the opposite of a test.

Credentials are deliberately not a precondition. These checks pass whether or not
the harness is logged in: what is verified is that the argv is accepted and that
whatever the binary says is classified and reaches the bus. Requiring a logged-in
harness would skip them on precisely the machines where the adapter is most
likely to be wrong.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from openburrow.adapters.harnesses.claude_code import ClaudeCodeAdapter
from openburrow.core.config.settings import Settings
from openburrow.core.models import Lane

#: Small enough that a logged-in harness spends nothing meaningful on it, and
#: still enough for a harness with no credentials to reach its own error path.
PROMPT = "Reply with the single word: ok"

#: Well under the suite's 120s per-test timeout, so a harness that hangs is
#: reported as a failure with its output attached rather than as a bare timeout.
SPAWN_TIMEOUT_S = 90


def _lane(harness: str) -> Lane:
    return Lane(name="e2e", harness=harness, session_id="sess_e2e")


def _spawn(adapter: ClaudeCodeAdapter) -> subprocess.CompletedProcess[str]:
    """Run the real binary with the argv the adapter builds.

    ``input=`` is what closes stdin, and closing it is the whole reason this is a
    subprocess rather than the adapter's lane path — see the module docstring.
    """
    spec = adapter.build_spawn_spec(_lane(adapter.name))
    if shutil.which(spec.command[0]) is None:
        pytest.skip(f"{spec.command[0]} is not installed on this machine")
    return subprocess.run(  # noqa: S603 - argv is built by the adapter, never a shell string
        spec.command,
        input=PROMPT,
        capture_output=True,
        text=True,
        timeout=SPAWN_TIMEOUT_S,
        check=False,
    )


def _json_lines(done: subprocess.CompletedProcess[str]) -> list[str]:
    return [line for line in done.stdout.splitlines() if line.startswith("{")]


def _frames(done: subprocess.CompletedProcess[str]) -> list[dict[str, object]]:
    return [json.loads(line) for line in _json_lines(done)]


@pytest.mark.e2e
class TestClaudeCodeAgainstTheInstalledBinary:
    """Claude Code is the one installed harness with a structured mode."""

    def test_the_argv_the_adapter_builds_is_accepted(self) -> None:
        """The regression test for the missing ``--verbose``.

        Without that flag the binary prints its complaint and exits, so this fails
        on the adapter's own declaration rather than on anything subtle.
        """
        adapter = ClaudeCodeAdapter(Settings())
        done = _spawn(adapter)
        combined = done.stdout + done.stderr

        assert "requires --verbose" not in combined, (
            "the installed binary rejected the adapter's own structured arguments: "
            f"{adapter.build_spawn_spec(_lane(adapter.name)).command!r}\n{combined[:600]}"
        )
        assert done.stdout.strip(), f"the harness produced no stdout:\n{combined[:600]}"

    def test_the_real_frames_are_classified_by_the_adapter(self) -> None:
        adapter = ClaudeCodeAdapter(Settings())
        done = _spawn(adapter)

        frames = _frames(done)
        assert frames, f"no JSON frames in the harness output:\n{done.stdout[:600]}"
        types = {str(frame.get("type")) for frame in frames}
        assert {"system", "result"} <= types, (
            f"expected the documented init and terminal frames, saw {sorted(types)}"
        )

        outputs = [output for line in _json_lines(done) for output in adapter._interpret(line)]
        kinds = {output.kind for output in outputs}
        assert "status" in kinds, f"the init frame was not classified as status: {sorted(kinds)}"
        assert any(output.terminal for output in outputs), (
            "no output was marked terminal, so the frame that ends a run was not recognised"
        )

    def test_a_harness_that_cannot_authenticate_is_reported_not_silent(self) -> None:
        """A lane that speaks and is not heard is the failure this guards against.

        Claude Code reports a missing login as an ``is_error`` result frame. Had
        the adapter mapped that to a kind the bus does not broadcast, the lane
        would look idle while it was in fact failing, and an operator would have
        no signal at all.
        """
        adapter = ClaudeCodeAdapter(Settings())
        done = _spawn(adapter)
        frames = _frames(done)

        outputs = [output for line in _json_lines(done) for output in adapter._interpret(line)]
        assert outputs, f"the adapter produced no output from real frames:\n{done.stdout[:600]}"

        if any(frame.get("is_error") for frame in frames):
            assert any(output.kind == "error" for output in outputs), (
                "the harness reported a failure and the adapter classified none of its "
                f"output as an error: {[output.kind for output in outputs]}"
            )

        published = [adapter.translate_output(output) for output in outputs]
        assert any(message is not None for message in published), (
            "nothing from this run would reach the bus, so the lane spoke and the bus "
            f"heard nothing: {[output.kind for output in outputs]}"
        )
