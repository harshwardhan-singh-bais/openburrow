"""Bring-your-own-harness adapter.

Drives an arbitrary executable through a small, documented contract:

* the command is read from ``lane.metadata["command"]`` or
  ``OPENBURROW_CUSTOM_ADAPTER_COMMAND``
* the prompt is written to stdin, one prompt per line
* the process may emit JSONL on stdout, which is parsed like any structured
  harness; anything else goes through the generic fallback parser
* exit code 0 means success; anything else fails the task

This is deliberately the *weakest* adapter in the set. A custom harness gets no
special treatment, no capability assumptions, and no structured guarantees — it
is the honest floor of what OpenBurrow can support, and it means the answer to
"does it work with my harness?" is always "yes, at least this well."

Weakest declaration, not weakest implementation
-----------------------------------------------

"Conservative" describes the *claims* this adapter makes, not the code it runs.
Those were conflated, and the cost was a fourth copy of every PTY bug.

This class used to extend :class:`~openburrow.adapters.base.HarnessAdapter`
directly and carry its own interaction code: a ``send_prompt`` that wrote
``prompt + "\\n"`` to the PTY, a ``read_output`` with a blocking
``self._pty.read(4096)`` inside an ``async`` generator, and an unconditional
``.decode("utf-8")`` on the result. Each of those is a defect that had already
been found and fixed one adapter at a time in Stage 10 — the LF-to-a-PTY bug
alone means the harness never receives a submitted line and every lane hangs —
and each was present here again, because a class that does not inherit a fix
does not get it.

It also instantiated a :class:`GenericCliAdapter` inside ``read_output`` purely
to borrow ``_interpret``, which meant a fresh adapter object per read loop, with
its own settings, its own lane reference, and no share in the buffer.

So the conservative *declaration* now sits on top of the shared implementation.
This adapter extends ``GenericCliAdapter`` and overrides only what is genuinely
different about it: where the command comes from, and what it can promise. PTY
handling, line assembly, ANSI stripping, the interpretation order, the usage
detector, and the fallback parser are all inherited, which is the point — there
is one copy of that code in the repository and this class is not it.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path

from openburrow.a2a.card import HarnessCapabilities
from openburrow.adapters.base import SpawnSpec
from openburrow.adapters.harnesses.generic import GenericCliAdapter
from openburrow.core.errors import ConfigError
from openburrow.core.models import Lane


class CustomScriptAdapter(GenericCliAdapter):
    """Runs a user-supplied command as a harness."""

    name = "custom"
    description = "Bring your own harness — any executable that reads a prompt on stdin."
    binary = ""
    binary_env = "OPENBURROW_CUSTOM_ADAPTER_COMMAND"
    docs_url = "https://github.com/openburrow/openburrow/blob/main/docs/adapters/README.md"

    #: No structured flag exists to pass, because we do not know the command.
    has_structured_mode: bool = False
    #: But JSON on the output stream is still a frame, and is still parsed.
    #:
    #: This is the one place in the adapter set where the two flags must differ,
    #: and it is the case that motivated splitting them. The contract above
    #: promises that a process "may emit JSONL on stdout, which is parsed like any
    #: structured harness" — that promise was false, because the JSON branch was
    #: gated on ``has_structured_mode``, which this adapter cannot honestly set.
    #: The result was that a well-behaved custom harness emitting JSONL had every
    #: frame classified ``text``, a kind the bus does not broadcast, so it ran
    #: correctly and published nothing.
    #:
    #: "I cannot promise this command emits structure" and "I should try to parse
    #: it if it does" are different statements. This adapter is the proof.
    parses_json_frames: bool = True
    is_resumable: bool = False
    mcp_native: bool = False

    @property
    def capabilities(self) -> HarnessCapabilities:
        """Conservative by construction: we know nothing about this harness."""
        return HarnessCapabilities(
            structured_output=False,
            streaming=True,
            resumable=False,
            mcp_tools=False,
            supports_interrupt=True,
            native_a2a=False,
        )

    def resolve_binary(self) -> str:
        return os.environ.get(self.binary_env, "").strip()

    def build_spawn_spec(self, lane: Lane) -> SpawnSpec:
        """Resolve the lane's command into a spawn spec.

        Overridden because the command is *data* here rather than a class
        attribute: it comes from the lane template or the environment. The
        inherited version would read :attr:`binary`, which is empty by design —
        there is no single binary this adapter knows about.

        ``command`` is accepted in both of the forms a YAML lane template can
        express, and the list form is the documented one — the error hint tells
        the user to "give the lane a ``command:`` list in openburrow.yaml".

        That hint was a lie. The list was stringified and then shell-split::

            lane.metadata = {"command": ["python", "-c", "pass"]}
            raw  = str(["python", "-c", "pass"])   # "[python, -c, pass]"
            argv = shlex.split(raw)                # ['[python,', '-c,', 'pass]']

        A lane template written exactly as documented produced an argv of
        mangled tokens and a harness that could not be found. The branch that
        was supposed to handle a list read ``command_argv`` instead — a key that
        appears nowhere else in the repository, in no documentation, and in no
        example, so the code path that worked was the one nobody was told to use.

        A list is now used verbatim as argv, and only a string is shell-split.
        That is the right split regardless of which key holds it: a list is
        already tokenised, and re-tokenising it can only lose information.

        ``use_pty`` defaults to False and is opt-in per lane. The inherited
        default is True, which is right for an interactive TUI and wrong for an
        arbitrary command: a build script or a pipe filter is not a terminal
        program, and forcing one onto a PTY changes how it buffers and how it
        renders. A lane that *is* interactive sets ``use_pty: true`` and gets the
        same PTY path every other adapter uses.
        """
        raw = lane.metadata.get("command") or self.resolve_binary()
        if not raw:
            raise ConfigError(
                "custom adapter has no command configured",
                hint=(
                    "Set OPENBURROW_CUSTOM_ADAPTER_COMMAND, or give the lane a "
                    "`command:` list in openburrow.yaml."
                ),
                context={"lane_id": lane.id},
            )

        argv = [str(part) for part in raw] if isinstance(raw, list) else shlex.split(str(raw))
        if not argv:
            raise ConfigError(
                "custom adapter resolved an empty command",
                hint="`command:` is present but contains no tokens.",
                context={"lane_id": lane.id},
            )

        worktree = Path(lane.worktree_path) if lane.worktree_path else Path.cwd()
        env = self.base_env(lane)
        env.update({str(k): str(v) for k, v in (lane.metadata.get("env") or {}).items()})

        return SpawnSpec(
            command=argv,
            cwd=worktree,
            env=env,
            use_pty=bool(lane.metadata.get("use_pty", False)),
            stdin_pipe=True,
        )


__all__ = ["CustomScriptAdapter"]
