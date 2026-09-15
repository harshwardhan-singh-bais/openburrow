"""Path derivation, specifically the Windows control-plane endpoint.

The pipe name used to be derived from the username alone. On POSIX the control
endpoint lives in the repo's own ``.openburrow/``, so two repos get two daemons;
on Windows they would have shared one name — and because Windows refuses a second
server on a bound pipe name (``PermissionError [WinError 5]``), the first repo to
start a daemon would have owned the endpoint for the whole machine and every
other repo's daemon would have died at bind.

These assertions run on every platform. ``pipe_name`` does not branch on the OS,
and testing it only on Windows would mean the collision could be reintroduced by
anyone developing on Linux.
"""

from __future__ import annotations

from pathlib import Path

from openburrow.core.paths import BurrowPaths

PIPE_PREFIX = "\\\\.\\pipe\\openburrow-"


def test_pipe_name_is_namespaced_by_repo(tmp_path: Path) -> None:
    first = BurrowPaths.for_repo(tmp_path / "repo-one")
    second = BurrowPaths.for_repo(tmp_path / "repo-two")

    assert first.pipe_name != second.pipe_name


def test_pipe_name_is_stable_for_one_repo(tmp_path: Path) -> None:
    """Two resolutions of the same repo must agree, or nothing can reconnect."""
    repo = tmp_path / "repo"
    assert BurrowPaths.for_repo(repo).pipe_name == BurrowPaths.for_repo(repo).pipe_name


def test_pipe_name_is_short_enough_for_the_windows_limit(tmp_path: Path) -> None:
    """A pipe name is capped at 256 characters; a deep repo path is not."""
    deep = tmp_path.joinpath(*[f"segment-{index:02d}" for index in range(20)])
    name = BurrowPaths.for_repo(deep).pipe_name

    assert name.startswith(PIPE_PREFIX)
    assert len(name) < 100
    # The path itself is nowhere near fitting in the name, which is the point.
    assert len(str(deep)) > len(name)


def test_pipe_name_is_case_insensitive_about_the_repo_path(tmp_path: Path) -> None:
    """Windows paths are case-insensitive, so the digest has to be too.

    Otherwise ``C:\\Repo`` and ``c:\\repo`` would be two daemons for one
    directory — the same collision, arrived at from the other direction.
    """
    upper = BurrowPaths.for_repo(tmp_path / "Repo")
    lower = BurrowPaths.for_repo(tmp_path / "repo")

    assert upper.pipe_name == lower.pipe_name
