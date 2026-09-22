"""VS Code extension adapter (roadmap item 22).

Best-effort by declared design: the roadmap says "hook API if the extension
exposes one". This adapter drives the VS Code CLI (``code``), which is what a
VS Code agent session is reachable through today, and declares honestly what
that buys — a workspace can be opened and a prompt can be handed over, but
there is no programmatic output channel to subscribe to, so output is read
through the shared PTY/fallback path like any black-box harness.

What is deliberately NOT claimed:

* no ``structured_output`` — the CLI emits terminal text, not frames;
* no ``resumable`` — a killed window is a lost conversation;
* no ``mcp_tools`` — tool access lives inside the extension, not the CLI.

An adapter that over-declares here does not fail loudly; it just makes the
capability-mismatch detector (item 186) fire on behaviour that was honest. The
declaration below is the floor the extension surface can actually support.
"""

from __future__ import annotations

from typing import Any

from openburrow.a2a.card import HarnessCapabilities
from openburrow.adapters.harnesses.generic import GenericCliAdapter

#: Where VS Code keeps per-user state. Surfaced by ``burrow doctor`` so a
#: missing CLI is distinguishable from a missing extension host.
_DOCS = "https://code.visualstudio.com/docs/editor/ai"


class VSCodeAdapter(GenericCliAdapter):
    """Drive a VS Code agent workspace through the ``code`` CLI."""

    name = "vscode"
    description = "VS Code agent sessions via the `code` CLI (best-effort, no output API)."
    binary = "code"
    binary_env = "OPENBURROW_VSCODE_CLI"
    docs_url = _DOCS

    has_structured_mode: bool = False
    parses_json_frames: bool = False
    is_resumable: bool = False
    mcp_native: bool = False

    @property
    def capabilities(self) -> HarnessCapabilities:
        return HarnessCapabilities(
            structured_output=False,
            streaming=True,
            resumable=False,
            mcp_tools=False,
            supports_interrupt=True,
            native_a2a=False,
        )

    def skills(self) -> list[Any]:
        """Deliberately empty: the Agent Card must not advertise skills the CLI
        surface cannot exercise. The base set comes from the lane itself."""
        return []

    def _interpret(self, chunk: str) -> Any:
        """Terminal text in, best-effort interpretation out (inherited)."""
        return super()._interpret(chunk)


__all__ = ["VSCodeAdapter"]
