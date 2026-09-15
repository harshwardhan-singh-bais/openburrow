"""Codex CLI adapter.

Codex has a sandbox and an approval policy of its own, which is worth being
explicit about: those settings are *the harness's* guardrails, not OpenBurrow's.
OpenBurrow's policy gate runs first (before the process spawns) and Codex's runs
second (inside the process). Layering rather than replacing is deliberate —
neither one alone is sufficient, and pretending one subsumes the other is how a
gap gets missed.

The adapter surfaces Codex's own approval prompts to the bus as
``auth_required``, so a Codex-native approval and an OpenBurrow approval are the
same event to every consumer.

**The ``--json`` wire format below is assumed, not verified.** Codex is not
installed on the machine this adapter was written on and no real capture exists
in the repository, so the envelope key and the event names are taken from
Codex's documentation and marked as a hypothesis throughout. The Claude Code
adapter's mapping, by contrast, is verified against fixtures and a stand-in
harness over a real PTY. Do not treat the two as equally established. Verify
against a real ``codex --json`` run before trusting the field names.
"""

from __future__ import annotations

import os
import re
from typing import Any, ClassVar

from openburrow.a2a.card import SkillSpec
from openburrow.adapters.base import HarnessOutput, SpawnSpec
from openburrow.adapters.harnesses.generic import GenericCliAdapter, _flatten_content
from openburrow.core.models import Lane

#: An approval *request*, as opposed to prose that merely mentions approvals.
#: Narrowed on purpose: "I'll ask for approval before deleting anything" is an
#: agent describing its plan, and reclassifying that as a blocking state would
#: park a lane that is working normally.
_APPROVAL_REQUEST = re.compile(
    r"\b(?:requires?|needs?|requesting|awaiting|grant)\s+approval\b",
    re.IGNORECASE,
)


