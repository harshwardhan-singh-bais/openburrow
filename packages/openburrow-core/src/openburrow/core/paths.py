"""Filesystem layout resolution.

OpenBurrow touches exactly four places on disk and it is worth naming them
precisely, because the governance story depends on knowing where state lives:

1. **Repo runtime** — ``<repo>/.openburrow/``  (git-ignored)
   SQLite db, sockets, pidfile, casts, reels, per-repo config overrides.

2. **Global runtime** — ``~/.openburrow/``
   Cross-repo state: the machine identity, the daemon registry, the adapter
   registry cache, and the SSH key material used for relay auth.

3. **Worktree pool** — ``<repo>/.openburrow/worktrees/<lane>/``
   One git worktree per lane. Default is inside the repo so cleanup is trivial;
   override with ``OPENBURROW_WORKTREE_ROOT`` when the repo lives on a slow disk.

4. **Cache** — the platformdirs cache dir
   Purely derived data: LLM judge verdicts, harness version probes, embeddings.
   Safe to delete at any time.

Everything is resolved through :class:`BurrowPaths` so no other module ever
concatenates a path by hand. That is what makes ``OPENBURROW_HOME`` overridable
and Windows-vs-POSIX differences a single-file concern.
"""

from __future__ import annotations

import hashlib
import os
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from platformdirs import user_cache_dir, user_config_dir, user_state_dir

from openburrow.core.errors import RepoNotInitializedError

#: Files whose presence marks a directory as a repository root, in priority order.
REPO_MARKERS: tuple[str, ...] = (
    "openburrow.yaml",
    ".git",
    "pyproject.toml",
    "package.json",
    "go.mod",
    "Cargo.toml",
)

CONFIG_FILENAME = "openburrow.yaml"
CONFIG_ALIASES: tuple[str, ...] = ("openburrow.yaml", "openburrow.yml", ".openburrow.yaml")
DB_FILENAME = "burrow.db"
PID_FILENAME = "burrow.pid"
SOCKET_FILENAME = "burrow.sock"
DEFAULT_RUNTIME_DIRNAME = ".openburrow"
DEFAULT_GLOBAL_DIRNAME = ".openburrow"
AGENTS_MD_FILENAME = "AGENTS.md"


def is_windows() -> bool:
    return sys.platform.startswith("win")


