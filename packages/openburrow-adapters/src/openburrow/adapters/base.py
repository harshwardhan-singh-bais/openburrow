"""The Harness Adapter Protocol.

An adapter is the only place in OpenBurrow that knows anything harness-specific.
Everything above it — the bus, the lifecycle, the governance ledger — treats all
lanes identically, because the adapter's job is precisely to make them identical.

The five operations every adapter must implement:

=================  =========================================================
``start``          spawn the harness in a lane's worktree
``send_prompt``    deliver text to the harness in whatever form it accepts
``read_output``    get structured output back, or a best-effort parse of it
``stop``           terminate cleanly, escalating to a kill if needed
``status``         report liveness and the harness's own notion of state
=================  =========================================================

Plus two translation hooks that only exist because harnesses differ:

``translate_output``  native output (plan text, diff, tool log) -> A2A message
``inject_message``    incoming A2A message -> whatever this harness accepts

The translation pair is where the real work is, and where the honest fallback
lives: a harness with structured output gets a precise mapping, and a black-box
harness gets text parsing plus a cheap classifier. The adapter declares which it
is via :class:`~openburrow.core.models.HarnessCapabilities`, and the governance
layer spot-checks that declaration against observed behaviour.
"""

from __future__ import annotations

import asyncio
import os
import re
import signal
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openburrow.a2a.card import HarnessCapabilities, SkillSpec
from openburrow.core.config.settings import Settings
from openburrow.core.errors import (
    AdapterError,
    HarnessNotFoundError,
    InjectionError,
)
from openburrow.core.logging import get_logger
from openburrow.core.models import (
    A2ATask,
    BusMessage,
    Lane,
    LaneStatus,
    TaskArtifact,
)

log = get_logger(__name__)

#: Phrases a provider uses to say "you are out of budget". Matched on word
#: boundaries, and the ``-ing`` form of "rate limiting" is excluded on purpose:
#: see :meth:`HarnessAdapter.detect_usage_limit`.
_LIMIT_PHRASE = re.compile(
    r"\b(?:"
    r"rate[\s_-]?limit(?:s|ed)?"
    r"|quota[\s_-]?(?:exceeded|reached)"
    r"|usage[\s_-]?limit"
    r"|too many requests"
    r"|out of credits"
    r"|insufficient[\s_-]?quota"
    r"|throttl(?:e|ed)"
    r")\b",
    re.IGNORECASE,
)

#: An HTTP status code only counts when something marks it as one.
_LIMIT_CODE = re.compile(
    r"\b(?:http|https|status|code|error|response|api|got|returned)\b[\s:=\-]{0,4}429\b"
    r"|\b429\b[\s:,\-]{0,4}(?:too many|rate|throttl)",
    re.IGNORECASE,
)

#: The signal used by ``stop(force=True)``.
#:
#: ``signal.SIGKILL`` is a POSIX constant and does not exist on Windows, where
#: the module exposes only SIGABRT/SIGBREAK/SIGFPE/SIGILL/SIGINT/SIGSEGV/
#: SIGTERM. Referencing it directly raised ``AttributeError`` *inside* the
#: shutdown ``try``, so the failure was logged as ``pty_stop_failed`` and the
#: PTY reference was dropped anyway — the harness kept running while ``stop``
#: reported success. Falling back to SIGTERM is the strongest signal Windows
#: actually has, and both PTY backends escalate internally.
_KILL_SIGNAL = getattr(signal, "SIGKILL", signal.SIGTERM)


def _pty_write(pty: Any, text: str) -> None:
    """Write text to a PTY, matching the backend's expected argument type.

    The two PTY backends disagree about strings in both directions.
    ``ptyprocess`` writes and reads ``bytes``; ``pywinpty`` writes and reads
    ``str``, and rejects bytes outright::

        TypeError: argument 'to_write': 'bytes' object is not an instance of 'str'

    This is the mirror of the read side, where ``ptyprocess`` returns bytes and
    ``pywinpty`` returns str. Both directions need the same care, and neither
    was handled — the write path had never run because the Windows PTY branch
    was unreachable, so the asymmetry stayed hidden.

    Selected on the backend's module name rather than by catching ``TypeError``
    and retrying. A ``TypeError`` raised from inside the child's own echo path is
    indistinguishable from a wrong argument type, and a retry would then write
    the prompt twice.
    """
    if type(pty).__module__.startswith("winpty"):
        pty.write(text)
    else:
        pty.write(text.encode("utf-8"))


#: Terminal control sequences: CSI, OSC, and two-character escapes.
_ANSI = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"  # CSI ... final byte
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC ... BEL or ST
    r"|\x1b[@-Z\\-_]"  # two-character escapes
)


def _strip_ansi(text: str) -> str:
    """Remove terminal control sequences and normalise line endings.

    A PTY is not a pipe: the child sees a terminal and emits what a terminal
    understands. Captured live from this platform, the first read of a Windows
    PTY child was ``'\\x1b[1t'``, then ``'\\x1b[c\\x1b[?1004h\\x1b[?9001h'`` —
    the terminal's own capability queries — before a single byte of real output.
    A colour-capable TUI additionally wraps every line in SGR codes.

    Both break the classifiers, and the second one breaks them silently. A diff
    line that should read ``--- a/x.py`` arrives as ``\\x1b[31m--- a/x.py\\x1b[0m``;
    a ``^``-anchored diff pattern no longer matches, and a real diff is filed as
    plain text. Stripping happens *before* classification so the classifiers see
    what a human would.

    ``\\r`` is normalised as well. PTY line endings are CRLF — observed as
    ``'\\r\\nsecond line'`` — and a stray CR left inside a diff artifact corrupts
    it for every consumer downstream.

    One definition, in the module that owns the PTY. An adapter that reimplements
    this gets a slightly different regex and a slightly different bug; Crush did.

    Fast-pathed because the common case is a clean chunk and this runs on every
    read.
    """
    if "\x1b" not in text and "\r" not in text:
        return text
    cleaned = _ANSI.sub("", text)
    return cleaned.replace("\r\n", "\n").replace("\r", "")


