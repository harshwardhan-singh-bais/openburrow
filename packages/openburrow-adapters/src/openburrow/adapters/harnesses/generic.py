"""Generic CLI adapter base.

Most harnesses are the same shape: a binary, a prompt on stdin or as an
argument, structured output behind a flag, and an interactive TUI if you do not
pass that flag. :class:`GenericCliAdapter` captures that shape once so a new
adapter is a declaration rather than an implementation.

Subclasses override a handful of class attributes and, when the harness is
unusual, one or two methods. Everything else — PTY handling, buffering, the
usage-limit detector, the fallback parser — is inherited.

The important honesty rule enforced here: an adapter that cannot confirm
structured output reports ``structured=False`` on its output. That flag is what
the metrics layer uses to decide whether a message meaningfully changed a lane's
behaviour, so over-claiming it would corrupt the one number that proves the
collaboration is real.

Two flags, and keeping them apart is the point
----------------------------------------------

``has_structured_mode`` is a *declaration about the harness*: "this thing can be
made to emit machine-readable output, and here is the flag that asks for it."
It feeds ``capabilities.structured_output``, which the Agent Card publishes and
the capability-mismatch detector compares against observed behaviour.

``parses_json_frames`` is a *decision about this adapter*: "JSON in the output
stream is a frame, so try to parse it." It is the gate on the interpretation
path, and on the line-assembly mode, and on nothing else.

They were the same flag, and that conflation silenced an adapter.
``CustomScriptAdapter`` documents that its process "may emit JSONL on stdout,
which is parsed like any structured harness" — but it cannot honestly claim
structured output, because it knows nothing at all about the command it runs.
Its declaration is therefore ``False``, the JSON branch never ran, every frame
was classified ``text``, and ``text`` is a kind the bus does not broadcast. The
lane was talking and the bus heard nothing, which is the same failure that had
already bitten three other adapters by a different route.

The general form of the mistake: a flag that answers "can you?" was used to
answer "should you?". An adapter that cannot *promise* structure can still
*recognise* it.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from openburrow.a2a.card import HarnessCapabilities
from openburrow.adapters.base import (
    HarnessAdapter,
    HarnessOutput,
    SpawnSpec,
    _as_text,
    _pty_write,
    _strip_ansi,
)
from openburrow.core.config.settings import Settings
from openburrow.core.logging import get_logger
from openburrow.core.models import Lane, TaskArtifact

log = get_logger(__name__)

#: Flags that make common CLI harnesses emit machine-readable output.
#: Probed in order; the first one the binary accepts is used.
STRUCTURED_FLAG_CANDIDATES: tuple[tuple[str, ...], ...] = (
    ("--output-format", "json"),
    ("--json",),
    ("-j",),
    ("--format", "json"),
)

#: Patterns for pulling structure out of unstructured terminal text.
_DIFF_HEADER = re.compile(r"^(---|\+\+\+|@@) ", re.MULTILINE)
_JSON_BLOB = re.compile(r"\{.*\}", re.DOTALL)
_STEP_LINE = re.compile(r"^\s*(?:\d+[.)]|[-*])\s+(.+)$", re.MULTILINE)

#: How many numbered or bulleted lines make a chunk a *plan* rather than prose
#: that happens to contain a list. Two is a sentence; three is a plan.
_PLAN_MIN_STEPS = 3

#: Key names harnesses use for the input and output halves of a usage object.
_USAGE_IN_KEYS = ("input", "prompt", "input_tokens", "prompt_tokens")
_USAGE_OUT_KEYS = ("output", "completion", "output_tokens", "completion_tokens")

#: Field names a harness may use for the body of a frame. Ordered by how often
#: each is the real one, because the first field that flattens to non-empty text
#: wins. ``response`` is last and was missing entirely: a harness whose frame is
#: ``{"type": "result", "response": "..."}`` mapped to an empty body, and an
#: empty body is dropped by :meth:`HarnessAdapter.translate_output` — so the
#: answer existed in the frame and never reached the bus.
_TEXT_KEYS = ("text", "content", "message", "result", "output", "delta", "response")

#: Diagnostic key attached to a structured frame that mapped to nothing.
#:
#: A frame that produced neither text nor an artifact is dropped before
#: broadcast, which is correct — a message carrying no bytes carries no
#: information. What is not correct is that the drop is *silent*: from the bus's
#: side, a harness with an unrecognised schema is indistinguishable from a
#: harness that said nothing, and that ambiguity has now cost three adapters.
#: Naming the keys makes the frame visible in ``recent_output`` and in the log,
#: so "we did not understand this harness" is a thing an operator can see.
_UNMAPPED_KEY = "openburrow:unmappedKeys"


def _flatten_content(value: Any) -> str:
    """Reduce a JSON content field to plain text.

    Harnesses nest their text in several shapes and all of them are common: a
    bare string, a single block object, or a list of typed blocks such as
    ``[{"type": "text", "text": "..."}]``. Anthropic's message envelope — which
    Claude Code's ``stream-json`` uses verbatim — nests one level deeper again,
    at ``message.content``.

    The previous implementation ran the candidate fields through ``str()``,
    which turns a nested object into its Python ``repr``. Live output for an
    assistant frame came back as::

        "{'id': 'msg_1', 'content': [{'type': 'text', 'text': 'I will patch the parser.'}], 'usage'"

    A Python dict literal, published to a bus where every consumer expected
    prose. Nothing raised. The text was simply wrong, and the lane's real
    sentence was buried inside a repr.

    An unrecognised shape returns ``""`` rather than a stringification, because
    "we could not find the text" is a recoverable state and a repr is not.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("text", "content", "message", "value"):
            if key in value:
                flattened = _flatten_content(value[key])
                if flattened:
                    return flattened
        return ""
    if isinstance(value, list):
        parts = [part for part in (_flatten_content(item) for item in value) if part]
        return "\n".join(parts)
    return ""