class CodexAdapter(GenericCliAdapter):
    name = "codex"
    description = "Codex CLI — OpenAI's terminal agent, with its own sandbox and approval policy."
    binary = "codex"
    binary_env = "CODEX_BIN"
    docs_url = "https://github.com/openai/codex"

    base_args: tuple[str, ...] = ()
    structured_args: tuple[str, ...] = ("--json",)
    has_structured_mode: bool = True
    is_resumable: bool = True
    mcp_native: bool = True

    #: Key each ``--json`` event is wrapped in. **ASSUMED — see the module docstring.**
    ENVELOPE_KEY = "msg"

    #: Inner event type -> the ``kind`` the bus understands.
    #:
    #: ``translate_output`` broadcasts only ``plan|diff|result|error|status``,
    #: so a frame left carrying Codex's own vocabulary is silently dropped.
    #: **The names are ASSUMED.**
    FRAME_KINDS: ClassVar[dict[str, str]] = {
        "agent_message": "result",
        "agent_reasoning": "text",
        "exec_command_begin": "tool-call",
        "exec_command_end": "tool-call",
        "task_complete": "result",
        "error": "error",
        "stream_error": "error",
    }

    #: Frame types that end a run. **ASSUMED.**
    TERMINAL_FRAMES: frozenset[str] = frozenset({"task_complete"})

    def skills(self) -> list[SkillSpec]:
        return [
            SkillSpec(
                skill_id="codex-implement",
                name="Sandboxed implementation",
                description="Make a change inside Codex's own sandbox and report the diff.",
                tags=["code", "sandboxed"],
            )
        ]

    def build_spawn_spec(self, lane: Lane) -> SpawnSpec:
        """Add Codex's sandbox/approval flags from settings.

        Both default to the *restrictive* end (``workspace-write`` and
        ``on-request``). A user who wants ``danger-full-access`` has to say so
        explicitly in their environment, which is the correct amount of friction
        for a setting that removes a safety layer.
        """
        spec = super().build_spawn_spec(lane)
        if self.settings.codex_sandbox:
            spec.command.extend(["--sandbox", self.settings.codex_sandbox])
        if self.settings.codex_approval_policy:
            spec.command.extend(["--ask-for-approval", self.settings.codex_approval_policy])
        return spec

    #: Credentials this harness understands. A lane gets one only by granting it
    #: in its template's ``env_passthrough``.
    credential_env: tuple[str, ...] = ("OPENAI_API_KEY",)

    def extra_env(self, lane: Lane) -> dict[str, str]:
        """Codex's non-credential configuration.

        ``CODEX_HOME`` and ``OPENAI_BASE_URL`` steer which account and endpoint
        Codex talks to, but they are not secrets, so they are read from the
        ambient environment rather than gated by a per-lane grant. They come
        after ``super()`` so the granted ``OPENAI_API_KEY`` is not dropped.
        """
        env = super().extra_env(lane)
        for key in ("CODEX_HOME", "OPENAI_BASE_URL"):
            value = os.environ.get(key)
            if value:
                env[key] = value
        return env

    def _from_json(self, payload: dict[str, Any]) -> HarnessOutput:
        """Unwrap Codex's event envelope, classify the inner event, flag approvals.

        Three things were wrong here, and only the third was the one this method
        was written for.

        1. **The envelope was never unwrapped.** Codex wraps each event as
           ``{"id": ..., "msg": {...}}``, so ``type``, ``message`` and ``text``
           all sit one level down. The base mapper reads them at the top level,
           found none of them, and produced ``kind="result"`` with empty text —
           the same silent-mute failure Claude Code had: no exception, no
           warning, and a lane whose replies never reached the bus.

        2. **The inner type was left as Codex's own vocabulary.**
           ``translate_output`` broadcasts only ``plan|diff|result|error|status``,
           so even once the text was found, a frame keeping ``"agent_message"``
           as its kind would still be dropped.

        3. **Approval detection was a substring test over the message body.**
           ``"approval" in text`` matched an agent saying "I'll ask for approval
           before deleting", which reclassified a working lane's message as a
           blocking state. :data:`_APPROVAL_REQUEST` requires the request form.

        ``msg`` is handled here rather than in the base mapper deliberately. The
        base mapper is shared by every adapter, and this envelope is *assumed*
        rather than confirmed. An unverified assumption belongs in the one
        adapter that makes it.

        ====================  ============  ==========
        inner ``type``         kind          terminal
        ====================  ============  ==========
        ``agent_message``      result        no
        ``agent_reasoning``    text          no
        ``exec_command_*``     tool-call     no
        ``task_complete``      result        yes
        ``*error``             error         no
        ====================  ============  ==========
        """
        frame = payload.get(self.ENVELOPE_KEY)
        enveloped = isinstance(frame, dict)
        inner: dict[str, Any] = frame if enveloped else payload

        output = super()._from_json(inner)
        frame_type = str(inner.get("type") or inner.get("kind") or "")

        if frame_type in self.FRAME_KINDS:
            output.kind = self.FRAME_KINDS[frame_type]
        if frame_type in self.TERMINAL_FRAMES:
            output.terminal = True

        # A tool event carries no prose, so a frame that is *only* a command
        # would reach the buffer with empty text — and ``translate_output``
        # drops a frame with neither text nor an artifact. The frame type is the
        # only information it had, so it becomes the text rather than being lost.
        if output.kind == "tool-call" and not output.text.strip():
            output.text = f"tool: {frame_type}"

        # ``task_complete`` reports its answer under ``last_agent_message``,
        # which is not one of the generic field names the base mapper probes —
        # so the *terminal* frame, the one carrying the run's conclusion, was
        # the one that arrived empty.
        if not output.text.strip():
            for key in ("last_agent_message", "agent_message", "message"):
                text = _flatten_content(inner.get(key))
                if text:
                    output.text = text
                    break

        data = dict(output.data)
        data["openburrow:frame"] = frame_type
        if enveloped:
            # Keep the envelope's id: it is the only handle a later frame has for
            # correlating with this one.
            data["openburrow:envelopeId"] = payload.get("id")

        if "approval" in frame_type.lower() or _APPROVAL_REQUEST.search(output.text):
            output.kind = "status"
            data["openburrow:requiresApproval"] = True

        output.data = data
        return output


__all__ = ["CodexAdapter"]
