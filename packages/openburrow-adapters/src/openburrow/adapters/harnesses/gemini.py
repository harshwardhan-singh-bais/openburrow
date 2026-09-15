"""Gemini CLI / Antigravity adapter.

Covers both surfaces with one adapter because they share a binary and a
configuration directory. Antigravity is treated as the same harness with a
different entry point — pretending they are two adapters would duplicate the
translation logic for no benefit.

Why this adapter does not declare structured output
---------------------------------------------------

It used to. ``has_structured_mode = True`` and
``structured_args = ("--output-format", "json")``, which is what Gemini's own
documentation recommends for automation. The declaration was wrong, and it was
wrong in a way that made the adapter *worse* than declaring nothing.

Gemini's CLI reference is explicit about when its headless mode engages:

* positional prompt — "Defaults to interactive mode **in a TTY**. Use
  ``-p/--prompt`` for non-interactive execution."
* ``-p/--prompt`` — "Forces non-interactive mode."
* headless mode is "triggered when the CLI is run in a non-TTY environment **or**
  when providing a query with the ``-p`` flag."

``--output-format`` is a headless-mode option. OpenBurrow spawns every CLI lane
under a PTY and writes prompts to it over the session, so the process *is* a TTY
and no ``-p`` is ever passed — meaning headless mode never engages, the output
format is inert, and what runs is the interactive TUI.

So the flag produced two failures at once:

1. **A false declaration.** ``has_structured_mode`` feeds
   ``capabilities.structured_output``, which the Agent Card publishes and the
   governance layer spot-checks against observed behaviour. A lane that
   advertised JSON and emitted a TUI is precisely the mismatch the detector
   exists to find, and the declaration was the thing lying.
2. **A worse reader.** ``has_structured_mode`` also drove
   ``parses_json_frames``, which selects line-delimited assembly — correct for
   JSONL, wrong for a TUI. A multi-line diff from the TUI would have been split
   into one output per line, each with its own artifact.

The real dependency
-------------------

Making Gemini's structured mode work is not a configuration change. It needs
``-p <prompt>``, which is a *one-shot* invocation: the process answers one
prompt and exits, so a lane would need a fresh process per turn and the reader
would have to survive the process exiting between turns.

That is exactly the trade-off
:attr:`~openburrow.adapters.harnesses.generic.GenericCliAdapter.prompt_as_arg`
already documents as the reason it is inert, and it is a lane-lifecycle feature
rather than an adapter fix. Until it exists, an interactive Gemini lane that
holds context across turns is the honest configuration and a structured lane
that dies after one turn is not.

Recorded in ``FEATURE_STATUS.md`` as remaining, with the documented JSON schema
so the work is a mapping job rather than a research job when it is picked up:
``{"response": str, "stats": {...}, "error": {...}}``, and for
``--output-format stream-json`` the event types ``init``, ``message``,
``tool_use``, ``tool_result``, ``error``, ``result``.
"""

from __future__ import annotations

import os

from openburrow.a2a.card import SkillSpec
from openburrow.adapters.harnesses.generic import GenericCliAdapter
from openburrow.core.models import Lane


class GeminiAdapter(GenericCliAdapter):
    name = "gemini"
    description = "Gemini CLI / Antigravity — Google's terminal agent."
    binary = "gemini"
    binary_env = "GEMINI_CLI_BIN"
    docs_url = "https://github.com/google-gemini/gemini-cli"

    base_args: tuple[str, ...] = ()
    #: Empty because no structured mode is reachable under a PTY. See the module
    #: docstring — this is a corrected declaration, not a removed feature.
    structured_args: tuple[str, ...] = ()
    has_structured_mode: bool = False
    is_resumable: bool = False
    mcp_native: bool = True

    def skills(self) -> list[SkillSpec]:
        return [
            SkillSpec(
                skill_id="gemini-implement",
                name="Implementation",
                description="Implement a change using Gemini models.",
                tags=["code"],
            )
        ]

    #: Credentials this harness understands. A lane gets one only by granting it
    #: in its template's ``env_passthrough``.
    #:
    #: ``GOOGLE_APPLICATION_CREDENTIALS`` is a *path*, not a secret, which is why
    #: ``is_credential_key`` looks for the word ``CREDENTIAL`` and not only for
    #: value-shaped secrets: a process handed that variable can read the file it
    #: points at, so withholding it is the same decision as withholding a key.
    credential_env: tuple[str, ...] = (
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
    )


__all__ = ["GeminiAdapter"]
