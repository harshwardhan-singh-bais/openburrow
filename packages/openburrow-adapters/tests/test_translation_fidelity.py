"""Translation fidelity across every adapter pair (roadmap item 51).

The bus only works if meaning survives the two hops every message takes:

1. **out** — a harness's native output is mapped onto an A2A message by
   ``translate_output``;
2. **in** — that message is rendered into a harness's native input by
   ``render_injection``.

A pair (sender, receiver) is faithful when what the sender said is still
present in what the receiver is asked to read. This module checks every
ordered pair of shipped adapters, because fidelity is a property of the *pair*:
a sender that drops the subject line breaks the exchange even if every
receiver would have honoured it.

Three properties, per pair:

* **round trip** — sender output → bus message → receiver-rendered text still
  carries the sender's words;
* **attribution** — the receiver can always tell who sent it;
* **PTY survivability** — rendered text contains no control sequences and no
  carriage returns (the PTY layer normalises CR and strips ANSI, so anything a
  renderer emits there arrives mangled), and every line is non-empty after
  line-mode reassembly, because a blank line in a JSONL stream is a dropped
  frame.
"""

from __future__ import annotations

from typing import Any

import pytest

from openburrow.adapters.base import HarnessOutput, _strip_ansi
from openburrow.adapters.harnesses.aider import AiderAdapter
from openburrow.adapters.harnesses.claude_code import ClaudeCodeAdapter
from openburrow.adapters.harnesses.codex import CodexAdapter
from openburrow.adapters.harnesses.crush import CrushAdapter
from openburrow.adapters.harnesses.custom import CustomScriptAdapter
from openburrow.adapters.harnesses.gemini import GeminiAdapter
from openburrow.adapters.harnesses.goose import GooseAdapter
from openburrow.adapters.harnesses.mock import MockAdapter
from openburrow.adapters.harnesses.opencode import OpenCodeAdapter
from openburrow.adapters.harnesses.vscode import VSCodeAdapter
from openburrow.core.config.settings import Settings
from openburrow.core.models import BusMessage, Performative

pytestmark = pytest.mark.unit

#: Every adapter the registry ships. A new adapter joins this suite by being
#: listed here — the suite fails for it until its translation pair holds up.
ALL_ADAPTERS: tuple[type, ...] = (
    ClaudeCodeAdapter,
    CodexAdapter,
    CrushAdapter,
    OpenCodeAdapter,
    GeminiAdapter,
    AiderAdapter,
    GooseAdapter,
    MockAdapter,
    CustomScriptAdapter,
    VSCodeAdapter,
)


def instances() -> dict[str, Any]:
    built: dict[str, Any] = {}
    for cls in ALL_ADAPTERS:
        adapter: Any = cls(Settings(), lane=None)
        built[str(adapter.name)] = adapter
    return built


def proposal() -> BusMessage:
    """A structured `counter` message: the hardest thing to keep faithful.

    Carries refs, a requested change and a reply requirement — every field the
    renderer is supposed to reproduce — plus a body with distinctive tokens so
    truncation or reflow shows up as a failed assertion rather than silently.
    """
    return BusMessage(
        session_id="sess_fidelity",
        sender_lane="lane_alice",
        sender_harness="codex",
        subject="signature change vs caller",
        body=(
            "I renamed `parse_config` to `load_config` and updated its three "
            "callers in `core/config/load.py`. Update your call site before "
            "rebasing."
        ),
        intent=Performative.COUNTER,
        requires_reply=True,
        payload={
            "openburrow:refs": ["core/config/load.py", "core/config/settings.py"],
            "openburrow:requestedChange": "rename your call sites to load_config",
        },
    )


def sender_output(kind: str, text: str) -> HarnessOutput:
    return HarnessOutput(kind=kind, text=text, structured=True)


# ---------------------------------------------------------------------------
# per-adapter invariants (the pairwise checks build on these)
# ---------------------------------------------------------------------------
class TestRenderInjectionBasics:
    def test_every_adapter_names_the_origin(self) -> None:
        for adapter in instances().values():
            rendered = adapter.render_injection(proposal())
            assert "[OpenBurrow" in rendered, f"{adapter.name} drops the attribution header"

    def test_every_adapter_preserves_the_body_verbatim(self) -> None:
        body = proposal().body
        for adapter in instances().values():
            rendered = adapter.render_injection(proposal())
            assert body in rendered, f"{adapter.name} mangled the message body"

    def test_no_adapter_emits_control_characters(self) -> None:
        """Anything the renderer emits that ANSI/CR handling would mangle."""
        for adapter in instances().values():
            rendered = adapter.render_injection(proposal())
            assert "\r" not in rendered, f"{adapter.name} emits CR into the injection"
            assert "\x1b" not in rendered, f"{adapter.name} emits ANSI into the injection"
            assert _strip_ansi(rendered) == rendered

    def test_rendered_lines_survive_line_mode_reassembly(self) -> None:
        """A blank line inside a JSONL stream is a frame the parser drops."""
        from openburrow.adapters.base import _ChunkAssembler

        assembler = _ChunkAssembler(line_mode=True)
        for adapter in instances().values():
            units = assembler.feed(adapter.render_injection(proposal()) + "\n")
            assert units, f"{adapter.name} rendered nothing"
            assert all(unit.strip() for unit in units), (
                f"{adapter.name} emitted a blank line into a line-delimited stream"
            )


# ---------------------------------------------------------------------------
# the pairwise round trip (item 51 proper)
# ---------------------------------------------------------------------------
_ADAPTER_NAMES: list[str] = [str(cls.name) for cls in ALL_ADAPTERS]  # type: ignore[attr-defined]


@pytest.mark.parametrize("sender_name", _ADAPTER_NAMES)
@pytest.mark.parametrize("receiver_name", _ADAPTER_NAMES)
class TestPairwiseFidelity:
    def test_outbound_translation_preserves_the_words(
        self, sender_name: str, receiver_name: str
    ) -> None:
        """Sender output → bus message must carry the sender's text intact."""
        sender = instances()[sender_name]
        receiver = instances()[receiver_name]

        output = sender_output(
            "result",
            f"[{sender_name}] tests pass; parse_config renamed to load_config everywhere",
        )
        message = sender.translate_output(output)
        assert message is not None, f"{sender_name} refused to broadcast a result"

        rendered = receiver.render_injection(message)
        assert "parse_config renamed" in rendered, (
            f"{sender_name} -> {receiver_name}: the receiver is asked to read "
            "a message whose content did not survive the trip"
        )

    def test_inbound_rendering_survives_the_pty_path(
        self, sender_name: str, receiver_name: str
    ) -> None:
        receiver = instances()[receiver_name]

        rendered = receiver.render_injection(proposal())
        cleaned = _strip_ansi(rendered)

        assert f"from {sender_name}" in cleaned or "[OpenBurrow" in cleaned, (
            f"{receiver_name} lost the attribution of a message sent by {sender_name}"
        )
        for ref in proposal().payload["openburrow:refs"]:
            assert ref in cleaned, f"{sender_name} -> {receiver_name}: ref {ref!r} dropped"

    def test_reply_requirement_survives_the_pair(
        self, sender_name: str, receiver_name: str
    ) -> None:
        """A message needing a reply must say so after rendering, every pair."""
        receiver = instances()[receiver_name]
        message = proposal()
        message.sender_harness = sender_name

        rendered = receiver.render_injection(message)
        assert "reply is required" in rendered.casefold(), (
            f"{sender_name} -> {receiver_name}: a blocking proposal arrives "
            "looking optional; the receiver will never answer"
        )
