"""Goose adapter.

Goose is a black-box-ish harness: it runs tools and narrates, but its output is
not strongly typed. It is therefore the adapter that exercises the fallback
parser most, and the one whose capability flags are most conservative.

This is intentional coverage. If the adapter layer only worked for harnesses
with clean structured output, the claim "any harness" would be false — so Goose
exists in the supported set precisely because it is the awkward case.

Which subcommand, and why it mattered
-------------------------------------

The adapter used to spawn ``goose run`` and write prompts to its stdin. Goose's
CLI reference rules that out twice over:

* ``run`` — "Execute commands from an instruction file or **stdin**", where
  stdin means ``-i -`` specifically: ``-i, --instructions <FILE>`` is documented
  as "Path to instruction file containing commands. **Use ``-`` for stdin**".
  The documented stdin form is ``echo "What is 2+2?" | goose run -i -``. Without
  ``-i``, a prompt written to stdin is not an instruction and is discarded.
* ``run`` is one-shot by design — "begins executing using any arguments provided
  and exits the session automatically once the task is complete". A long-lived
  lane writing prompts into it is asking for a session that has already ended.

So the lane's prompt went nowhere, and the process exited after the first
invocation. The correct base for a long-lived lane is the command the reference
describes as "Start or resume **interactive chat sessions**":

    goose session

That is what this adapter now spawns, and it is the same shape as every other
interactive adapter in the set: a long-lived process under a PTY, with prompts
written to it one line at a time.

The structured output that is deliberately declined
---------------------------------------------------

``goose run --output-format json|stream-json`` exists and is real, so this
adapter's ``has_structured_mode = False`` is a choice rather than a limitation.
It is declined for three reasons, in order of weight:

1. **It is a ``run`` option.** The flag is documented under ``run``, not
   ``session``. Taking it means going back to a one-shot invocation, which is
   the problem described above.
2. **``json`` is not streaming.** The reference says ``json`` gives "results
   after completion" and only ``stream-json`` gives "events as they occur". A
   lane that reports nothing until it is finished is a lane the bus cannot
   observe, which defeats the point of watching it.
3. **The roadmap says so.** Stage 12's design note keeps Goose "as the awkward
   case... the honest floor of what the protocol can do", and explicitly warns
   that "special-casing it away would make the protocol look cleaner than it
   is." A structured Goose would delete the coverage that note asks for.

Item 3 is the deciding one. The other two are reasons this would be awkward;
this is the reason it would be wrong.

Noted for later: ``goose acp`` runs Goose as an ACP agent server over stdio, and
``goose serve`` over HTTP/WebSocket. That is a stronger integration than
scraping a TUI and belongs with the negotiation work rather than here.
"""

from __future__ import annotations

import os

from openburrow.a2a.card import SkillSpec
from openburrow.adapters.harnesses.generic import GenericCliAdapter
from openburrow.core.models import Lane


class GooseAdapter(GenericCliAdapter):
    name = "goose"
    description = "Goose — general-purpose terminal agent; output is parsed, not typed."
    binary = "goose"
    binary_env = "GOOSE_BIN"
    docs_url = "https://block.github.io/goose/"

    #: ``session``, not ``run``. See the module docstring: ``run`` is one-shot
    #: and ignores stdin unless it is given ``-i -``, so a lane built on it
    #: delivered no prompt and exited immediately.
    base_args: tuple[str, ...] = ("session",)
    #: Declined on purpose; the flag is a ``run`` option and the roadmap keeps
    #: Goose as the fallback-parser case. See the module docstring.
    structured_args: tuple[str, ...] = ()
    has_structured_mode: bool = False
    is_resumable: bool = False
    mcp_native: bool = True

    def skills(self) -> list[SkillSpec]:
        return [
            SkillSpec(
                skill_id="goose-run",
                name="Run a task",
                description="Execute a described task and report what it did.",
                tags=["code", "mcp-native"],
            )
        ]

    #: Credentials this harness understands. A lane gets one only by granting it
    #: in its template's ``env_passthrough``.
    credential_env: tuple[str, ...] = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")

    def extra_env(self, lane: Lane) -> dict[str, str]:
        """Goose's non-credential configuration.

        ``GOOSE_PROVIDER`` and ``GOOSE_MODEL`` select a provider and model rather
        than carrying a secret, so they are read from the ambient environment.
        They come after ``super()`` so the granted credentials survive — an
        override that dropped them would leave the harness unable to
        authenticate while looking like it had configured the lane.
        """
        env = super().extra_env(lane)
        for key in ("GOOSE_PROVIDER", "GOOSE_MODEL"):
            value = os.environ.get(key)
            if value:
                env[key] = value
        return env


__all__ = ["GooseAdapter"]
