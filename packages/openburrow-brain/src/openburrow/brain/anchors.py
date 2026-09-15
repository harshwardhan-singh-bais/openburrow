"""Git anchoring: deciding whether a Brain entry is still true.

The Brain's central design claim is that every durable entry is *anchored* — it
says "this was true about **this file**, at **this commit**". That is what makes
staleness a question with an answer instead of a guess.

Contrast the alternative, which is what most project-memory systems do: store a
fact with a timestamp and hope. Six weeks later nobody can tell whether
"`AuthMiddleware` reads the token from the cookie" is still true, so the fact
either gets trusted past its expiry or ignored. Both are failures — one ships a
bug, the other makes the whole memory layer worthless because nobody believes it.

Anchoring gives us three honest states:

``fresh``
    The anchored path has not changed since the anchor commit. The entry is
    probably still true. (Probably, not certainly — a change elsewhere can
    invalidate a fact about this file. We do not claim otherwise.)
``stale``
    The anchored path *has* changed. The entry might still be true, but it is now
    a claim someone should re-check. Marking it stale is not a judgement that it
    is wrong; it is a statement that its evidence has expired.
``unknown``
    We cannot tell — no anchor, no git, or the anchor commit is not in this
    clone. Unknown is deliberately not collapsed into ``fresh``. An entry we
    cannot verify is an entry we cannot vouch for, and pretending otherwise is
    exactly the failure mode anchoring exists to prevent.

Unscoped entries (a general convention with no ``anchor_path``) never go stale
from file drift, because there is no file whose drift would matter. They are
still bounded by their own retirement and by supersession.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from openburrow.core.logging import get_logger
from openburrow.core.models import BrainEntry

log = get_logger(__name__)

#: Git is invoked with an explicit argument list and never through a shell, but
#: the linter cannot see that, so the subprocess calls carry noqa markers.
_GIT_TIMEOUT_S = 10.0


class AnchorState(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class AnchorStatus:
    """The result of checking one entry's anchor."""

    state: AnchorState
    reason: str = ""
    #: Commit that last touched the anchored path, when we could determine it.
    last_commit: str = ""
    #: Commits the path has received since the anchor.
    commits_since: int = 0

    @property
    def is_stale(self) -> bool:
        return self.state == AnchorState.STALE


def _git(args: list[str], *, cwd: Path) -> tuple[int, str]:
    """Run git and return ``(returncode, stdout)``.

    Failures are returned rather than raised. A missing ``git`` binary, a
    directory that is not a repository, and a shallow clone that lacks the
    anchor commit are all normal situations for this code, not exceptional ones —
    they map onto :data:`AnchorState.UNKNOWN`, which is a real answer.
    """
    try:
        completed = subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("brain.anchor.git_unavailable", error=str(exc))
        return 127, ""
    return completed.returncode, completed.stdout.strip()


class AnchorChecker:
    """Answers "is this entry still true" for a repository working tree.

    One instance per repository. The head commit is read once and cached, because
    a staleness sweep checks every entry in the Brain and re-reading HEAD per
    entry would mean hundreds of identical subprocess calls.
    """

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = Path(repo_root)
        self._head: str | None = None
        self._head_resolved = False

    # --- repository state --------------------------------------------------
    @property
    def head_commit(self) -> str:
        """The current HEAD, or ``""`` when it cannot be determined."""
        if not self._head_resolved:
            code, out = _git(["rev-parse", "HEAD"], cwd=self.repo_root)
            self._head = out if code == 0 else ""
            self._head_resolved = True
        return self._head or ""

    def invalidate(self) -> None:
        """Forget the cached HEAD. Call after a commit lands."""
        self._head = None
        self._head_resolved = False

    def commit_exists(self, commit: str) -> bool:
        if not commit:
            return False
        code, _ = _git(["cat-file", "-e", f"{commit}^{{commit}}"], cwd=self.repo_root)
        return code == 0

    def path_changed_since(self, path: str, commit: str) -> tuple[bool, int]:
        """Has ``path`` changed since ``commit``? Returns ``(changed, n_commits)``.

        Uses ``--follow`` so a renamed file still counts as changed. Without it a
        rename looks like "no changes to this path", which would silently keep an
        entry fresh after the file moved — precisely the case where a stale entry
        is most dangerous, because the code it describes is now somewhere else.
        """
        if not path or not commit:
            return False, 0
        code, out = _git(
            ["rev-list", "--count", "--follow", f"{commit}..HEAD", "--", path],
            cwd=self.repo_root,
        )
        if code != 0:
            return False, 0
        try:
            count = int(out or "0")
        except ValueError:
            return False, 0
        return count > 0, count

    def last_commit_for_path(self, path: str) -> str:
        if not path:
            return ""
        code, out = _git(["log", "-1", "--format=%H", "--", path], cwd=self.repo_root)
        return out if code == 0 else ""

    # --- entry checks ------------------------------------------------------
    def check(self, entry: BrainEntry) -> AnchorStatus:
        """Determine the freshness of one entry."""
        if not entry.is_scoped:
            # Nothing to drift. A general convention is bounded by supersession
            # and retirement, not by file churn.
            return AnchorStatus(AnchorState.FRESH, reason="unscoped entry")

        if not entry.anchor_commit:
            return AnchorStatus(
                AnchorState.UNKNOWN,
                reason=(
                    f"anchored to {entry.anchor_path} but no commit was recorded, "
                    "so drift cannot be measured"
                ),
            )

        if not self.head_commit:
            return AnchorStatus(
                AnchorState.UNKNOWN,
                reason="repository HEAD is unavailable (no git, or not a repository)",
            )

        if not self.commit_exists(entry.anchor_commit):
            return AnchorStatus(
                AnchorState.UNKNOWN,
                reason=(
                    f"anchor commit {entry.anchor_commit[:8]} is not present in this "
                    "clone (shallow checkout, or history rewritten)"
                ),
            )

        changed, count = self.path_changed_since(entry.anchor_path, entry.anchor_commit)
        if not changed:
            return AnchorStatus(AnchorState.FRESH, reason=f"{entry.anchor_path} unchanged")

        last = self.last_commit_for_path(entry.anchor_path)
        return AnchorStatus(
            AnchorState.STALE,
            reason=(
                f"{entry.anchor_path} changed in {count} commit(s) since it was "
                f"anchored at {entry.anchor_commit[:8]}"
            ),
            last_commit=last,
            commits_since=count,
        )

    def sweep(self, entries: list[BrainEntry]) -> list[BrainEntry]:
        """Mark every stale entry in ``entries`` and return those that changed.

        Mutates the entries in place, because the caller owns the session and is
        responsible for persisting them. Returning the changed subset keeps the
        write path proportional to what actually moved rather than to the size of
        the Brain.
        """
        changed: list[BrainEntry] = []
        for entry in entries:
            if not entry.is_active:
                continue
            status = self.check(entry)
            if status.is_stale:
                entry.mark_stale(superseded_by="", reason=status.reason)
                changed.append(entry)
        if changed:
            log.info("brain.anchor.sweep", stale=len(changed), checked=len(entries))
        return changed


__all__ = ["AnchorChecker", "AnchorState", "AnchorStatus"]
