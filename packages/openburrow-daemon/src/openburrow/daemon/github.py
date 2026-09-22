"""GitHub integration (Stage 17, items 226/227).

Two deliverables, one module:

* **PR auto-open** — on session completion, open a PR from the session branch
  into its base branch (item 226).
* **Status checks** — post the session outcome as a commit status on the head
  commit (item 227).

Both shells out to the ``gh`` CLI when it is available and falls back to the
REST API over ``httpx`` when it is not. The CLI path exists because an operator
who has already authenticated ``gh`` should not have to also manage a token in
``openburrow.yaml``; the API path exists because CI containers routinely have a
token and no ``gh`` binary. Both are attempted only when the corresponding
``github_*`` setting is on, and both degrade to a log line on failure: a PR that
did not open must not fail a session that already completed its actual work.

The design constraint both paths honour is the same one the rest of the notify
stack follows: integration failures are *reported*, never raised. The session is
already over by the time this runs; there is nothing left to gate on the result.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from typing import Any

from openburrow.core.logging import get_logger

log = get_logger(__name__)

#: Bounded because this runs inside the daemon's shutdown-adjacent paths.
_HTTP_TIMEOUT_S = 15.0


@dataclass(slots=True)
class GitHubResult:
    """The outcome of one GitHub delivery attempt — reported, never raised."""

    ok: bool
    detail: str
    url: str = ""


def _parse_repo(repo: str) -> tuple[str, str] | None:
    """``owner/name`` from a config value, tolerating URL and SCP forms."""
    slug = repo.strip()
    # SCP form first: git@github.com:owner/name — the colon, not a slash,
    # separates host from path.
    if slug.startswith("git@") and ":" in slug:
        slug = slug.split(":", 1)[1]
    elif slug.startswith(("http://", "https://")):
        slug = slug.removeprefix("https://").removeprefix("http://")
        slug = slug.split("/", 1)[-1] if "/" in slug else slug
        if ":" in slug:
            slug = slug.split(":", 1)[1]
    if slug.endswith(".git"):
        slug = slug.removesuffix(".git")
    parts = [part for part in slug.split("/") if part]
    if len(parts) < 2:
        return None
    return parts[-2], parts[-1]


async def _gh(argv: list[str], *, stdin: str | None = None) -> GitHubResult:
    """Run one ``gh`` invocation. Absent binary and non-zero exit are results."""
    if shutil.which("gh") is None:
        return GitHubResult(ok=False, detail="gh CLI not installed")
    try:
        import asyncio

        process = await asyncio.create_subprocess_exec(
            "gh",
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(_stdin(stdin)), timeout=60.0)
    except TimeoutError:
        return GitHubResult(ok=False, detail="gh timed out after 60s")
    except OSError as exc:
        return GitHubResult(ok=False, detail=f"gh failed to start: {exc}")
    if process.returncode != 0:
        return GitHubResult(
            ok=False,
            detail=stderr.decode("utf-8", "replace").strip()[:300] or f"exit {process.returncode}",
        )
    return GitHubResult(
        ok=True, detail="ok", url=stdout.decode("utf-8", "replace").strip().splitlines()[-1]
    )


def _stdin(value: str | None) -> bytes | None:
    return value.encode("utf-8") if value is not None else None


async def _api(
    method: str, path: str, token: str, *, json_body: dict[str, Any] | None = None
) -> GitHubResult:
    """One REST call. Used when ``gh`` is absent — CI containers, mostly."""
    import httpx

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S) as client:
        response = await client.request(
            method,
            f"https://api.github.com{path}",
            json=json_body,
            headers={
                "authorization": f"Bearer {token}",
                "accept": "application/vnd.github+json",
                "x-github-api-version": "2022-11-28",
            },
        )
    if response.status_code >= 300:
        return GitHubResult(
            ok=False, detail=f"{method} {path} -> {response.status_code}: {response.text[:200]}"
        )
    body: Any = response.json() if response.content else {}
    return GitHubResult(
        ok=True,
        detail="ok",
        url=str(body.get("html_url") or body.get("_links", {}).get("html", {}).get("href") or ""),
    )


async def open_pull_request(
    *,
    token: str,
    repo: str,
    head_branch: str,
    base_branch: str,
    title: str,
    body: str,
) -> GitHubResult:
    """Open a PR for a completed session (item 226).

    Prefers ``gh pr create`` (which reuses the operator's auth and resolves the
    repo from the checkout), falling back to the REST API with an explicit
    ``token``/``repo``.
    """
    if not head_branch or head_branch == base_branch:
        return GitHubResult(
            ok=False, detail=f"nothing to open: {head_branch!r} == base {base_branch!r}"
        )

    gh_result = await _gh(
        [
            "pr",
            "create",
            "--head",
            head_branch,
            "--base",
            base_branch,
            "--title",
            title,
            "--body-file",
            "-",
        ],
        stdin=body,
    )
    if gh_result.ok:
        return gh_result
    if not token or (parsed := _parse_repo(repo)) is None:
        log.warning("github.pr_skipped", reason=gh_result.detail)
        return GitHubResult(ok=False, detail=gh_result.detail)

    owner, name = parsed
    api = await _api(
        "POST",
        f"/repos/{owner}/{name}/pulls",
        token,
        json_body={
            "title": title,
            "head": head_branch,
            "base": base_branch,
            "body": body,
        },
    )
    if api.ok:
        return api
    log.warning("github.pr_failed", gh=gh_result.detail, api=api.detail)
    return api


async def post_commit_status(
    *,
    token: str,
    repo: str,
    commit_sha: str,
    state: str,
    context: str = "openburrow",
    description: str = "",
    target_url: str = "",
) -> GitHubResult:
    """Post a session outcome as a commit status (item 227).

    ``state`` is one of GitHub's four values; anything else is refused here
    rather than letting GitHub return a 422 with a longer error message.
    """
    if state not in {"error", "failure", "pending", "success"}:
        return GitHubResult(ok=False, detail=f"invalid status state: {state!r}")
    if not commit_sha:
        return GitHubResult(ok=False, detail="no head commit to mark")

    parsed = _parse_repo(repo)
    if parsed is None:
        return GitHubResult(ok=False, detail=f"unparseable github_repo: {repo!r}")
    owner, name = parsed

    gh_result = await _gh(
        [
            "api",
            f"repos/{owner}/{name}/statuses/{commit_sha}",
            "-f",
            f"state={state}",
            "-f",
            f"context={context}",
            "-f",
            f"description={description[:140]}",
            "-f",
            f"target_url={target_url}",
        ]
    )
    if gh_result.ok:
        return gh_result
    if not token:
        log.warning("github.status_skipped", reason=gh_result.detail)
        return GitHubResult(ok=False, detail=gh_result.detail)
    api = await _api(
        "POST",
        f"/repos/{owner}/{name}/statuses/{commit_sha}",
        token,
        json_body={
            "state": state,
            "context": context,
            "description": description[:140],
            "target_url": target_url,
        },
    )
    if api.ok:
        return api
    log.warning("github.status_failed", gh=gh_result.detail, api=api.detail)
    return api


__all__ = ["GitHubResult", "open_pull_request", "post_commit_status"]