def _extract_usage(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Find a usage object, including the nested ``message.usage`` form.

    Top level is the common case. Claude Code puts it one level down at
    ``message.usage``, so a top-level-only lookup returned ``{}`` for every
    assistant frame — the lane's token counters stayed at zero while the harness
    was reporting usage on every single turn.

    Returns ``None`` rather than ``{}`` so "no usage reported" and "a usage
    object that happens to be empty" stay distinguishable at the call site.
    """
    candidates: list[Any] = [payload.get("usage"), payload.get("tokens")]
    message = payload.get("message")
    if isinstance(message, dict):
        candidates.append(message.get("usage"))
        candidates.append(message.get("tokens"))
    for candidate in candidates:
        if isinstance(candidate, dict) and candidate:
            return candidate
    return None


def _first_int(mapping: dict[str, Any], keys: tuple[str, ...]) -> int:
    """First present, coercible, non-negative value among ``keys``; else zero.

    The key sets differ per provider — ``input``/``prompt`` versus
    ``input_tokens``/``prompt_tokens`` — and the previous lookup only knew the
    short forms. Claude Code reports ``input_tokens``/``output_tokens``, so its
    usage object parsed correctly and then recorded zero tokens.
    """
    for key in keys:
        value = mapping.get(key)
        if value is None:
            continue
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            continue
    return 0


def _as_float(value: Any) -> float:
    """Coerce a cost to a non-negative float, or ``0.0``.

    ``Lane.record_usage`` applies ``max(0.0, cost_usd)``, so a non-numeric value
    raises ``TypeError`` from *inside the model* rather than at the boundary
    where the bad value arrived. A provider reporting ``"cost": "unknown"``
    would take down the read loop — and a cost field is exactly the kind of
    thing a provider formats loosely.
    """
    try:
        return max(0.0, float(value or 0.0))
    except (TypeError, ValueError):
        return 0.0


class GenericCliAdapter(HarnessAdapter):
    """Base for CLI harnesses driven over a PTY."""

    name = "generic-cli"
    description = "Base class for CLI harnesses."
    binary = ""
    binary_env = ""
    docs_url = ""

    #: Extra args always passed on spawn.
    base_args: tuple[str, ...] = ()
    #: Args that select structured output, if the harness supports it.
    structured_args: tuple[str, ...] = ()
    #: True when the harness accepts a prompt as an argument rather than on stdin.
    #:
    #: **Not wired up.** Nothing reads this, and nothing reads ``prompt_flag``
    #: either — both were declared, documented, and referenced nowhere, so a
    #: reader reasonably assumed the prompt was being passed as an argument when
    #: it is always written to stdin by :meth:`send_prompt`.
    #:
    #: The reason is architectural rather than an oversight: ``-p <prompt>`` is a
    #: *one-shot* invocation, and OpenBurrow spawns a long-lived harness per lane
    #: and writes prompts into it over the session. Honouring this flag would mean
    #: re-spawning the harness for every turn, which changes the lane lifecycle.
    #: Left declared because the capability is real and a one-shot mode is a
    #: plausible future addition — but it is inert today, and saying so is
    #: cheaper than letting the name imply otherwise.
    prompt_as_arg: bool = False
    #: Flag that introduces the prompt when ``prompt_as_arg`` is set. Also inert;
    #: see above.
    prompt_flag: str = ""
    #: True when the harness has a genuinely structured mode we can rely on.
    #:
    #: This is the *declaration*, and it is published: ``capabilities`` reads it
    #: for ``structured_output``, and ``build_spawn_spec`` reads it to decide
    #: whether to append ``structured_args``. It is a claim about the harness,
    #: and it is spot-checked.
    has_structured_mode: bool = False
    #: Whether to attempt JSON parsing on the output stream. ``None`` inherits
    #: :attr:`has_structured_mode`.
    #:
    #: Override this when the two answers differ, which is exactly the case for a
    #: harness whose output *might* be JSON but whose structure cannot be
    #: promised: a custom command, a wrapper script, an agent behind a shell
    #: alias. Setting ``True`` here while leaving ``has_structured_mode`` at
    #: ``False`` says "try to parse, but do not advertise" — which is the honest
    #: position for an adapter that does not know what it is talking to.
    #:
    #: It also selects line assembly. JSONL is line-delimited by definition, so a
    #: frame split across two reads has to be rejoined before it can parse; see
    #: :class:`~openburrow.adapters.base._ChunkAssembler`.
    parses_json_frames: bool | None = None
    #: True when the harness can resume a prior session.
    is_resumable: bool = False
    #: True when the harness exposes MCP tools natively.
    mcp_native: bool = False

    def __init__(self, settings: Settings, *, lane: Lane | None = None) -> None:
        super().__init__(settings, lane=lane)
        self._pending_prompt: str | None = None

    # --- declaration -------------------------------------------------------
    @property
    def capabilities(self) -> HarnessCapabilities:
        return HarnessCapabilities(
            structured_output=self.has_structured_mode,
            streaming=True,
            resumable=self.is_resumable,
            mcp_tools=self.mcp_native,
            supports_interrupt=True,
            native_a2a=False,
        )

    @property
    def _parses_json(self) -> bool:
        """Resolve whether to attempt JSON parsing.

        Separate from :attr:`has_structured_mode` on purpose — see the module
        docstring. The default inherits the declaration, so an adapter that only
        ever wanted one flag still declares one flag; an adapter that needs the
        two answers to differ sets :attr:`parses_json_frames` explicitly.

        Deliberately not a cached attribute: an adapter may flip it at runtime —
        ``OpenCodeAdapter`` degrades when its server fails to start — and a value
        snapshotted in ``__init__`` would keep parsing frames the harness has
        stopped emitting.
        """
        if self.parses_json_frames is not None:
            return self.parses_json_frames
        return self.has_structured_mode

    # --- spawn -------------------------------------------------------------
    def build_spawn_spec(self, lane: Lane) -> SpawnSpec:
        worktree = Path(lane.worktree_path) if lane.worktree_path else Path.cwd()
        command = [self.resolve_binary(), *self.base_args]

        if self.has_structured_mode and self.settings.adapter_structured_output != "never":
            command.extend(self.structured_args)

        # Per-lane extra args come from the lane template in openburrow.yaml.
        extra = lane.metadata.get("extra_args") or []
        command.extend(str(arg) for arg in extra)

        env = self.base_env(lane)
        env.update(self.extra_env(lane))

        return SpawnSpec(
            command=command,
            cwd=worktree,
            env=env,
            use_pty=True,
            stdin_pipe=True,
        )

    def extra_env(self, lane: Lane) -> dict[str, str]:
        """Harness-specific environment: exactly the credentials the lane granted.

        The default returns :meth:`~openburrow.adapters.base.HarnessAdapter.granted_env`
        — the intersection of what this adapter declared in
        :attr:`~openburrow.adapters.base.HarnessAdapter.credential_env` and what
        the lane's template granted. Override this only to add *non-credential*
        harness-specific variables, and call ``super().extra_env(lane)`` so the
        granted credentials survive.

        An override that reads ``os.environ`` directly is what broke isolation
        before: seven adapters did exactly that, each looking correct in
        isolation, and none of them consulting the lane. Such an override would
        now be stripped again by
        :meth:`~openburrow.adapters.base.HarnessAdapter.enforce_env_passthrough`,
        which is worse than useless — it would look like it worked. Declare the
        variables in ``credential_env`` instead and let the lane decide.
        """
        return self.granted_env(lane)

    # --- interaction -------------------------------------------------------
    async def send_prompt(self, lane: Lane, prompt: str) -> None:
        """Write a prompt to the harness.

        Held in ``_pending_prompt`` as well as written, so a harness that dies
        between write and read can have its prompt recovered by the checkpoint
        layer rather than silently losing the work.

        The terminator differs by transport, and getting it wrong is not a
        cosmetic difference. A PTY is a terminal: the Enter key sends **CR**,
        and the line discipline translates it to LF for the reading process.
        Writing a bare LF to the master instead delivers the text to the
        terminal's input buffer without ever submitting the line, so the harness
        sits in ``readline()`` forever waiting for an Enter that never comes.

        Measured on this platform against a real child, with the same prompt:

        ============  ==================
        ``"hello\\n"``   child never responded
        ``"hello\\r"``   child responded
        ``"hello\\r\\n"``  child responded
        ============  ==================

        A pipe wants LF, because there is no terminal to translate anything. So
        the two transports get different terminators, and the PTY path — which
        had never run — gets the one that actually submits a line.
        """
        self._pending_prompt = prompt

        if self._pty is not None:
            _pty_write(self._pty, prompt + "\r")
            return
        if self.process is not None and self.process.stdin is not None:
            payload = (prompt + "\n").encode("utf-8")
            self.process.stdin.write(payload)
            await self.process.stdin.drain()
            return
        raise RuntimeError(f"{self.name} is not running")

    async def read_output(self) -> AsyncIterator[HarnessOutput]:
        """Read output, preferring structured frames and falling back to parsing."""
        if self._pty is not None:
            async for output in self._read_pty():
                yield output
        elif self.process is not None and self.process.stdout is not None:
            async for output in self._read_pipe():
                yield output

    async def _read_pty(self) -> AsyncIterator[HarnessOutput]:
        """Read the PTY and interpret each unit.

        The reading itself lives in
        :meth:`~openburrow.adapters.base.HarnessAdapter._iter_pty_text`, which
        owns the reader thread, the backend type differences, the ANSI
        stripping, and the line assembly. It moved there because
        ``OpenCodeAdapter`` is not a CLI scraper and does not inherit this class,
        so it had grown its own copy of the loop — blocking read, unconditional
        ``.decode()``, and all.

        ``line_delimited`` is :attr:`_parses_json`: a harness whose frames are
        JSON gets whole lines, and one that emits a TUI gets raw reads. It read
        ``has_structured_mode``, which happens to agree for every adapter that
        declares structured output — and is wrong for exactly the adapters that
        set the two flags differently, which is the case this distinction was
        introduced for.
        """
        async for unit in self._iter_pty_text(line_delimited=self._parses_json):
            for output in self._interpret(unit):
                self.buffer_output(output)
                yield output

    async def _read_pipe(self) -> AsyncIterator[HarnessOutput]:
        """Read line-by-line from a plain pipe.

        No assembler here: ``readline`` already returns whole lines, which is
        the alignment the assembler exists to reconstruct for a PTY.
        """
        assert self.process is not None and self.process.stdout is not None
        while True:
            line = await self.process.stdout.readline()
            if not line:
                break
            for output in self._interpret(_as_text(line)):
                self.buffer_output(output)
                yield output

    # --- interpretation ----------------------------------------------------
    def _interpret(self, text: str) -> list[HarnessOutput]:
        """Turn a raw chunk into zero or more structured outputs.

        The order is fixed, and it is the order the roadmap documents. It is not
        configurable, and that is a decision rather than an omission: a
        configurable order would let two installations interpret the same harness
        output differently, which makes a bug report untriable.

        ==================  =====================================================
        1. JSON             a harness in structured mode emits JSON and nothing
                            else
        2. diff             ``---``/``+++``/``@@`` at line start is unambiguous
        3. usage limit      a provider limit is terminal — and by this point it
                            cannot be a substring of something better
        4. plan             three or more numbered or bulleted steps
        5. plain text       the honest fallback
        ==================  =====================================================

        This used to run the usage-limit check **first**, which inverted the
        documented order. The consequence was not a mislabelled message. The
        check is a substring test over the whole chunk, so a diff that happened
        to contain the digits ``429`` — a hunk header, a port, a byte count —
        was replaced by a terminal error and the lane's real output was thrown
        away. Recognising the two unambiguous shapes first means a limit marker
        can only fire on content that is not already recognisable as something
        better.

        The trade-off that buys, stated plainly: a limit notice arriving in the
        same chunk as a diff is now missed rather than reported. That is the
        cheaper error. A missed notice costs one lane that was about to fail
        anyway and whose next chunk usually repeats the notice; a false notice
        costs the work, silently, and marks the stream terminal while doing it.

        Control sequences are stripped before any of this runs, so a diff that a
        colour-capable TUI wrapped in SGR codes is still recognised as a diff.
        See :func:`_strip_ansi` for why that is not merely cosmetic.

        The JSON gate is :attr:`_parses_json`, not ``has_structured_mode``. The
        two agree for every adapter that declares structured output and disagree
        for every adapter that cannot — and for those, using the declaration as
        the gate meant the JSON branch was dead code. A harness emitting JSONL
        while declaring no structure had all of its frames classified ``text``,
        a kind the bus does not broadcast, so it produced a running lane that
        published nothing.

        On the ``structured`` flag: the JSON path yields ``True``, because those
        frames really did come from the harness in machine-readable form. Every
        *regex-derived* path below yields ``False``. The flag means "we mapped
        this precisely", not "we recognised this"; a diff recovered by
        pattern-matching is a good guess, and over-claiming it would corrupt the
        one number that proves a message changed a lane's behaviour.

        That is also why ``structured=True`` on an output is not in tension with
        ``capabilities.structured_output == False`` on the same adapter. The
        capability says "this harness can be relied on to produce structure"; the
        output flag says "this particular frame was structure". A custom command
        can be the second without being the first, and collapsing the two is the
        bug this method used to have.
        """
        outputs: list[HarnessOutput] = []
        stripped = _strip_ansi(text).strip()
        if not stripped:
            return outputs

        if self._parses_json:
            for payload in self._extract_json_objects(stripped):
                outputs.append(self._from_json(payload))
            if outputs:
                return outputs

        if _DIFF_HEADER.search(stripped):
            return [
                HarnessOutput(
                    kind="diff",
                    text=stripped,
                    structured=False,
                    artifacts=[TaskArtifact.diff(stripped)],
                )
            ]

        limit = self.detect_usage_limit(stripped)
        if limit:
            return [
                HarnessOutput(
                    kind="error",
                    text=stripped,
                    structured=False,
                    terminal=True,
                    data={"usage_limit": limit},
                )
            ]

        steps = _STEP_LINE.findall(stripped)
        if len(steps) >= _PLAN_MIN_STEPS:
            return [
                HarnessOutput(
                    kind="plan",
                    text=stripped,
                    structured=False,
                    data={"steps": steps},
                )
            ]

        return [HarnessOutput(kind="text", text=stripped, structured=False)]

    @staticmethod
    def _extract_json_objects(text: str) -> list[dict[str, Any]]:
        """Pull complete JSON objects out of a text stream.

        Handles the common case of one JSON object per line (JSONL), which is
        what most harnesses emit in structured mode.
        """
        found: list[dict[str, Any]] = []
        for line in text.splitlines():
            candidate = line.strip()
            if not (candidate.startswith("{") and candidate.endswith("}")):
                continue
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                found.append(parsed)
        if found:
            return found
        match = _JSON_BLOB.search(text)
        if match:
            try:
                parsed = json.loads(match.group(0))
                if isinstance(parsed, dict):
                    return [parsed]
            except json.JSONDecodeError:
                pass
        return []

    def _from_json(self, payload: dict[str, Any]) -> HarnessOutput:
        """Map a harness's JSON frame onto a :class:`HarnessOutput`.

        Subclasses override this when their harness has a distinctive schema —
        usually to reclassify ``kind``, since the base cannot know that one
        harness's ``"assistant"`` frame is another's terminal ``"result"``.

        The base recognises the field names most CLIs converge on (see
        :data:`_TEXT_KEYS`), plus the two structural shapes that are not field
        names: nested content and nested usage. Both were wrong before this, and
        both failed silently — a Python ``repr`` where prose belonged, and a
        usage object that parsed but recorded zero.

        When a frame maps to nothing at all, the keys it *did* carry are attached
        under :data:`_UNMAPPED_KEY` and logged. The frame is still dropped before
        broadcast — it has no content to broadcast — but the drop stops being
        invisible, which is the difference between "this harness is quiet" and
        "we do not understand this harness".
        """
        kind = str(payload.get("type") or payload.get("kind") or "result")

        # First candidate field that actually yields text. An ``or`` chain was
        # wrong here: a present-but-empty ``message`` dict is truthy, so it won
        # the chain and then flattened to nothing, masking a ``result`` field
        # further down that held the real answer.
        text = ""
        for key in _TEXT_KEYS:
            flattened = _flatten_content(payload.get(key))
            if flattened:
                text = flattened
                break

        artifacts: list[TaskArtifact] = []
        if kind in {"diff", "patch"} or "diff" in payload:
            diff_text = _flatten_content(payload.get("diff")) or text
            artifacts.append(TaskArtifact.diff(diff_text))
            kind = "diff"

        usage = _extract_usage(payload)
        if usage is not None and self.lane is not None:
            self.lane.record_usage(
                tokens_in=_first_int(usage, _USAGE_IN_KEYS),
                tokens_out=_first_int(usage, _USAGE_OUT_KEYS),
            )

        if payload.get("is_error") and kind != "error":
            kind = "error"

        terminal = bool(payload.get("done") or payload.get("complete") or payload.get("final"))

        data = payload
        if not text and not artifacts:
            unmapped = sorted(str(key) for key in payload)
            log.warning(
                "adapter.unmapped_frame",
                adapter=self.name,
                kind=kind,
                keys=unmapped,
                hint=(
                    "This frame carried no recognisable text field. Add the "
                    "harness's field name to _TEXT_KEYS or override _from_json."
                ),
            )
            data = {**payload, _UNMAPPED_KEY: unmapped}

        return HarnessOutput(
            kind=kind,
            text=text,
            structured=True,
            terminal=terminal,
            artifacts=artifacts,
            data=data,
        )

    def parse_usage(self, text: str) -> dict[str, Any]:
        """Look for a usage object anywhere in the output.

        Uses the same nested lookup as :meth:`_from_json`, so a Claude Code
        assistant frame — which carries usage at ``message.usage`` — reports its
        tokens instead of ``{}``.
        """
        for payload in self._extract_json_objects(_strip_ansi(text)):
            usage = _extract_usage(payload)
            if usage is not None:
                return usage
        return {}


__all__ = ["STRUCTURED_FLAG_CANDIDATES", "GenericCliAdapter"]