def _as_text(chunk: Any) -> str:
    """Normalise one PTY read to text.

    ``ptyprocess`` returns ``bytes``; ``pywinpty`` returns ``str``. Both are
    real: the Windows probe returned ``type=str`` for every read. Calling
    ``chunk.decode("utf-8")`` unconditionally raises
    ``AttributeError: 'str' object has no attribute 'decode'`` on the very first
    read of every lane on Windows.
    """
    if isinstance(chunk, bytes):
        return chunk.decode("utf-8", errors="replace")
    return str(chunk)


def _is_alive(pty: Any) -> bool:
    """``isalive()`` on a closed or reaped PTY raises; any failure means gone."""
    try:
        return bool(pty.isalive())
    except Exception:  # liveness must never raise
        return False


#: Name fragments that mark an environment variable as a credential.
#:
#: ``CREDENTIAL`` is in the list for one concrete case:
#: ``GOOGLE_APPLICATION_CREDENTIALS`` holds a *path to* a service-account file
#: rather than a secret, so a scan for secrets by value would never see it — and
#: a process handed that variable can read the file. A name that says
#: "credentials" is the only signal available, and it is a good one.
_CREDENTIAL_TOKENS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")


def is_credential_key(name: str) -> bool:
    """True when an environment variable name looks like a credential.

    Deliberately one predicate with two callers, and that is the point. It
    decides what ``SpawnSpec.redacted()`` **hides from logs** and what
    :meth:`HarnessAdapter.enforce_env_passthrough` **refuses to pass to a
    process**. Two separate heuristics would drift, and the drift would be
    invisible in exactly the wrong direction: a key that gets redacted from a log
    but handed to a harness anyway.

    Name-based rather than value-based because there is nothing else to go on.
    ``OPENAI_API_KEY`` and ``OPENAI_BASE_URL`` are distinguishable by name and
    not by content, and a secrets scanner that reads values would have to read
    every value — including the ones it is supposed to protect.

    The false-positive direction is the safe one: a variable named ``KEYBOARD``
    is treated as a credential and dropped unless a lane explicitly grants it.
    The fix is one line of YAML, and the failure is loud (see
    :meth:`HarnessAdapter.enforce_env_passthrough`), which is the right way round
    for a mechanism whose job is to withhold.
    """
    upper = name.upper()
    return any(token in upper for token in _CREDENTIAL_TOKENS)


class _ChunkAssembler:
    """Reassemble a raw PTY stream into units an interpreter can parse.

    A PTY read returns whatever happened to be available, and that is not
    aligned to anything. Measured on this platform, a child that printed two
    lines arrived as ``'hello from child'`` and then ``'\\r\\nsecond line'`` —
    the line boundary fell *between* two reads.

    That is harmless for regex classifiers, which are content-addressed and happy
    to see a fragment. It is fatal for a JSON one. A structured harness emits
    JSONL, so the frame is the unit, and any parser requiring a line that both
    starts with ``{`` and ends with ``}`` correctly refuses a fragment. Real
    ``stream-json`` frames are ~1.7 KB against a 4096-byte read, so frames landed
    split, every line failed the "ends with ``}``" test, and *zero* frames
    parsed. The harness looked silent while it was in fact talking, and the
    failure was invisible because an empty interpretation is not an error.

    So the unit depends on the mode:

    * **line-delimited** — accumulate until a newline, because JSONL is
      line-delimited by definition.
    * **otherwise** — emit each read as it arrives. A diff or a plan is not
      line-delimited, and splitting one would turn a single diff into a dozen
      one-line "diffs", each with its own artifact.

    ``max_buffer`` bounds the buffer so a harness emitting one very long line
    cannot grow it without limit; on overflow the buffer is released as-is, which
    is no worse than not assembling at all.
    """

    __slots__ = ("_buffer", "_line_mode", "_max_buffer")

    def __init__(self, *, line_mode: bool, max_buffer: int = 1 << 20) -> None:
        self._line_mode = line_mode
        self._buffer = ""
        self._max_buffer = max_buffer

    def feed(self, text: str) -> list[str]:
        """Add a read; return the complete units it completed."""
        if not text:
            return []
        if not self._line_mode:
            return [text]

        self._buffer += text
        units: list[str] = []
        while "\n" in self._buffer:
            line, _, self._buffer = self._buffer.partition("\n")
            if line.strip():
                units.append(line)
        if len(self._buffer) > self._max_buffer:
            units.append(self._buffer)
            self._buffer = ""
        return units

    def flush(self) -> list[str]:
        """Release a trailing partial unit at end of stream."""
        if not self._buffer:
            return []
        remainder, self._buffer = self._buffer, ""
        return [remainder] if remainder.strip() else []


