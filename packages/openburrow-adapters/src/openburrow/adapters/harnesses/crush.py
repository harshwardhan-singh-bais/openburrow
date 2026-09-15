"""Crush adapter.

Crush is the near-frictionless target: it is Go, multi-provider, MCP-native, and
it already reads ``AGENTS.md`` natively. Two consequences worth noting:

1. The adapter does not need to inject ``AGENTS.md`` — Crush reads it itself.
   OpenBurrow's Brain exports *into* that file (item 109), so a Crush lane
   benefits from shared knowledge without any injection at all.
2. Because Crush is MCP-native, the adapter declares ``mcp_tools=True``
   truthfully, and the bus deliberately does not mediate its tool calls. MCP is
   the tool layer; A2A is the agent layer. Mixing them is the mistake this
   architecture avoids.
"""

from __future__ import annotations

import os

from openburrow.a2a.card import SkillSpec
from openburrow.adapters.base import _strip_ansi
from openburrow.adapters.harnesses.generic import GenericCliAdapter
from openburrow.core.models import Lane


class CrushAdapter(GenericCliAdapter):
    name = "crush"
    description = "Crush — Go, multi-provider, MCP-native CLI that reads AGENTS.md natively."
    binary = "crush"
    binary_env = "CRUSH_BIN"
    docs_url = "https://github.com/charmbracelet/crush"

    base_args: tuple[str, ...] = ()
    structured_args: tuple[str, ...] = ()
    has_structured_mode: bool = False
    is_resumable: bool = False
    mcp_native: bool = True

    def skills(self) -> list[SkillSpec]:
        return [
            SkillSpec(
                skill_id="crush-implement",
                name="MCP-native implementation",
                description="Implement a change with direct access to MCP tool servers.",
                tags=["code", "mcp-native"],
            )
        ]

    #: Credentials this harness understands. A lane gets one only by granting it
    #: in its template's ``env_passthrough``.
    #:
    #: Crush can talk to several providers, so all three are declared — but
    #: declaring is not granting. This list used to be an unconditional
    #: ``os.environ`` sweep, which meant a Crush lane configured for Gemini was
    #: also handed the Anthropic and OpenAI keys. Under least privilege a lane
    #: gets the provider it was configured for, and the others stay out of reach
    #: of a process that could be prompt-injected.
    credential_env: tuple[str, ...] = (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
    )

    def extra_env(self, lane: Lane) -> dict[str, str]:
        """Crush's non-credential configuration.

        ``CRUSH_CONFIG_DIR`` selects a config directory rather than carrying a
        secret, so it is read from the ambient environment. It comes after
        ``super()`` so the granted credentials survive.
        """
        env = super().extra_env(lane)
        for key in ("CRUSH_CONFIG_DIR",):
            value = os.environ.get(key)
            if value:
                env[key] = value
        return env

    def _interpret(self, text: str) -> list:
        """Strip terminal styling, then note whether Crush read ``AGENTS.md``.

        The stripping is not done *here* any more. This adapter used to carry its
        own ``_strip_ansi`` — a lazy global holding a narrower pattern that
        matched CSI and OSC but not the two-character escapes — while the base
        ``_interpret`` stripped ANSI too, with a different pattern. Two
        definitions of "what counts as a control sequence" is one too many: they
        drift, and the divergence shows up as a diff that parses for one adapter
        and not another. There is now a single implementation in
        :func:`~openburrow.adapters.base._strip_ansi`, applied by the base
        ``_interpret`` for every adapter.

        What remains genuinely Crush-specific is the AGENTS.md note. Crush reads
        that file itself rather than being told to, so the adapter does not inject
        it — but the bus should still know the lane consumed shared knowledge,
        because that is a fact about what the lane saw.
        """
        cleaned = _strip_ansi(text)
        outputs = super()._interpret(cleaned)
        if "AGENTS.md" in cleaned:
            for output in outputs:
                output.data = {**output.data, "openburrow:readAgentsMd": True}
        return outputs


__all__ = ["CrushAdapter"]
