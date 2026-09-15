"""Shared CLI context.

Every command gets a :class:`CliContext`, which resolves three things once:

* the merged **configuration** (env + ``openburrow.yaml``)
* a **daemon client**, or a clear explanation of why there is not one
* the **output mode** (human, JSON, or quiet) so rendering is decided centrally

Commands that need the daemon call :meth:`CliContext.require_daemon` and get a
``DaemonNotRunningError`` with a fix-it hint if it is down. Commands that can run
standalone (``burrow init``, ``burrow doctor``, ``burrow config``) do not, which
is why the daemon is not started eagerly here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from openburrow.core.config.load import ResolvedConfig, load_config
from openburrow.core.errors import DaemonNotRunningError, OpenBurrowError
from openburrow.core.logging import configure_logging
from openburrow.core.paths import BurrowPaths, find_repo_root_or_none
from openburrow.daemon.ipc import IpcClient


class OutputMode(StrEnum):
    """How to render results."""

    HUMAN = "human"
    JSON = "json"
    QUIET = "quiet"


@dataclass(slots=True)
class CliContext:
    """Per-invocation state shared by every command."""

    output_mode: OutputMode = OutputMode.HUMAN
    repo_root: Path | None = None
    verbose: bool = False
    no_daemon: bool = False
    assume_yes: bool = False

    _config: ResolvedConfig | None = field(default=None, repr=False)
    _client: IpcClient | None = field(default=None, repr=False)
    _daemon_checked: bool = field(default=False, repr=False)

    # --- configuration -----------------------------------------------------
    def config(self, *, require_repo: bool = True) -> ResolvedConfig:
        """Merged configuration, loaded once per invocation."""
        if self._config is None:
            self._config = load_config(self.repo_root, require_repo=require_repo)
        return self._config

    def config_or_none(self) -> ResolvedConfig | None:
        """Configuration if a repo is available, else ``None``.

        Used by commands that must work outside a repo — ``burrow version``,
        ``burrow daemon status``, ``burrow relay serve``.
        """
        try:
            return self.config()
        except OpenBurrowError:
            return None

    @property
    def paths(self) -> BurrowPaths:
        return self.config().paths

    @property
    def is_json(self) -> bool:
        return self.output_mode == OutputMode.JSON

    @property
    def is_quiet(self) -> bool:
        return self.output_mode == OutputMode.QUIET

    # --- daemon ------------------------------------------------------------
    def daemon_client(self) -> IpcClient:
        """A control-plane client. Does not connect until a call is made."""
        if self._client is None:
            paths = self.repo_root_path() / ".openburrow"
            from openburrow.core.paths import BurrowPaths

            self._client = IpcClient(BurrowPaths.for_repo(self.repo_root_path()))
            del paths
        return self._client

    def repo_root_path(self) -> Path:
        if self.repo_root is not None:
            return self.repo_root
        found = find_repo_root_or_none()
        return found or Path.cwd()

    async def require_daemon(self) -> IpcClient:
        """Return a client for a daemon that is definitely up.

        The liveness check is a real ping rather than a PID file read, because a
        stale PID file is exactly the case where the naive check lies.
        """
        client = self.daemon_client()
        if self._daemon_checked:
            return client

        if not await client.ping():
            raise DaemonNotRunningError(
                "the burrow daemon is not running",
                hint=(
                    "Start it with `burrow daemon start`. "
                    "Commands that do not need live lanes can run with --no-daemon."
                ),
                context={"endpoint": client.paths.ipc_endpoint},
            )
        self._daemon_checked = True
        return client

    async def daemon_is_up(self) -> bool:
        return await self.daemon_client().ping()

    # --- logging -----------------------------------------------------------
    def configure_logging(self) -> None:
        """Quiet by default so command output is not buried in log lines."""
        if self.is_json:
            configure_logging(level="ERROR", fmt="json")
        elif self.verbose:
            configure_logging(level="DEBUG", fmt="console")
        else:
            configure_logging(level="WARNING", fmt="console")


def build_context(
    *,
    json_output: bool = False,
    quiet: bool = False,
    verbose: bool = False,
    no_daemon: bool = False,
    assume_yes: bool = False,
    repo_root: Path | None = None,
) -> CliContext:
    """Construct a context from parsed global flags."""
    mode = OutputMode.JSON if json_output else OutputMode.QUIET if quiet else OutputMode.HUMAN
    context = CliContext(
        output_mode=mode,
        repo_root=repo_root,
        verbose=verbose,
        no_daemon=no_daemon,
        assume_yes=assume_yes,
    )
    context.configure_logging()
    return context


def human_size(num_bytes: int) -> str:
    """Format a byte count for terminal display."""
    size = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


def short_id(value: str, length: int = 12) -> str:
    """Trim an identifier for tabular display, keeping the type prefix."""
    if not value:
        return "-"
    if "_" in value:
        prefix, _, body = value.partition("_")
        return f"{prefix}_{body[:length]}"
    return value[:length]


def truncate(text: str, width: int = 60) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= width else text[: width - 1] + "…"


def as_dict(value: Any) -> dict[str, Any]:
    """Best-effort conversion of a model or mapping into a plain dict."""
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return {"value": value}


__all__ = [
    "CliContext",
    "OutputMode",
    "as_dict",
    "build_context",
    "human_size",
    "short_id",
    "truncate",
]