@dataclass(slots=True)
class HarnessOutput:
    """One chunk of interpreted harness output.

    ``structured`` is the honesty flag: True when the harness emitted something
    machine-readable and the adapter mapped it precisely, False when the adapter
    guessed. Consumers can weight the two differently — the metrics rollup does,
    because "messages that changed behaviour" is only meaningful for structured
    output.
    """

    kind: str = "text"  # text | plan | diff | tool-call | status | error | result
    text: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    structured: bool = False
    at: float = field(default_factory=time.time)
    #: Set when this output closes a unit of work.
    terminal: bool = False
    artifacts: list[TaskArtifact] = field(default_factory=list)

    def to_bus_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "text": self.text[:8192],
            "structured": self.structured,
            "terminal": self.terminal,
            "at": self.at,
        }


@dataclass(slots=True)
class SpawnSpec:
    """Everything needed to launch a harness, resolved before spawn.

    Resolving this separately from launching is what lets ``burrow doctor``
    report "the harness would spawn with this command" without actually
    spawning it — and what lets the policy gate inspect the command before it
    runs, which is the whole point of a pre-execution gate.
    """

    command: list[str]
    cwd: Path
    env: dict[str, str]
    #: True when the harness wants a PTY (interactive TUIs). False for
    #: server-mode harnesses that speak HTTP and only need a plain process.
    use_pty: bool = True
    stdin_pipe: bool = True

    def redacted(self) -> dict[str, Any]:
        """Safe for logs and `burrow doctor`: env values replaced by presence flags."""
        return {
            "command": self.command,
            "cwd": str(self.cwd),
            "use_pty": self.use_pty,
            "env_keys": sorted(self.env),
            "env_secrets_set": sorted(key for key in self.env if is_credential_key(key)),
        }


