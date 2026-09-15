"""Mock adapter — the reference harness for tests and CI.

A deterministic, scriptable harness that speaks the full adapter protocol
without needing a vendor CLI, an API key, or a network. Every integration test
in the project that is not an explicitly-marked ``e2e`` test uses this.

It is not a toy. It supports the behaviours the test suite needs to exercise:

* emitting structured output (plan, diff, tool-call, result)
* emitting deliberately malformed output, to test the fallback parser
* hanging, to test timeouts
* crashing on demand, to test the restart policy
* emitting a rate-limit message, to test involuntary handoff
* accepting injected messages and recording that it did

Making the mock scriptable rather than random is what makes the chaos tests
(item 249-253) reproducible: a seeded mock produces the same failure every run.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path

from openburrow.a2a.card import HarnessCapabilities, SkillSpec
from openburrow.adapters.base import HarnessAdapter, HarnessOutput, SpawnSpec
from openburrow.core.config.settings import Settings
from openburrow.core.logging import get_logger
from openburrow.core.models import Lane, TaskArtifact

log = get_logger(__name__)


class MockAdapter(HarnessAdapter):
    """A scriptable harness with no external dependencies."""

    name = "mock"
    description = "Deterministic scriptable harness for tests and CI. No external dependencies."
    binary = "python"
    binary_env = "OPENBURROW_MOCK_BIN"
    docs_url = "https://github.com/openburrow/openburrow/blob/main/docs/adapters/README.md"

    def __init__(self, settings: Settings, *, lane: Lane | None = None) -> None:
        super().__init__(settings, lane=lane)
        self.injected: list[str] = []
        self.prompts: list[str] = []
        #: Scripted output lines. Empty => the default sensible script.
        self.script: list[str] = []
        self._queue: asyncio.Queue[HarnessOutput] = asyncio.Queue()
        self._started = False

    # --- declaration -------------------------------------------------------
    @property
    def capabilities(self) -> HarnessCapabilities:
        return HarnessCapabilities(
            structured_output=True,
            streaming=True,
            resumable=True,
            mcp_tools=False,
            supports_interrupt=True,
            native_a2a=False,
        )

    def skills(self) -> list[SkillSpec]:
        return [
            SkillSpec(
                skill_id="mock-echo",
                name="Echo",
                description="Echoes back whatever it is asked, for wiring tests.",
                tags=["test"],
            )
        ]

    def build_spawn_spec(self, lane: Lane) -> SpawnSpec:
        """The mock does not really spawn a harness — but it still resolves a spec.

        Returning a real-looking spec matters: the policy gate and
        ``burrow doctor`` both consume this, and a mock that bypassed them would
        let tests pass while production paths stayed broken.
        """
        import sys

        worktree = Path(lane.worktree_path) if lane.worktree_path else Path.cwd()
        return SpawnSpec(
            command=[sys.executable, "-c", "import time; time.sleep(3600)"],
            cwd=worktree,
            env=self.base_env(lane),
            use_pty=False,
            stdin_pipe=True,
        )

    async def start(self, lane: Lane, *, spec: SpawnSpec | None = None) -> None:
        """Mark the lane started without spawning anything.

        ``spec`` is accepted and ignored: this adapter has no process, so there is
        nothing for a caller's prebuilt plan to describe. It is in the signature
        because the base class declares it, and an override that silently drops a
        keyword is an ``TypeError`` the first time the daemon passes one.
        """
        self.lane = lane
        self._started = True
        lane.pid = os.getpid()
        lane.command = ["mock"]
        from openburrow.core.models.base import now
        from openburrow.core.models.enums import LaneStatus

        lane.started_at = lane.started_at or now()
        lane.status = LaneStatus.IDLE
        log.debug("mock.started", lane_id=lane.id)

    async def stop(self, *, timeout: float = 10.0, force: bool = False) -> None:
        self._started = False
        if self.lane is not None:
            from openburrow.core.models.enums import LaneStatus

            self.lane.status = LaneStatus.STOPPED
        await self._queue.put(HarnessOutput(kind="status", text="stopped", terminal=True))

    @property
    def is_running(self) -> bool:
        return self._started

    async def status(self) -> dict:
        base = await super().status()
        base.update({"running": self._started, "injected": len(self.injected)})
        return base

    # --- interaction -------------------------------------------------------
    async def send_prompt(self, lane: Lane, prompt: str) -> None:
        self.prompts.append(prompt)
        for chunk in self._script_for(prompt):
            await self._queue.put(chunk)

    def _script_for(self, prompt: str) -> list[HarnessOutput]:
        """Produce output for a prompt.

        The default script exercises the full translation path: a plan, a
        tool-call, a diff, and a terminal result. That is what makes an
        integration test using the mock actually cover the translator rather
        than skip it.
        """
        if self.script:
            return [self._parse_script_line(line) for line in self.script]

        lowered = prompt.lower()
        if "hang" in lowered:
            return [HarnessOutput(kind="status", text="working", structured=True)]
        if "crash" in lowered:
            self._started = False
            return [HarnessOutput(kind="error", text="simulated crash", terminal=True)]
        if "rate limit" in lowered or "ratelimit" in lowered:
            return [
                HarnessOutput(
                    kind="error",
                    text="Error 429: rate limit exceeded for this model",
                    terminal=True,
                )
            ]
        if "malformed" in lowered:
            return [
                HarnessOutput(
                    kind="text",
                    text="{not valid json, and not a diff either",
                    structured=False,
                    terminal=True,
                )
            ]

        return [
            HarnessOutput(
                kind="plan",
                text="1. Inspect the module\n2. Apply the change\n3. Run the tests",
                structured=True,
                data={"steps": ["inspect", "apply", "test"]},
            ),
            HarnessOutput(
                kind="tool-call",
                text="read_file src/example.py",
                structured=True,
                data={"tool": "read_file", "args": {"path": "src/example.py"}},
            ),
            HarnessOutput(
                kind="diff",
                text=(
                    "--- a/src/example.py\n"
                    "+++ b/src/example.py\n"
                    "@@ -1,3 +1,3 @@\n"
                    "-def greet():\n"
                    "+def greet(name: str) -> str:\n"
                    "     return 'hi'\n"
                ),
                structured=True,
                artifacts=[
                    TaskArtifact.diff(
                        "@@ -1,3 +1,3 @@\n-def greet():\n+def greet(name: str) -> str:\n"
                    )
                ],
            ),
            HarnessOutput(
                kind="result",
                text="Applied the change and the tests pass.",
                structured=True,
                terminal=True,
                artifacts=[
                    TaskArtifact.test_result("3 passed in 0.42s", name="pytest"),
                ],
            ),
        ]

    @staticmethod
    def _parse_script_line(line: str) -> HarnessOutput:
        """``"kind:text"`` -> HarnessOutput. Unknown kinds degrade to text."""
        kind, _, text = line.partition(":")
        if not text:
            return HarnessOutput(kind="text", text=line)
        return HarnessOutput(
            kind=kind.strip() or "text",
            text=text.strip(),
            structured=kind.strip() in {"plan", "diff", "tool-call", "result"},
            terminal=kind.strip() in {"result", "error"},
        )

    async def read_output(self) -> AsyncIterator[HarnessOutput]:
        while self._started:
            try:
                output = await asyncio.wait_for(self._queue.get(), timeout=0.2)
            except TimeoutError:
                continue
            self.buffer_output(output)
            yield output
            if output.terminal:
                continue

    async def inject_message(self, message, task=None) -> bool:
        rendered = self.render_injection(message)
        self.injected.append(rendered)
        self.prompts.append(rendered)
        await self._queue.put(
            HarnessOutput(
                kind="status",
                text=f"received {message.intent} from {message.sender_lane}",
                structured=True,
            )
        )
        return True

    # --- test helpers ------------------------------------------------------
    def set_script(self, lines: list[str]) -> None:
        """Install a scripted output sequence. Used by chaos and e2e tests."""
        self.script = list(lines)

    def parse_usage(self, text: str) -> dict:
        """The mock reports usage in a simple, parseable form."""
        if "tokens:" in text.lower():
            try:
                payload = text.lower().split("tokens:", 1)[1].strip().split()[0]
                return {"tokens": int(payload)}
            except (ValueError, IndexError):
                return {}
        return {}


__all__ = ["MockAdapter"]
