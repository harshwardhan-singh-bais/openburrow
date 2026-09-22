"""OpenCode adapter — the reference implementation.

Built first on purpose (roadmap item 19). OpenCode exposes a headless HTTP
server plus an official SDK, which means it needs no PTY scraping and no
best-effort parsing: the adapter gets structured output directly. Every other
adapter is written against the shape this one establishes, so getting it right
first is what keeps the rest consistent.

Two modes:

* **Server mode** (preferred) — ``opencode serve`` on a local port; the adapter
  posts prompts over HTTP and reads structured responses. This is the mode the
  architecture wants: no terminal parsing, exact state, real cancellation.
* **PTY mode** (fallback) — if the server will not start, fall back to driving
  the interactive CLI. Degraded, and the adapter says so in its health output
  rather than pretending the two are equivalent.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx

from openburrow.a2a.card import HarnessCapabilities, SkillSpec
from openburrow.adapters.base import (
    HarnessAdapter,
    HarnessOutput,
    SpawnSpec,
    _pty_write,
    _strip_ansi,
)
from openburrow.adapters.harnesses.generic import (
    _DIFF_HEADER,
    _USAGE_IN_KEYS,
    _USAGE_OUT_KEYS,
    _as_float,
    _first_int,
)
from openburrow.core.config.settings import Settings
from openburrow.core.errors import AdapterError
from openburrow.core.logging import get_logger
from openburrow.core.models import Lane, TaskArtifact

log = get_logger(__name__)

#: How long to wait for the headless server to accept connections.
SERVER_BOOT_TIMEOUT_S = 20.0


def _info_dict(item: Any) -> dict[str, Any]:
    """The message's ``info`` object, or an empty one.

    Assigned to a local before the isinstance check so the narrowed type
    survives: repeating the lookup in the guard and in the value leaves mypy
    with ``Any | dict | None``, which is how ``info.get(...)`` type-checked
    while the docstring below promised a null ``info`` could not raise.
    """
    info = item.get("info")
    return info if isinstance(info, dict) else {}


class OpenCodeAdapter(HarnessAdapter):
    """Drives OpenCode via its headless server, with a PTY fallback."""

    name = "opencode"
    description = "OpenCode — multi-provider CLI with a headless HTTP server and official SDK."
    binary = "opencode"
    binary_env = "OPENCODE_BIN"
    docs_url = "https://opencode.ai/docs"

    #: Credentials this harness understands. A lane gets one only by granting it
    #: in its template's ``env_passthrough``.
    #:
    #: Declared even though :meth:`build_spawn_spec` takes the value from
    #: ``settings`` rather than from the ambient environment — and that is
    #: precisely why the declaration matters. ``Settings`` reads the same
    #: environment, so a key configured there was reaching the process without
    #: ever passing through ``extra_env``, where the old isolation logic lived.
    #: Enforcement sits on the finished spec, so it catches this path too.
    credential_env: tuple[str, ...] = ("OPENCODE_API_KEY",)

    def __init__(self, settings: Settings, *, lane: Lane | None = None) -> None:
        super().__init__(settings, lane=lane)
        self.server_url = settings.opencode_server_url.rstrip("/")
        self._client: httpx.AsyncClient | None = None
        self._server_mode = False
        self._session_id: str = ""

    # --- declaration -------------------------------------------------------
    @property
    def capabilities(self) -> HarnessCapabilities:
        """OpenCode is the one harness we can claim the most about.

        ``structured_output=True`` because the server returns JSON; the fallback
        PTY path downgrades this at runtime via :meth:`effective_capabilities`.
        """
        return HarnessCapabilities(
            structured_output=True,
            streaming=True,
            resumable=True,
            mcp_tools=True,
            supports_interrupt=True,
            native_a2a=False,
        )

    def skills(self) -> list[SkillSpec]:
        return [
            SkillSpec(
                skill_id="opencode-serve",
                name="Headless session control",
                description="Start, drive, and cancel a headless OpenCode session over HTTP.",
                tags=["structured", "headless"],
            )
        ]

    def effective_capabilities(self) -> HarnessCapabilities:
        """Capabilities as they actually are right now, not as declared.

        Overrides :meth:`~openburrow.adapters.base.HarnessAdapter.effective_capabilities`,
        which the daemon now consults when it builds the lane's Agent Card. The
        governance layer compares declared against observed behaviour, so an
        adapter that silently degrades must report the degraded truth. A
        fallback-to-PTY run that still claimed structured output would trip the
        capability-mismatch detector — correctly.

        The override is only meaningful after ``start`` has decided the mode, so
        ``_server_mode`` is set before anything reads this.
        """
        caps = self.capabilities
        if not self._server_mode:
            return HarnessCapabilities(
                structured_output=False,
                streaming=True,
                resumable=False,
                mcp_tools=caps.mcp_tools,
                supports_interrupt=False,
                native_a2a=False,
            )
        return caps

    # --- spawn -------------------------------------------------------------
    def build_spawn_spec(self, lane: Lane) -> SpawnSpec:
        worktree = Path(lane.worktree_path) if lane.worktree_path else Path.cwd()
        env = self.base_env(lane)
        if self.settings.opencode_api_key:
            env["OPENCODE_API_KEY"] = self.settings.opencode_api_key

        port = self._server_port_for(lane)
        command = [self.resolve_binary(), "serve", "--port", str(port), "--hostname", "127.0.0.1"]
        if self.settings.opencode_config_dir:
            command.extend(["--config", self.settings.opencode_config_dir])

        return SpawnSpec(command=command, cwd=worktree, env=env, use_pty=False, stdin_pipe=False)

    def _server_port_for(self, lane: Lane) -> int:
        """Derive a stable port per lane from the configured base URL.

        Stable rather than ephemeral so a restarted lane reclaims its port and
        in-flight HTTP clients reconnect without rediscovery.

        The derivation used ``abs(hash(lane.id)) % 100``, which is **not stable
        across processes**: CPython randomises ``hash()`` for ``str`` and
        ``bytes`` per interpreter (PEP 456 / ``PYTHONHASHSEED``), so the same
        lane got a different port on every daemon restart. The docstring above
        was therefore false, and the failure it was written to prevent — clients
        reconnecting to a port nobody is listening on — was the failure it
        caused.

        ``blake2b`` over the lane id gives the same number in every process, for
        the same reason :attr:`~openburrow.core.models.base.BurrowModel.content_hash`
        uses it: a hash that is stable across runs is a different tool from one
        that is stable only within a run.
        """
        base = self.server_url
        try:
            configured = int(base.rsplit(":", 1)[-1].split("/")[0])
        except (ValueError, IndexError):
            configured = 4096
        digest = hashlib.blake2b(lane.id.encode("utf-8"), digest_size=2).digest()
        return configured + int.from_bytes(digest, "big") % 100

    # --- lifecycle ---------------------------------------------------------
    async def start(self, lane: Lane, *, spec: SpawnSpec | None = None) -> None:
        await super().start(lane, spec=spec)
        self._server_mode = await self._await_server()
        if self._server_mode:
            self._client = httpx.AsyncClient(base_url=self.server_url, timeout=30.0)
            await self._create_session(lane)
        else:
            log.warning(
                "opencode.server_unavailable",
                lane_id=lane.id,
                url=self.server_url,
                hint="Falling back to PTY mode; structured output will be unavailable.",
            )

    async def _await_server(self, timeout: float = SERVER_BOOT_TIMEOUT_S) -> bool:
        """Poll the server's health endpoint until it answers or we give up."""
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if not self.is_running:
                return False
            try:
                async with httpx.AsyncClient(timeout=1.0) as probe:
                    response = await probe.get(f"{self.server_url}/app")
                    if response.status_code < 500:
                        return True
            except httpx.HTTPError:
                await asyncio.sleep(0.4)
        return False

    async def _create_session(self, lane: Lane) -> None:
        if self._client is None:
            return
        try:
            response = await self._client.post("/session", json={"title": f"openburrow:{lane.id}"})
            if response.status_code < 400:
                payload = response.json()
                self._session_id = str(payload.get("id") or payload.get("sessionID") or "")
                log.info("opencode.session_created", lane_id=lane.id, session=self._session_id)
        except httpx.HTTPError as exc:
            log.warning("opencode.session_create_failed", lane_id=lane.id, error=str(exc))

    async def stop(self, *, timeout: float = 10.0, force: bool = False) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        await super().stop(timeout=timeout, force=force)

    # --- interaction -------------------------------------------------------
    async def send_prompt(self, lane: Lane, prompt: str) -> None:
        """Send a prompt. In server mode this is an HTTP post; otherwise a PTY write."""
        if self._server_mode and self._client is not None:
            payload: dict[str, Any] = {
                "parts": [{"type": "text", "text": prompt}],
            }
            if self._session_id:
                payload["sessionID"] = self._session_id
            try:
                response = await self._client.post(
                    f"/session/{self._session_id}/message"
                    if self._session_id
                    else "/session/message",
                    json=payload,
                )
                response.raise_for_status()
                return
            except httpx.HTTPError as exc:
                log.warning("opencode.prompt_failed", lane_id=lane.id, error=str(exc))
                raise AdapterError(
                    "OpenCode rejected the prompt",
                    hint="Check that the headless server is still running and the session is alive.",
                    context={"lane_id": lane.id, "session": self._session_id},
                    cause=exc,
                ) from exc

        # PTY fallback: submit the line the way a terminal does.
        if self._pty is not None:
            _pty_write(self._pty, prompt + "\r")
            return
        raise AdapterError("OpenCode is not running and has no PTY to write to")

    async def read_output(self) -> AsyncIterator[HarnessOutput]:
        """Yield interpreted output.

        In server mode we poll the session's message list and emit only what we
        have not seen, which is a poor man's streaming but is robust against
        the server restarting mid-session. In PTY mode we scrape the terminal.
        """
        if self._server_mode:
            async for output in self._read_server_output():
                yield output
        else:
            async for output in self._read_pty_output():
                yield output

    async def _read_server_output(self, poll_interval: float = 0.5) -> AsyncIterator[HarnessOutput]:
        if self._client is None or not self._session_id:
            return
        seen: set[str] = set()
        while self.is_running:
            try:
                response = await self._client.get(f"/session/{self._session_id}/message")
                if response.status_code >= 400:
                    await asyncio.sleep(poll_interval)
                    continue
                for item in response.json() or []:
                    if not isinstance(item, dict):
                        continue
                    info = _info_dict(item)
                    message_id = str(item.get("id") or info.get("id") or "")
                    if not message_id or message_id in seen:
                        continue
                    seen.add(message_id)
                    output = self._interpret_server_message(item)
                    if output is not None:
                        self.buffer_output(output)
                        yield output
            except httpx.HTTPError:
                pass
            await asyncio.sleep(poll_interval)

    def _interpret_server_message(self, item: dict[str, Any]) -> HarnessOutput | None:
        """Map an OpenCode message object onto a :class:`HarnessOutput`.

        This is the whole reason OpenCode is the reference: the mapping is a
        field rename, not a heuristic. Compare with the PTY path below, which
        is regex and hope.

        The message envelope is not uniform across OpenCode's own responses:
        some fields sit at the top level and some under ``info``. ``info`` is
        read once, with a type check, because ``item.get("info", {}).get(...)``
        only defaults when the key is *absent* — an ``"info": null`` or
        ``"info": "..."`` would raise ``AttributeError`` from inside the read
        loop and end the lane's output with a traceback in the log.
        """
        info = _info_dict(item)

        role = str(item.get("role") or info.get("role") or "")
        if role not in {"assistant", ""}:
            return None

        parts = item.get("parts") or []
        texts: list[str] = []
        artifacts: list[TaskArtifact] = []
        kind = "text"

        for part in parts:
            if not isinstance(part, dict):
                continue
            part_type = str(part.get("type") or part.get("kind") or "")
            if part_type in {"text", "reasoning"}:
                texts.append(str(part.get("text") or ""))
            elif part_type in {"tool", "tool-invocation", "tool_call"}:
                kind = "tool-call"
                tool_name = str(part.get("tool") or part.get("name") or "tool")
                texts.append(f"tool: {tool_name}")
            elif part_type in {"patch", "diff"}:
                kind = "diff"
                diff_text = str(part.get("content") or part.get("diff") or "")
                texts.append(diff_text)
                artifacts.append(TaskArtifact.diff(diff_text))

        if not texts:
            return None

        info = _info_dict(item)
        usage = item.get("usage") or info.get("tokens")
        tokens = usage if isinstance(usage, dict) else {}
        cost = _as_float(item.get("cost"))

        # Cost is recorded independently of the usage object. The previous guard
        # was ``if usage and self.lane`` around a call that also passed the cost,
        # so a message reporting a cost but no token counts — which is exactly
        # what a patch event looks like — recorded nothing at all.
        if self.lane is not None and (tokens or cost):
            self.lane.record_usage(
                tokens_in=_first_int(tokens, _USAGE_IN_KEYS),
                tokens_out=_first_int(tokens, _USAGE_OUT_KEYS),
                cost_usd=cost,
            )

        completed = bool(item.get("completed") or info.get("completed"))
        return HarnessOutput(
            kind=kind,
            text="\n".join(text for text in texts if text),
            structured=True,
            terminal=completed,
            artifacts=artifacts,
            data={"role": role, "usage": usage or {}},
        )

    async def _read_pty_output(self) -> AsyncIterator[HarnessOutput]:
        """PTY fallback: read the terminal and guess at structure.

        Honest about being degraded. ``structured=False`` on everything means the
        metrics layer can tell this lane apart from a structured one, and the
        capability-mismatch detector does not fire on a claim we did not make.

        The reading is delegated to
        :meth:`~openburrow.adapters.base.HarnessAdapter._iter_pty_text`. This
        method used to run its own loop — ``self._pty.read(4096)`` on the event
        loop, then ``chunk.decode("utf-8")`` — which is the blocking-read bug and
        the bytes/str bug in a second place, because ``OpenCodeAdapter`` extends
        :class:`~openburrow.adapters.base.HarnessAdapter` directly and never
        inherited the fixed version.
        """
        async for unit in self._iter_pty_text():
            output = self._parse_pty_chunk(unit)
            if output is not None:
                self.buffer_output(output)
                yield output

    def _parse_pty_chunk(self, text: str) -> HarnessOutput | None:
        """Best-effort structure detection from raw terminal output.

        The fallback path, and it says so: everything it produces is
        ``structured=False`` except a chunk that really was JSON.

        The order matches
        :meth:`~openburrow.adapters.harnesses.generic.GenericCliAdapter._interpret`
        — JSON, then diff, then usage limit, then plain text — because two
        adapters classifying the same bytes differently is the bug a fixed order
        exists to prevent. This used to test diff first, and only at the *start*
        of a chunk, so a diff that arrived after a banner line was filed as
        prose. The diff pattern is imported rather than redefined for the same
        reason ``_strip_ansi`` is shared: two copies drift.
        """
        stripped = _strip_ansi(text).strip()
        if not stripped:
            return None

        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                return HarnessOutput(
                    kind="result", text=stripped, structured=True, data=json.loads(stripped)
                )
            except json.JSONDecodeError:
                # Not JSON after all — fall through to the ordinary classifiers
                # rather than guessing twice.
                pass

        if _DIFF_HEADER.search(stripped):
            return HarnessOutput(
                kind="diff",
                text=stripped,
                structured=False,
                artifacts=[TaskArtifact.diff(stripped)],
            )

        limit = self.detect_usage_limit(stripped)
        if limit:
            return HarnessOutput(
                kind="error",
                text=stripped,
                structured=False,
                terminal=True,
                data={"usage_limit": limit},
            )
        return HarnessOutput(kind="text", text=stripped, structured=False)

    async def healthcheck(self) -> tuple[bool, str]:
        ok, message = await super().healthcheck()
        if not ok:
            return ok, message
        try:
            async with httpx.AsyncClient(timeout=2.0) as probe:
                response = await probe.get(f"{self.server_url}/app")
            if response.status_code < 500:
                return True, f"headless server responding at {self.server_url}"
        except httpx.HTTPError:
            pass
        return True, f"{message} (headless server not currently running; PTY fallback available)"

    def parse_usage(self, text: str) -> dict[str, Any]:
        """OpenCode reports usage in JSON when in server mode."""
        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return {}
        usage = payload.get("usage") or payload.get("tokens")
        return dict(usage) if isinstance(usage, dict) else {}


__all__ = ["OpenCodeAdapter"]