class HarnessAdapter(ABC):
    """Base class for every harness adapter.

    Subclasses implement the five operations plus the two translation hooks.
    Everything else — health checks, crash policy, usage accounting, output
    buffering — is provided here so a new adapter is a few dozen lines rather
    than a few hundred.
    """

    #: Registry key. Must match the ``harness:`` value in ``openburrow.yaml``.
    name: str = "base"
    #: Human-readable description shown in `burrow adapters list`.
    description: str = ""
    #: The binary this adapter needs on PATH.
    binary: str = ""
    #: Env var that overrides the binary path.
    binary_env: str = ""
    #: URL of the harness's own docs, surfaced in error hints.
    docs_url: str = ""
    #: Credential environment variables this harness understands.
    #:
    #: A **declaration of need**, not a grant. Declaring ``OPENAI_API_KEY`` here
    #: says "this harness can use an OpenAI key"; it does not say this lane gets
    #: one. The lane grants it by naming the variable in its template's
    #: ``env_passthrough``. See :meth:`granted_env` and
    #: :meth:`enforce_env_passthrough`.
    credential_env: tuple[str, ...] = ()

    def __init__(self, settings: Settings, *, lane: Lane | None = None) -> None:
        self.settings = settings
        self.lane = lane
        self.process: asyncio.subprocess.Process | None = None
        self._pty: Any = None
        self._started_at: float = 0.0
        self._output_buffer: list[HarnessOutput] = []
        self._buffer_limit = max(16, settings.adapter_output_buffer_kb // 4)

    # --- declaration -------------------------------------------------------
    @property
    @abstractmethod
    def capabilities(self) -> HarnessCapabilities:
        """What this harness can truthfully do. Spot-checked by governance."""

    def skills(self) -> list[SkillSpec]:
        """Extra A2A skills beyond the base set. Default: none."""
        return []

    def effective_capabilities(self) -> HarnessCapabilities:
        """Capabilities as they are *right now*, not as declared.

        ``capabilities`` is a declaration: what this harness can do at its best.
        It is what ``burrow adapters list`` prints and what the Agent Card is
        built from. What the bus and the governance layer need, though, is the
        current truth — an adapter that has *degraded* must not keep advertising
        the capability it lost, or the capability-mismatch detector can never
        fire on it.

        The default returns the declaration, because most adapters cannot
        degrade. Override it when yours can: ``OpenCodeAdapter`` does, since its
        headless server may fail to start and drop it into PTY mode.

        This method existed on that one adapter, documented as the thing the
        governance layer compares against, and was called by nothing — the Agent
        Card was built from ``adapter.capabilities`` directly. A degradation hook
        nobody consults is worse than no hook, because the docstring reads as
        though the problem is already handled.
        """
        return self.capabilities

    def resolve_binary(self) -> str:
        """Locate the harness binary, honouring the env override."""
        override = os.environ.get(self.binary_env, "").strip() if self.binary_env else ""
        return override or self.binary

    # --- spawn planning ----------------------------------------------------
    @abstractmethod
    def build_spawn_spec(self, lane: Lane) -> SpawnSpec:
        """Compute the command, cwd, and env for a lane.

        Must be pure: no side effects, no spawning. The policy gate calls this
        to inspect a command before anything executes.
        """

    def base_env(self, lane: Lane) -> dict[str, str]:
        """Build the non-credential environment slice for a lane.

        Only plumbing lives here — ``PATH``, ``HOME``, ``TERM``, and the
        ``OPENBURROW_*`` identity of the lane — plus the variables the lane's
        template granted. Credentials are added by
        :meth:`granted_env` and are subject to enforcement in
        :meth:`enforce_env_passthrough`.

        This docstring used to claim more than the code did::

            Credential isolation happens here and it is not optional: a lane
            gets the ambient environment minus every other lane's credentials,
            plus only the passthrough vars its template declared.

        The first clause described a mechanism that does not exist — there is no
        per-lane credential store, so "another lane's credentials" was not a
        thing a lane could be given or denied. The second clause was simply
        false: ``base_env`` did add only the declared passthrough vars, and then
        every adapter's ``extra_env`` read ``os.environ`` directly and handed
        the harness whatever provider keys it found. A Crush lane received the
        Anthropic and OpenAI keys whether or not its template mentioned them.

        So the property that is now true, and checkable, is the second clause:
        **a lane receives a credential variable only if its template granted
        that variable.** The grant is enforced centrally, after the adapter has
        finished building, in :meth:`enforce_env_passthrough`.
        """
        env: dict[str, str] = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", os.environ.get("USERPROFILE", "")),
            "TERM": self.settings.adapter_term,
            "OPENBURROW_LANE_ID": lane.id,
            "OPENBURROW_SESSION_ID": lane.session_id,
            "OPENBURROW_HARNESS": self.name,
            "OPENBURROW_ROLE": str(lane.role),
        }
        for key in lane.env_passthrough:
            value = os.environ.get(key)
            if value is not None:
                env[key] = value
        if self.settings.env == "development":
            env.setdefault("OPENBURROW_ENV", "development")
        return env

    def granted_env(self, lane: Lane) -> dict[str, str]:
        """Credentials this lane has granted *and* this harness declared.

        The intersection, not either set alone:

        * declared-but-not-granted is the least-privilege case — the harness
          could use the key, the lane was not given it, so it is withheld;
        * granted-but-not-declared is not passed either, because a lane naming a
          variable its harness does not understand is more likely a typo in a
          template than a deliberate grant, and ``base_env`` has already added
          it if the lane really did name it.

        Adapters should call this instead of reading ``os.environ`` themselves.
        Seven of them used to hand-roll the same loop, which is how the
        enforcement came to be bypassed in the first place: each loop looked
        correct on its own and none of them consulted the lane.
        """
        granted = set(lane.env_passthrough)
        return {
            key: os.environ[key]
            for key in self.credential_env
            if key in granted and os.environ.get(key)
        }

    def enforce_env_passthrough(self, spec: SpawnSpec, lane: Lane) -> SpawnSpec:
        """Drop any credential the lane did not grant. The backstop.

        Called from :meth:`start` on the finished spec, so it applies to *every*
        adapter whether or not that adapter cooperates. That placement is
        deliberate: the leak this fixes was not a mistake in one adapter, it was
        the absence of a choke point. Seven adapters each read ``os.environ``
        correctly by their own lights, and the only way to make the isolation
        claim true is to check the result rather than audit the authors.

        It also catches credentials that never went through ``extra_env`` at
        all — ``OpenCodeAdapter`` takes its key from ``settings``, which reads
        the same ambient environment.

        Enforcement drops and warns rather than raising. A hard failure here
        would take down a lane whose template merely forgot a line, and the
        diagnostic is more useful than the crash: the warning names the exact
        variable and the exact YAML to add. The harness is left to fail on its
        own authentication, which produces an error message the operator can act
        on — and which cannot be mistaken for OpenBurrow refusing to start.

        Mutates ``spec.env`` in place and returns the spec, because ``start``
        owns it from here and the policy gate must see the enforced result.
        """
        granted = set(lane.env_passthrough)
        withheld = sorted(key for key in spec.env if is_credential_key(key) and key not in granted)
        if not withheld:
            return spec

        for key in withheld:
            del spec.env[key]

        log.warning(
            "adapter.credential_withheld",
            adapter=self.name,
            lane_id=lane.id,
            variables=withheld,
            hint=(
                "These look like credentials and the lane's template did not "
                "grant them. Add the ones this lane needs to the template's "
                "`env_passthrough:` list. See the credential isolation notes in "
                "docs/architecture/."
            ),
        )
        return spec

    def prepare_spawn_spec(self, lane: Lane) -> SpawnSpec:
        """Build the spawn plan and apply credential enforcement to it.

        The single producer of a *finished* spec. Both paths use it — the daemon
        calls it so the policy gate can inspect the exact argv that will be
        executed, and :meth:`start` calls it when no spec was supplied — so the
        gated plan and the executed plan cannot drift apart. A gate that inspects
        a second, separately-built plan is inspecting a different command than
        the one that runs.
        """
        return self.enforce_env_passthrough(self.build_spawn_spec(lane), lane)

    # --- lifecycle ---------------------------------------------------------
    async def start(self, lane: Lane, *, spec: SpawnSpec | None = None) -> None:
        """Spawn the harness for ``lane`` and wait until it accepts input.

        ``spec`` lets a caller that has already built and inspected the plan pass
        it back in, which is how the daemon gets the policy gate in front of the
        spawn without the adapter having to know that policy exists. Omitting it
        builds a fresh plan, so every existing call site is unaffected.
        """
        spec = spec if spec is not None else self.prepare_spawn_spec(lane)
        self.lane = lane

        # Credential isolation is enforced inside ``prepare_spawn_spec``, on the
        # finished spec, rather than inside each builder. One choke point every
        # adapter must pass through is what makes the property checkable;
        # auditing seven hand-written ``os.environ`` loops is what let it be
        # bypassed.
        #
        # Order matters: the spec is complete before anything reads it, so a
        # caller gating the plan sees the environment the process will actually
        # receive, not the builder's unenforced draft.
        spec = self.enforce_env_passthrough(spec, lane)

        if not self._binary_exists(spec.command[0]):
            raise HarnessNotFoundError(
                f"harness binary not found: {spec.command[0]}",
                hint=(
                    f"Install {self.name}"
                    + (
                        f", or set {self.binary_env} to its absolute path."
                        if self.binary_env
                        else "."
                    )
                    + (f" Docs: {self.docs_url}" if self.docs_url else "")
                ),
                context={"adapter": self.name, "command": spec.command},
            )

        spec.cwd.mkdir(parents=True, exist_ok=True)
        log.info(
            "adapter.starting",
            adapter=self.name,
            lane_id=lane.id,
            command=spec.command,
            cwd=str(spec.cwd),
        )

        try:
            # ``use_pty`` alone decides. This read
            # ``spec.use_pty and not self._is_windows()``, which excluded the
            # PTY path on Windows at the *call site* — so fixing the backend
            # inside ``_start_with_pty`` would still have left it unreachable.
            # The exclusion was the reason ``use_pty=True`` did nothing on
            # Windows, and it was invisible because the pipe fallback also
            # produces a working process.
            if spec.use_pty:
                await self._start_with_pty(spec)
            else:
                await self._start_plain(spec)
        except FileNotFoundError as exc:
            raise HarnessNotFoundError(
                f"could not execute {spec.command[0]}: {exc}",
                context={"adapter": self.name, "command": spec.command},
                cause=exc,
            ) from exc

        self._started_at = time.time()
        lane.status = LaneStatus.STARTING
        lane.pid = self.process.pid if self.process else None
        lane.command = spec.command
        lane.started_at = (
            lane.started_at or __import__("openburrow.core.models.base", fromlist=["now"]).now()
        )
        await self._after_start(lane)
        lane.status = LaneStatus.IDLE
        log.info("adapter.started", adapter=self.name, lane_id=lane.id, pid=lane.pid)

    async def _start_plain(self, spec: SpawnSpec) -> None:
        self.process = await asyncio.create_subprocess_exec(
            *spec.command,
            cwd=str(spec.cwd),
            env=spec.env,
            stdin=asyncio.subprocess.PIPE if spec.stdin_pipe else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    async def _start_with_pty(self, spec: SpawnSpec) -> None:
        """Spawn under a PTY so interactive harnesses behave as if run by hand.

        Interactive harnesses detect a TTY and change behaviour — line
        buffering, colour codes, prompt handling. Spawning them on a plain pipe
        makes them silently degrade, so a PTY is the default and the plain path
        is the exception for server-mode harnesses.

        Two backends, because a PTY is a platform primitive rather than a
        portable one: ``ptyprocess`` on POSIX and ``pywinpty`` on Windows (whose
        module is imported as ``winpty``). Both are declared in this package's
        ``pyproject.toml`` behind ``sys_platform`` markers, and both present the
        same small surface — ``spawn``/``read``/``write``/``isalive``/
        ``terminate``/``kill``/``exitstatus``/``close`` — which is why the rest
        of the adapter is written against that shape rather than either library.

        The Windows branch used to be absent: this method tried
        ``import ptyprocess``, caught ``ImportError``, and fell through to a
        plain pipe. On Windows that import always fails, because the dependency
        marker excludes it — so ``use_pty=True``, which
        :meth:`~openburrow.adapters.harnesses.generic.GenericCliAdapter.build_spawn_spec`
        hardcodes, silently degraded to a pipe for *every* harness. That is
        exactly the degradation the PTY exists to prevent, and it was invisible
        because the fallback was also a working process.

        The spawn itself runs in a worker thread. Both libraries spawn
        synchronously, and a synchronous spawn on the daemon's event loop stalls
        every other lane for the duration.
        """
        self._pty = await asyncio.to_thread(self._spawn_pty, spec)
        if self._pty is None:
            await self._start_plain(spec)
            return
        # The PTY owns the process; liveness goes through _pty from here.
        self.process = None

    @staticmethod
    def _spawn_pty(spec: SpawnSpec) -> Any:
        """Create a PTY-backed process, or return ``None`` if no backend exists.

        Returning ``None`` instead of raising keeps the fallback decision in one
        place. A missing PTY backend degrades to a pipe — a working harness with
        reduced fidelity — rather than failing the start outright.
        """
        try:
            import ptyprocess

            return ptyprocess.PtyProcess.spawn(
                spec.command,
                cwd=str(spec.cwd),
                env=spec.env,
                dimensions=(40, 120),
            )
        except ImportError:
            pass

        try:
            import winpty  # provided by the ``pywinpty`` distribution
        except ImportError:
            log.warning(
                "adapter.no_pty_backend",
                hint=(
                    "Neither ptyprocess (POSIX) nor pywinpty (Windows) is installed. "
                    "This harness is running on a plain pipe and may degrade."
                ),
            )
            return None

        return winpty.PtyProcess.spawn(
            spec.command,
            cwd=str(spec.cwd),
            env=spec.env,
            dimensions=(40, 120),
        )

    def _close_pty(self) -> None:
        """Release the PTY handle, then drop the reference.

        The original shutdown path only cleared ``self._pty``, which leaked the
        master descriptor once per lane — invisible in a short test run, and
        descriptor exhaustion in a daemon that cycles lanes for days. Closing is
        also what unblocks a reader thread parked in ``read``.
        """
        pty = self._pty
        self._pty = None
        if pty is None:
            return
        try:
            pty.close(force=True)
        except Exception as exc:  # closing must never raise
            log.debug("adapter.pty_close_failed", error=str(exc))

    # --- PTY reading -------------------------------------------------------
    async def _iter_pty_text(self, *, line_delimited: bool = False) -> AsyncIterator[str]:
        """Yield aligned, control-sequence-free text units from the PTY.

        Lives on the base rather than on one adapter because two adapters need
        it and only one had it. ``OpenCodeAdapter`` extends
        :class:`HarnessAdapter` directly — it is not a CLI scraper — so it never
        inherited :meth:`~openburrow.adapters.harnesses.generic.GenericCliAdapter._read_pty`
        and grew its own copy of the loop, complete with the blocking read and
        the unconditional ``.decode()`` that this method exists to remove. Two
        copies of a subtle reader is one copy too many, and Stage 12's adapters
        would have made it four.

        ``line_delimited`` says whether the harness emits one structured frame
        per line (JSONL). See :class:`_ChunkAssembler` for why that changes the
        unit.

        The blocking-read problem, stated plainly: ``pty.read`` blocks in both
        backends. Called directly from an async generator it parks the daemon's
        entire event loop — one idle harness stops every other lane from being
        scheduled, which is the opposite of what a multi-harness coordinator is
        for. It does not look like a hang either; the loop is alive, it simply
        never advances.

        A reader thread plus a queue fixes it, and it is the same code on both
        platforms. Windows has no ``add_reader`` for PTY handles, so a
        descriptor-based path would need a second implementation that only one
        platform could exercise.

        One reader per lane. The daemon runs one pump per lane, but a second
        concurrent reader would *split* the stream rather than duplicate it, so
        the contract is worth stating: this generator owns the PTY until it is
        closed.

        The thread is never joined. It may be parked inside a blocking ``read``
        when the generator closes, and joining would block the event loop for the
        whole timeout — reintroducing the stall this method exists to remove. It
        is a daemon thread, and ``stop`` closes the PTY, which unblocks the read.
        """
        pty = self._pty
        if pty is None:
            return

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        stop = threading.Event()

        def pump() -> None:
            while not stop.is_set():
                try:
                    chunk = pty.read(4096)
                except (EOFError, OSError, ValueError):
                    break
                except Exception as exc:
                    log.debug("adapter.pty_read_failed", adapter=self.name, error=str(exc))
                    break
                if chunk:
                    loop.call_soon_threadsafe(queue.put_nowait, _as_text(chunk))
                    continue
                if not _is_alive(pty):
                    break
                # No data yet. Yield the thread briefly rather than spinning.
                time.sleep(0.02)
            loop.call_soon_threadsafe(queue.put_nowait, None)

        threading.Thread(target=pump, name=f"pty-read-{self.name}", daemon=True).start()

        assembler = _ChunkAssembler(line_mode=line_delimited)
        try:
            while True:
                chunk = await queue.get()
                if chunk is None:
                    break
                for unit in assembler.feed(_strip_ansi(chunk)):
                    yield unit
            for unit in assembler.flush():
                yield unit
        finally:
            stop.set()

    async def _after_start(self, lane: Lane) -> None:  # noqa: B027 - optional hook
        """Hook for adapters that must do setup after spawn (e.g. wait for HTTP).

        ``async`` because the call site awaits it and because the documented use
        case — waiting for a harness's HTTP server to answer — is inherently
        async. It used to be a plain ``def`` returning ``None`` while the caller
        wrote ``await self._after_start(lane)``, so every adapter start raised
        ``TypeError: object NoneType can't be used in 'await' expression``.
        Nothing overrode it, so the base implementation is the one that ran, and
        no adapter could start at all.

        Deliberately not abstract. Most adapters need nothing here, and marking it
        abstract would force every one of them to write an empty override purely to
        satisfy the base class — which is the boilerplate this hook exists to avoid.
        """

    def _is_windows(self) -> bool:
        import sys

        return sys.platform.startswith("win")

    @staticmethod
    def _binary_exists(binary: str) -> bool:
        """Resolve a binary on PATH without executing it."""
        import shutil

        if Path(binary).is_absolute() or os.sep in binary:
            return Path(binary).exists()
        return shutil.which(binary) is not None

    # --- liveness ----------------------------------------------------------
    @property
    def is_running(self) -> bool:
        if self._pty is not None:
            return bool(self._pty.isalive())
        if self.process is None:
            return False
        return self.process.returncode is None

    @property
    def returncode(self) -> int | None:
        if self.process is not None:
            return self.process.returncode
        if self._pty is not None and not self._pty.isalive():
            return self._pty.exitstatus
        return None

    @property
    def uptime_seconds(self) -> float:
        return time.time() - self._started_at if self._started_at else 0.0

    async def status(self) -> dict[str, Any]:
        """Report liveness plus whatever the harness exposes about itself."""
        return {
            "adapter": self.name,
            "running": self.is_running,
            "pid": self.process.pid if self.process else getattr(self._pty, "pid", None),
            "returncode": self.returncode,
            "uptime_s": round(self.uptime_seconds, 2),
            "lane_status": str(self.lane.status) if self.lane else None,
            "capabilities": self.capabilities.to_dict(),
        }

    async def healthcheck(self) -> tuple[bool, str]:
        """Cheap pre-flight check used by ``burrow doctor`` and on lane start."""
        binary = self.resolve_binary()
        if not self._binary_exists(binary):
            return False, f"{binary!r} not found on PATH"
        return True, f"{binary!r} resolved"

    # --- interaction -------------------------------------------------------
    @abstractmethod
    async def send_prompt(self, lane: Lane, prompt: str) -> None:
        """Deliver a prompt. Must not block until the harness finishes."""

    @abstractmethod
    def read_output(self) -> AsyncIterator[HarnessOutput]:
        """Yield interpreted output chunks as the harness produces them."""

    async def inject_message(self, message: BusMessage, task: A2ATask | None = None) -> bool:
        """Deliver an inbound A2A message into the harness's native input.

        Default strategy, in order of fidelity:

        1. A structured injection hook, if the harness exposes one.
        2. A prompt prefix — the message rendered as context, then the task.
        3. A context file the harness reads on next turn (item 53's fallback:
           the file lands in the lane's working directory, where every harness
           reads *something* — AGENTS.md for the convention-aware ones, the
           repo listing for the rest).

        Returns True when the injection took. A False return is a real signal:
        it means the lane is discoverable on the bus but not actually reachable,
        and the caller should surface that rather than pretend it worked.
        """
        rendered = self.render_injection(message)
        if not rendered.strip():
            return False
        try:
            await self.send_prompt(self.lane, rendered)  # type: ignore[arg-type]
            return True
        except Exception as exc:
            dropped = self.drop_context_file(message, rendered)
            if dropped:
                log.warning(
                    "adapter.inject_via_context_file",
                    adapter=self.name,
                    lane_id=getattr(self.lane, "id", None),
                    path=str(dropped),
                )
                return True
            log.warning(
                "adapter.inject_failed",
                adapter=self.name,
                lane_id=getattr(self.lane, "id", None),
                error=str(exc),
            )
            raise InjectionError(
                f"could not inject a message into {self.name}",
                hint=(
                    "This harness may have no programmatic input hook. "
                    "Check the adapter's capability flags, or use --prompt-only mode."
                ),
                context={"adapter": self.name, "message_id": message.id},
                cause=exc,
            ) from exc

    def drop_context_file(self, message: BusMessage, rendered: str) -> Path | None:
        """Last-resort delivery: write the message where the harness will read it.

        Item 53's fallback for harnesses with no programmatic hook. The file is
        written into the lane's worktree under a name every harness either reads
        by convention (``AGENTS.md``) or cannot miss in a directory listing; the
        message's provenance stays in the file so a harness that picks it up is
        never reading unattributed text. Returns the path written, or ``None``
        when there is nowhere to write — a lane with no worktree has no fallback,
        and pretending otherwise would lose the message silently.
        """
        lane = self.lane
        cwd: Path | None = None
        worktree = str(getattr(lane, "worktree_path", "") or "") if lane is not None else ""
        if worktree:
            cwd = Path(worktree)
        if cwd is None or not cwd.exists():
            return None
        target = cwd / "AGENTS.md"
        try:
            existing = target.read_text(encoding="utf-8") if target.exists() else ""
            block = (
                f"\n<!-- openburrow:message:{message.id} -->\n"
                f"{rendered}\n"
                "<!-- /openburrow:message -->\n"
            )
            target.write_text(existing + block, encoding="utf-8")
        except OSError as exc:
            log.warning("adapter.context_file_failed", adapter=self.name, error=str(exc))
            return None
        return target

    def render_injection(self, message: BusMessage) -> str:
        """Render an A2A message as text a harness will understand.

        Deliberately explicit about where the text came from. A harness that
        sees "another agent said this" behaves differently from one that sees an
        unattributed instruction — and the attribution is also what makes the
        poisoned-message test (item 206) meaningful.
        """
        origin = message.sender_harness or message.sender_lane or "another agent"
        header = f"[OpenBurrow · from {origin}"
        if message.intent and str(message.intent) != "inform":
            header += f" · {message.intent}"
        header += "]"
        lines = [header]
        if message.subject:
            lines.append(f"Subject: {message.subject}")
        lines.append(message.body)
        refs = (message.payload or {}).get("openburrow:refs") or []
        if refs:
            lines.append("References: " + ", ".join(str(r) for r in refs[:8]))
        requested = (message.payload or {}).get("openburrow:requestedChange")
        if requested:
            lines.append(f"Requested change: {requested}")
        if message.requires_reply:
            lines.append("A reply is required. Respond with one of: accept, reject, counter.")
        return "\n".join(lines)

    def translate_output(self, output: HarnessOutput) -> BusMessage | None:
        """Map harness output onto a bus message.

        Returns ``None`` for output that is not worth broadcasting (progress
        chatter, spinner frames). The default is deliberately conservative:
        broadcasting everything is how a bus becomes noise.

        A frame with neither text nor an artifact is dropped whatever its kind.
        The old guard only covered ``kind == "text"``, so an empty ``status``
        frame — which is what a harness's init/keepalive notice looks like once
        it has been mapped — was published as a message with an empty body. A
        message carrying no bytes carries no information, and on a bus that
        every lane reads, the cost of that is paid by everyone.
        """
        if output.kind not in {"plan", "diff", "result", "error", "status"}:
            return None
        if not output.text.strip() and not output.artifacts:
            return None
        return BusMessage(
            session_id=getattr(self.lane, "session_id", ""),
            thread_id=getattr(self.lane, "session_id", ""),
            sender_lane=getattr(self.lane, "id", ""),
            sender_harness=self.name,
            recipients=[],
            broadcast=True,
            subject=f"{self.name}: {output.kind}",
            body=output.text[:4096],
            payload=output.to_bus_payload(),
        )

    # --- shutdown ----------------------------------------------------------
    async def stop(self, *, timeout: float = 10.0, force: bool = False) -> None:
        """Terminate the harness: SIGTERM, wait, then SIGKILL.

        The two-phase sequence matters because a harness that is mid-write needs
        the grace period, but a wedged one must not hold the daemon open. The
        timeout is enforced, not hoped for.
        """
        if not self.is_running:
            return

        log.info("adapter.stopping", adapter=self.name, lane_id=getattr(self.lane, "id", None))

        if self._pty is not None:
            try:
                if force:
                    self._pty.kill(_KILL_SIGNAL)
                else:
                    self._pty.terminate(force=False)
                    await asyncio.sleep(min(timeout, 2.0))
                    if self._pty.isalive():
                        self._pty.terminate(force=True)
            except Exception as exc:
                log.warning("adapter.pty_stop_failed", error=str(exc))
            self._close_pty()
        elif self.process is not None:
            try:
                self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), timeout=timeout)
                except TimeoutError:
                    log.warning("adapter.force_kill", adapter=self.name)
                    self.process.kill()
                    await self.process.wait()
            except ProcessLookupError:
                pass
            self.process = None

        if self.lane is not None:
            self.lane.status = LaneStatus.STOPPED
            from openburrow.core.models.base import now

            self.lane.stopped_at = now()

    async def restart(self, lane: Lane) -> None:
        """Stop and start again, preserving the lane object."""
        await self.stop(force=True)
        await asyncio.sleep(0.5)
        await self.start(lane)

    # --- output buffering --------------------------------------------------
    def buffer_output(self, output: HarnessOutput) -> None:
        """Append to the bounded in-memory buffer, dropping the oldest first.

        Bounded on purpose: an unbounded buffer in a long-running daemon is a
        memory leak with extra steps.
        """
        self._output_buffer.append(output)
        if len(self._output_buffer) > self._buffer_limit:
            drop = len(self._output_buffer) - self._buffer_limit
            del self._output_buffer[:drop]

    def recent_output(self, limit: int = 50) -> list[HarnessOutput]:
        return self._output_buffer[-limit:]

    def clear_buffer(self) -> None:
        self._output_buffer.clear()

    # --- usage -------------------------------------------------------------
    def parse_usage(self, text: str) -> dict[str, Any]:
        """Extract token/cost usage from harness output, when it reports any.

        Returns an empty dict when the harness says nothing — which is the
        honest answer, and better than a fabricated estimate that then flows
        into a cost report a human might act on.
        """
        return {}

    def detect_usage_limit(self, text: str) -> str | None:
        """Detect the harness's own rate-limit message.

        Recognising this is what enables the involuntary-takeover trigger: a
        lane that has hit its provider limit should hand off rather than spin.

        This is a **detector**, and per
        [ADR 0009](../../../../../docs/architecture/adr/0009-detection-vs-enforcement.md)
        detectors are tuned for recall and are allowed to be noisy — a
        flagged-but-allowed message is a correct outcome. Two things are
        therefore deliberately *not* done here:

        * it does not try to be exhaustive about the ways a provider phrases a
          limit, because an unrecognised phrasing costs one spinning lane; and
        * it does not require corroboration from a second signal, because the
          common case is a single line of terminal output and nothing else.

        What it does do is avoid the two false positives that are expensive
        rather than merely noisy, because both destroy real work rather than
        raising a flag:

        1. **A bare ``429`` is not a status code.** It is a line number in a diff
           hunk header, a port, a byte count, part of a task id, or the digits
           inside a longer number. ``_LIMIT_CODE`` requires something to mark it
           as a status — an ``HTTP``/``status``/``code`` label before it, or a
           reason phrase after it. The old substring test matched any ``429``
           anywhere, so a diff that touched line 429 was replaced by a terminal
           error and the lane's actual output was thrown away.
        2. **The gerund is prose, not a notice.** "I'll add rate limiting to the
           retry loop" is a coding agent describing its plan. "Rate limit
           exceeded" is the provider talking. ``_LIMIT_PHRASE`` matches the noun
           and the participle and deliberately not ``-ing``, because the
           ``-ing`` form is how a person discusses the concept.

        Residual noise is accepted: "the API has rate limits" will still match.
        That is a flagged message, not a lost one.
        """
        phrase = _LIMIT_PHRASE.search(text)
        if phrase is not None:
            return phrase.group(0).strip().lower()
        if _LIMIT_CODE.search(text):
            return "429"
        return None

    def __repr__(self) -> str:
        lane_id = getattr(self.lane, "id", None)
        return f"<{type(self).__name__} lane={lane_id} running={self.is_running}>"


__all__ = [
    # Re-exported under its real name. The previous ``AdapterError_`` alias was
    # defined, listed here, and referenced nowhere — a trailing-underscore class
    # that existed only to avoid importing from core at the call site.
    "AdapterError",
    "HarnessAdapter",
    "HarnessOutput",
    "SpawnSpec",
]
