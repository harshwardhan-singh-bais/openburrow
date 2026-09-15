"""File watching per worktree.

A lane's harness changes files; the bus needs to know. This module watches each
lane's worktree and translates change bursts into A2A messages.

Two decisions worth stating:

**Debouncing is mandatory, not an optimisation.** A single editor save produces
several filesystem events (write, chmod, rename-for-atomic-save). Without
debouncing, one save becomes four bus messages, and a bus that emits four
messages per keystroke is a bus nobody reads. The debounce window is configurable
because the right value depends on the harness's write pattern.

**The watcher never emits raw paths as messages.** It emits a *summary* — which
lane touched which files — because the point is to inform other lanes that a
region of the tree is moving, not to replicate a filesystem journal onto the bus.
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from openburrow.core.logging import get_logger

log = get_logger(__name__)

#: Emit a change message after this long with no further events.
DEFAULT_DEBOUNCE_S = 1.5

#: Directories never worth watching.
IGNORED_DIRECTORIES: tuple[str, ...] = (
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".next",
    "dist",
    "build",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    ".openburrow",
)

#: File patterns never worth reporting.
IGNORED_PATTERNS: tuple[str, ...] = (
    "*.pyc",
    "*.pyo",
    "*.swp",
    "*.swx",
    "*.tmp",
    "*.log",
    "*~",
    ".DS_Store",
    "Thumbs.db",
    "*.lock",
    "uv.lock",
    "package-lock.json",
)


@dataclass(slots=True)
class FileChange:
    """A debounced batch of changes in one lane's worktree."""

    lane_id: str
    worktree: str
    added: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.modified or self.deleted)

    @property
    def total(self) -> int:
        return len(self.added) + len(self.modified) + len(self.deleted)

    def all_paths(self) -> list[str]:
        return [*self.added, *self.modified, *self.deleted]

    def summary(self) -> str:
        parts: list[str] = []
        if self.added:
            parts.append(f"+{len(self.added)}")
        if self.modified:
            parts.append(f"~{len(self.modified)}")
        if self.deleted:
            parts.append(f"-{len(self.deleted)}")
        preview = ", ".join(Path(p).name for p in self.all_paths()[:4])
        suffix = f" ({preview})" if preview else ""
        return f"{' '.join(parts)} files{suffix}"

    def to_payload(self) -> dict:
        return {
            "lane_id": self.lane_id,
            "worktree": self.worktree,
            "added": self.added[:200],
            "modified": self.modified[:200],
            "deleted": self.deleted[:200],
            "total": self.total,
        }


ChangeHandler = Callable[[FileChange], Awaitable[None]]


def is_ignored(relative_path: str) -> bool:
    """Should this path be excluded from change reporting?"""
    parts = Path(relative_path).parts
    if any(part in IGNORED_DIRECTORIES for part in parts):
        return True
    name = Path(relative_path).name
    return any(fnmatch.fnmatch(name, pattern) for pattern in IGNORED_PATTERNS)


