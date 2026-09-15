"""Claude Code adapter.

Claude Code is a PTY-driven CLI. It has a structured output mode
(``--output-format stream-json``) which the adapter prefers, and it keeps its
own credentials in ``~/.claude`` — the adapter never touches them, it only
passes through the environment variable names the lane template declared.

Notable behaviour: Claude Code is the harness most likely to be the one making
a *review* decision in a negotiation, because it is strong at reasoning about a
diff. The adapter therefore advertises the review skill prominently.

The ``stream-json`` schema is Anthropic's message envelope wrapped in a small
event frame, which is why this adapter overrides the mapper rather than
inheriting it: the base can extract the text and the usage, but only this
adapter knows that ``"assistant"`` means "the lane is talking" and that
``"result"`` is the frame that ends the run.
"""

from __future__ import annotations

import os
from typing import Any

from openburrow.a2a.card import SkillSpec
from openburrow.adapters.base import HarnessOutput
from openburrow.adapters.harnesses.generic import _DIFF_HEADER, GenericCliAdapter
from openburrow.core.models import Lane


class ClaudeCodeAdapter(GenericCliAdapter):
    name = "claude-code"
    description = "Claude Code — Anthropic's terminal agent, PTY-driven with a stream-json mode."
    binary = "claude"
    binary_env = "CLAUDE_CODE_BIN"
    docs_url = "https://docs.anthropic.com/en/docs/claude-code"

    base_args: tuple[str, ...] = ()
    structured_args: tuple[str, ...] = ("--output-format", "stream-json")
    prompt_as_arg: bool = True
    prompt_flag: str = "-p"
    has_structured_mode: bool = True
    is_resumable: bool = True
    mcp_native: bool = True

    #: Frame types that close a run. Claude Code emits exactly one.
    TERMINAL_FRAMES: frozenset[str] = frozenset({"result"})

    def skills(self) -> list[SkillSpec]:
        return [
            SkillSpec(
                skill_id="claude-review",
                name="Deep diff review",
                description="Reason carefully about a proposed diff and respond with a performative.",
                tags=["review", "reasoning"],
                examples=["Would this signature change break callers?", "Is this migration safe?"],
            )
        ]

    #: Credentials this harness understands. A lane gets one only by granting it
    #: in its template's ``env_passthrough``.
    credential_env: tuple[str, ...] = ("ANTHROPIC_API_KEY",)

    def extra_env(self, lane: Lane) -> dict[str, str]:
        """Pass through only the credential names this lane was granted.

        This docstring said exactly that before it was true, and it was false:
        ``ANTHROPIC_API_KEY`` was copied from the ambient environment whether or
        not the lane's template mentioned it. "Included by default because Claude
        Code is useless without it" is a real concern and it is now handled where
        it belongs — in the template, which grants the key explicitly, and in
        ``burrow init``, which writes that grant.

        ``ANTHROPIC_MODEL`` and ``CLAUDE_CODE_USE_BEDROCK`` are configuration
        rather than secrets, so they are read from the ambient environment and
        are not subject to the grant. They still come after ``super()``, because
        that is where the granted credentials are.
        """
        env = super().extra_env(lane)
        for key in ("ANTHROPIC_MODEL", "CLAUDE_CODE_USE_BEDROCK"):
            value = os.environ.get(key)
            if value:
                env[key] = value
        return env

    # --- frame mapping -----------------------------------------------------
    def _from_json(self, payload: dict[str, Any]) -> HarnessOutput:
        """Map one ``stream-json`` frame onto a bus-visible output.

        The base mapper extracts text and usage correctly — including from
        Claude Code's nested ``message`` envelope — but it cannot classify,
        because ``kind`` is the harness's own vocabulary. Claude Code calls its
        main frame ``"assistant"``, and
        :meth:`~openburrow.adapters.base.HarnessAdapter.translate_output`
        broadcasts only ``plan|diff|result|error|status``. Left to the base,
        every frame carried kind ``"assistant"`` or ``"system"`` and was
        therefore **never broadcast**. The lane was talking and the bus heard
        nothing: no error, no warning, just silence, which is the hardest kind
        of failure to notice and the reason this override exists.

        =============  =====================================================
        ``system``     ``status`` — the init frame; its ``session_id`` is
                       captured so ``is_resumable`` has something to resume
        ``assistant``  ``tool-call`` when the content is a tool invocation,
                       ``diff`` when the prose carries one, otherwise
                       ``result`` (non-terminal) so the reply reaches the bus
        ``user``       ``status`` — tool results being fed back in
        ``result``     ``error`` when ``is_error``, else ``result``; terminal
                       either way, because this frame ends the run
        =============  =====================================================

        ``total_cost_usd`` is deliberately **not** passed to
        :meth:`~openburrow.core.models.session.Lane.record_usage`. That field is
        cumulative for the session while ``record_usage`` accumulates, so
        feeding it in would report the running total as the cost of every turn
        and inflate the figure quadratically. It stays in ``data``, where a
        consumer that knows the semantics can read it without the model layer
        silently mis-adding it.
        """
        output = super()._from_json(payload)
        frame = str(payload.get("type") or "")

        if frame == "system":
            session_id = payload.get("session_id")
            if session_id and self.lane is not None:
                self.lane.metadata.setdefault("harness_session_id", str(session_id))
            output.kind = "status"
            output.data = {**output.data, "openburrow:frame": "init"}

        elif frame == "assistant":
            kind, extra = self._classify_assistant(payload, output)
            output.kind = kind
            output.data = {**output.data, **extra}

        elif frame == "user":
            output.kind = "status"
            output.data = {**output.data, "openburrow:frame": "tool-result"}

        elif frame in self.TERMINAL_FRAMES:
            output.kind = "error" if payload.get("is_error") else "result"
            output.terminal = True

        return output

    @staticmethod
    def _classify_assistant(
        payload: dict[str, Any], output: HarnessOutput
    ) -> tuple[str, dict[str, Any]]:
        """Decide what an ``assistant`` frame is: tool call, diff, or reply.

        A frame can carry prose and tool invocations at once, so the order is
        deliberate. A tool invocation is the more specific fact: a frame reading
        ``"Let me read that file."`` alongside a ``Read`` block is better
        described as a tool call than as a reply.

        Fills in ``output.text`` for a pure tool call. A ``tool_use`` block
        carries no prose of its own, so such a frame would otherwise reach the
        buffer with empty text and the tool name — the only information it had —
        would be lost.
        """
        message = payload.get("message")
        blocks = message.get("content") if isinstance(message, dict) else None

        tool_names: list[str] = []
        if isinstance(blocks, list):
            tool_names = [
                str(block.get("name") or "tool")
                for block in blocks
                if isinstance(block, dict) and block.get("type") == "tool_use"
            ]

        if tool_names:
            if not output.text.strip():
                output.text = "tool_use: " + ", ".join(tool_names)
            return "tool-call", {"openburrow:tools": tool_names}

        if output.artifacts or _DIFF_HEADER.search(output.text):
            return "diff", {}

        return "result", {}


__all__ = ["ClaudeCodeAdapter"]