@lru_cache(maxsize=1)
def global_home() -> Path:
    """``~/.openburrow`` unless ``OPENBURROW_GLOBAL_HOME`` overrides it."""
    override = os.environ.get("OPENBURROW_GLOBAL_HOME", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / DEFAULT_GLOBAL_DIRNAME


@lru_cache(maxsize=1)
def cache_home() -> Path:
    """Derived, disposable data. Overridable via ``OPENBURROW_CACHE_HOME``."""
    override = os.environ.get("OPENBURROW_CACHE_HOME", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return Path(user_cache_dir("openburrow", "openburrow"))


@lru_cache(maxsize=1)
def config_home() -> Path:
    """User-level (not repo-level) configuration."""
    return Path(user_config_dir("openburrow", "openburrow"))


@lru_cache(maxsize=1)
def state_home() -> Path:
    """Platform state directory, used for the daemon registry."""
    return Path(user_state_dir("openburrow", "openburrow"))


def find_repo_root(start: Path | None = None) -> Path:
    """Walk up from ``start`` looking for a repository marker.

    Prefers a directory containing ``openburrow.yaml`` — that is an *initialised*
    repo. Falls back to any VCS marker so ``burrow init`` can run in a fresh
    clone that has never seen OpenBurrow before.
    """
    current = (start or Path.cwd()).resolve()
    if current.is_file():
        current = current.parent

    openburrow_root: Path | None = None
    vcs_root: Path | None = None

    for candidate in (current, *current.parents):
        if openburrow_root is None and any(
            (candidate / alias).is_file() for alias in CONFIG_ALIASES
        ):
            openburrow_root = candidate
        if vcs_root is None and (candidate / ".git").exists():
            vcs_root = candidate
        if openburrow_root is not None and vcs_root is not None:
            break

    root = openburrow_root or vcs_root
    if root is None:
        raise RepoNotInitializedError(
            f"no repository found at or above {current}",
            context={"start": str(current), "markers": list(REPO_MARKERS)},
        )
    return root


def find_repo_root_or_none(start: Path | None = None) -> Path | None:
    try:
        return find_repo_root(start)
    except RepoNotInitializedError:
        return None


def find_config_file(repo_root: Path) -> Path | None:
    for alias in CONFIG_ALIASES:
        candidate = repo_root / alias
        if candidate.is_file():
            return candidate
    return None


def repo_id_for(repo_root: Path | str) -> str:
    """A stable, filesystem-safe identifier for one repository.

    Hashed rather than spelled out because the value lands in a database column
    and in share links, and a Windows path contains characters that are awkward in
    both. Twelve hex characters is 48 bits, which is ample for telling apart the
    handful of repos one user has checked out — the same reasoning as the named
    pipe name, applied to a value that is stored rather than compared.

    Lives here rather than beside its first caller because three layers need it
    now — the request surface, the lane briefing, and anything that later reads a
    row back by repo — and two of those cannot import the third. A second copy of
    this hash would be a second definition of what a repo *is*, which is the kind
    of thing that stays consistent until the day it does not.
    """
    digest = hashlib.blake2b(str(repo_root).casefold().encode("utf-8"), digest_size=8)
    return f"repo_{digest.hexdigest()}"


@dataclass(frozen=True, slots=True)
class BurrowPaths:
    """Every path OpenBurrow derives, resolved once per repo.

    Construct via :meth:`for_repo` (or :meth:`current`) rather than directly, so
    the ``OPENBURROW_*`` overrides are applied consistently.
    """

    repo_root: Path
    runtime_dir: Path
    global_dir: Path
    worktree_root: Path
    cache_dir: Path

    # --- derived files -----------------------------------------------------
    @property
    def db_path(self) -> Path:
        override = os.environ.get("OPENBURROW_STATE_DIR", "").strip()
        base = Path(override).expanduser().resolve() if override else self.runtime_dir
        return base / DB_FILENAME

    @property
    def db_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.db_path.as_posix()}"

    @property
    def pid_path(self) -> Path:
        override = os.environ.get("OPENBURROW_DAEMON_PIDFILE", "").strip()
        if override:
            return Path(override).expanduser().resolve()
        return self.runtime_dir / PID_FILENAME

    @property
    def socket_path(self) -> Path:
        """POSIX unix socket path. Windows uses :attr:`pipe_name` instead."""
        override = os.environ.get("OPENBURROW_DAEMON_SOCKET", "").strip()
        if override:
            return Path(override).expanduser()
        return self.runtime_dir / SOCKET_FILENAME

    @property
    def pipe_name(self) -> str:
        """Windows named-pipe path, namespaced per user *and* per repo.

        The repo component is not decoration. Windows refuses a second server on
        a pipe name that is already bound (``PermissionError [WinError 5]``), so a
        name derived from the username alone would mean the first repo to start a
        daemon owned the endpoint for the whole machine and every other repo's
        daemon died at bind. The unix socket has always been per-repo — it lives
        in that repo's ``.openburrow/`` — and the two transports have to agree
        about how many daemons can exist.

        Hashed rather than spelled out because a pipe name is limited to 256
        characters and a Windows repo path can approach that on its own. The
        digest is truncated to 12 hex characters: 48 bits, which is ample for
        telling apart the handful of repos one user has checked out.

        **Lowercased, not casefolded**, and the reason is cross-language rather
        than typographic. ``apps/web/src/lib/daemon-bridge.ts`` computes this
        digest a second time in TypeScript, where the only case operation
        available is ``String.prototype.toLowerCase()``; JavaScript has no
        casefold. Measured over every Unicode codepoint, ``casefold`` disagrees
        with ``toLowerCase`` at **352** of them while ``lower`` disagrees at
        **55** — and all 55 are codepoints Python's bundled Unicode tables do not
        yet know (V8 maps them, CPython leaves them as identity), which is a
        version skew that closes on its own rather than a semantic difference.

        The 352 included ``U+00DF``, so a repo at ``...\\straße-projekt``
        produced one pipe name here and another in the dashboard: the daemon
        listened on one and the browser dialled the other, and the dashboard said
        only "daemon unreachable". ``lower`` also handles case-insensitivity
        better for that character, since ``"STRAßE".lower() == "straße".lower()``
        on both sides, whereas ``casefold`` turns both into ``strasse`` here and
        leaves them as ``straße`` there.

        For pure-ASCII paths — every path this project has actually been run
        against — ``lower`` and ``casefold`` are identical, so no existing pipe
        name moved. ``scripts/check_endpoint_parity.py`` asserts this agreement
        against the real TypeScript; do not change the operation on one side
        without the other.
        """
        user = os.environ.get("USERNAME") or os.environ.get("USER") or "default"
        repo = hashlib.sha256(str(self.repo_root).lower().encode("utf-8")).hexdigest()[:12]
        return rf"\\.\pipe\openburrow-{user}-{repo}"

    @property
    def ipc_endpoint(self) -> str:
        """Transport-agnostic endpoint the CLI dials."""
        if is_windows():
            override = os.environ.get("OPENBURROW_DAEMON_SOCKET", "").strip()
            return override or self.pipe_name
        return str(self.socket_path)

    @property
    def config_file(self) -> Path:
        return self.repo_root / CONFIG_FILENAME

    @property
    def agents_md(self) -> Path:
        name = os.environ.get("OPENBURROW_AGENTS_MD", AGENTS_MD_FILENAME).strip()
        return self.repo_root / (name or AGENTS_MD_FILENAME)

    @property
    def casts_dir(self) -> Path:
        return self.runtime_dir / "casts"

    @property
    def reels_dir(self) -> Path:
        return self.runtime_dir / "reels"

    @property
    def logs_dir(self) -> Path:
        return self.runtime_dir / "logs"

    @property
    def adapters_dir(self) -> Path:
        return self.runtime_dir / "adapters"

    @property
    def policy_file(self) -> Path:
        override = os.environ.get("OPENBURROW_POLICY_FILE", "").strip()
        if override:
            p = Path(override)
            return p if p.is_absolute() else self.repo_root / p
        return self.runtime_dir / "policy.yaml"

    @property
    def risk_tiers_file(self) -> Path:
        override = os.environ.get("OPENBURROW_APPROVALS_RISK_TIER_FILE", "").strip()
        if override:
            p = Path(override)
            return p if p.is_absolute() else self.repo_root / p
        return self.runtime_dir / "risk-tiers.yaml"

    @property
    def hooks_file(self) -> Path:
        override = os.environ.get("OPENBURROW_HOOKS_FILE", "").strip()
        if override:
            p = Path(override)
            return p if p.is_absolute() else self.repo_root / p
        return self.runtime_dir / "hooks.yaml"

    @property
    def checkpoint_dir(self) -> Path:
        return self.runtime_dir / "checkpoints"

    @property
    def machine_identity_file(self) -> Path:
        return self.global_dir / "machine.json"

    @property
    def daemon_registry_file(self) -> Path:
        return self.global_dir / "daemons.json"

    def lane_worktree(self, lane_id: str) -> Path:
        return self.worktree_root / lane_id

    def lane_cast(self, lane_id: str) -> Path:
        return self.casts_dir / f"{lane_id}.cast"

    def lane_log(self, lane_id: str) -> Path:
        return self.logs_dir / f"{lane_id}.jsonl"

    def ensure(self, *, worktrees: bool = True) -> BurrowPaths:
        """Create the directories this instance needs. Idempotent."""
        for directory in (
            self.runtime_dir,
            self.global_dir,
            self.cache_dir,
            self.logs_dir,
            self.casts_dir,
            self.reels_dir,
            self.checkpoint_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        if worktrees:
            self.worktree_root.mkdir(parents=True, exist_ok=True)
        return self

    # --- constructors ------------------------------------------------------
    @classmethod
    def for_repo(cls, repo_root: Path | str) -> BurrowPaths:
        root = Path(repo_root).expanduser().resolve()

        runtime_override = os.environ.get("OPENBURROW_HOME", "").strip()
        if runtime_override:
            runtime = Path(runtime_override).expanduser()
            runtime_dir = runtime if runtime.is_absolute() else root / runtime
        else:
            runtime_dir = root / DEFAULT_RUNTIME_DIRNAME

        worktree_override = os.environ.get("OPENBURROW_WORKTREE_ROOT", "").strip()
        if worktree_override:
            worktree_root = Path(worktree_override).expanduser().resolve()
        else:
            worktree_root = runtime_dir / "worktrees"

        return cls(
            repo_root=root,
            runtime_dir=runtime_dir,
            global_dir=global_home(),
            worktree_root=worktree_root,
            cache_dir=cache_home(),
        )

    @classmethod
    def current(cls, start: Path | None = None) -> BurrowPaths:
        return cls.for_repo(find_repo_root(start))


__all__ = [
    "AGENTS_MD_FILENAME",
    "CONFIG_ALIASES",
    "CONFIG_FILENAME",
    "DB_FILENAME",
    "REPO_MARKERS",
    "BurrowPaths",
    "cache_home",
    "config_home",
    "find_config_file",
    "find_repo_root",
    "find_repo_root_or_none",
    "global_home",
    "is_windows",
    "repo_id_for",
    "state_home",
]