class WorktreeWatcher:
    """Watches one lane's worktree and debounces changes into batches."""

    def __init__(
        self,
        *,
        lane_id: str,
        worktree: Path,
        on_change: ChangeHandler,
        debounce_s: float = DEFAULT_DEBOUNCE_S,
        extra_ignore: Iterable[str] = (),
    ) -> None:
        self.lane_id = lane_id
        self.worktree = Path(worktree)
        self.on_change = on_change
        self.debounce_s = debounce_s
        self.extra_ignore = tuple(extra_ignore)
        self._pending = FileChange(lane_id=lane_id, worktree=str(self.worktree))
        self._timer: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    # --- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        if not self.worktree.exists():
            log.warning("filewatch.worktree_missing", lane_id=self.lane_id, path=str(self.worktree))
            return
        self._task = asyncio.create_task(self._run())
        log.debug("filewatch.started", lane_id=self.lane_id, path=str(self.worktree))

    async def stop(self) -> None:
        self._stop.set()
        if self._timer is not None:
            self._timer.cancel()
        if self._task is not None:
            self._task.cancel()
            # Awaiting a task we just cancelled raises the cancellation we caused,
            # so suppressing it is the point rather than an oversight.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        # Flush anything still pending so a lane's last edits are not lost.
        if not self._pending.is_empty:
            await self._flush()

    # --- watching ----------------------------------------------------------
    async def _run(self) -> None:
        """Watch using ``watchfiles`` when available, polling otherwise.

        The polling fallback exists because ``watchfiles`` needs a Rust
        extension, and a lane that silently stops reporting changes on a machine
        where that wheel is unavailable is worse than a slightly less efficient
        watcher.
        """
        try:
            from watchfiles import awatch
        except ImportError:
            await self._run_polling()
            return

        try:
            async for changes in awatch(
                self.worktree,
                stop_event=self._stop,
                debounce=int(self.debounce_s * 1000),
                step=100,
                recursive=True,
                ignore_permission_denied=True,
            ):
                for change_type, raw_path in changes:
                    self._record(change_type, raw_path)
                await self._flush()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("filewatch.failed", lane_id=self.lane_id, error=str(exc))

    async def _run_polling(self, interval: float = 2.0) -> None:
        """Fallback: snapshot mtimes and diff them."""
        snapshot = self._snapshot()
        while not self._stop.is_set():
            await asyncio.sleep(interval)
            current = self._snapshot()
            for path in current.keys() - snapshot.keys():
                self._pending.added.append(path)
            for path in snapshot.keys() - current.keys():
                self._pending.deleted.append(path)
            for path in current.keys() & snapshot.keys():
                if current[path] != snapshot[path]:
                    self._pending.modified.append(path)
            snapshot = current
            await self._flush()

    def _snapshot(self) -> dict[str, float]:
        result: dict[str, float] = {}
        for path in self.worktree.rglob("*"):
            if not path.is_file():
                continue
            try:
                relative = str(path.relative_to(self.worktree))
            except ValueError:
                continue
            if is_ignored(relative) or any(
                fnmatch.fnmatch(relative, pat) for pat in self.extra_ignore
            ):
                continue
            try:
                result[relative] = path.stat().st_mtime
            except OSError:
                continue
        return result

    def _record(self, change_type: int, raw_path: str) -> None:
        """Record one raw filesystem event into the pending batch."""
        try:
            relative = str(Path(raw_path).relative_to(self.worktree))
        except ValueError:
            return
        if is_ignored(relative) or any(fnmatch.fnmatch(relative, pat) for pat in self.extra_ignore):
            return

        # watchfiles uses 1=added, 2=modified, 3=deleted.
        if change_type == 1:
            self._pending.added.append(relative)
        elif change_type == 3:
            self._pending.deleted.append(relative)
        else:
            self._pending.modified.append(relative)

    async def _flush(self) -> None:
        """Emit the pending batch, if it is worth emitting."""
        if self._pending.is_empty:
            return

        batch = self._pending
        self._pending = FileChange(lane_id=self.lane_id, worktree=str(self.worktree))

        # Collapse duplicates: a file saved three times is one modified entry.
        batch.added = sorted(set(batch.added) - set(batch.deleted))
        batch.modified = sorted(set(batch.modified) - set(batch.deleted) - set(batch.added))
        batch.deleted = sorted(set(batch.deleted))

        if batch.total == 0:
            return

        log.debug(
            "filewatch.change",
            lane_id=self.lane_id,
            total=batch.total,
            summary=batch.summary(),
        )
        try:
            await self.on_change(batch)
        except Exception as exc:
            log.warning("filewatch.handler_failed", lane_id=self.lane_id, error=str(exc))

    # --- test hooks --------------------------------------------------------
    async def inject_change(
        self,
        *,
        added: list[str] | None = None,
        modified: list[str] | None = None,
        deleted: list[str] | None = None,
    ) -> None:
        """Simulate a change without touching the filesystem. Used in tests."""
        self._pending.added.extend(added or [])
        self._pending.modified.extend(modified or [])
        self._pending.deleted.extend(deleted or [])
        await self._flush()


class WatcherPool:
    """Manages one watcher per lane."""

    def __init__(self) -> None:
        self._watchers: dict[str, WorktreeWatcher] = {}

    async def watch(
        self,
        *,
        lane_id: str,
        worktree: Path,
        on_change: ChangeHandler,
        debounce_s: float = DEFAULT_DEBOUNCE_S,
        extra_ignore: Iterable[str] = (),
    ) -> WorktreeWatcher:
        await self.unwatch(lane_id)
        watcher = WorktreeWatcher(
            lane_id=lane_id,
            worktree=worktree,
            on_change=on_change,
            debounce_s=debounce_s,
            extra_ignore=extra_ignore,
        )
        await watcher.start()
        self._watchers[lane_id] = watcher
        return watcher

    async def unwatch(self, lane_id: str) -> None:
        watcher = self._watchers.pop(lane_id, None)
        if watcher is not None:
            await watcher.stop()

    async def stop_all(self) -> None:
        for lane_id in list(self._watchers):
            await self.unwatch(lane_id)

    @property
    def count(self) -> int:
        return len(self._watchers)


__all__ = [
    "DEFAULT_DEBOUNCE_S",
    "IGNORED_DIRECTORIES",
    "IGNORED_PATTERNS",
    "ChangeHandler",
    "FileChange",
    "WatcherPool",
    "WorktreeWatcher",
    "is_ignored",
]
