"""GitHub and email integrations (Stage 17: items 224, 226, 227).

All of these integrations share one contract — failures are reported results,
never exceptions — because they run after the session is already closed. These
tests are how the contract is enforced: every failure path below asserts a
returned ``GitHubResult(ok=False)``, not a raised error.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from openburrow.daemon.github import GitHubResult, open_pull_request, post_commit_status
from openburrow.daemon.notify import render_standup_text, send_email_digest

pytestmark = [pytest.mark.unit]


def _ok(url: str = "https://github.com/o/r/pull/1") -> GitHubResult:
    return GitHubResult(ok=True, detail="ok", url=url)


class TestOpenPullRequest:
    async def test_same_head_and_base_is_refused(self) -> None:
        result = await open_pull_request(
            token="t",
            repo="o/r",
            head_branch="main",
            base_branch="main",
            title="t",
            body="b",
        )
        assert result.ok is False
        assert "nothing to open" in result.detail

    async def test_empty_head_branch_is_refused(self) -> None:
        result = await open_pull_request(
            token="t", repo="o/r", head_branch="", base_branch="main", title="t", body="b"
        )
        assert result.ok is False

    async def test_gh_success_short_circuits_api(self) -> None:
        with patch("openburrow.daemon.github._gh", return_value=_ok()) as gh:
            result = await open_pull_request(
                token="t",
                repo="owner/repo",
                head_branch="openburrow/sess",
                base_branch="main",
                title="t",
                body="b",
            )
        assert result.ok is True
        assert gh.await_count == 1

    async def test_gh_failure_with_no_token_reports_not_raises(self) -> None:
        with patch(
            "openburrow.daemon.github._gh",
            return_value=GitHubResult(ok=False, detail="gh CLI not installed"),
        ):
            result = await open_pull_request(
                token="",
                repo="owner/repo",
                head_branch="b",
                base_branch="main",
                title="t",
                body="b",
            )
        assert result.ok is False
        assert "gh CLI not installed" in result.detail

    async def test_gh_failure_falls_back_to_rest_api(self) -> None:
        with (
            patch(
                "openburrow.daemon.github._gh",
                return_value=GitHubResult(ok=False, detail="no auth"),
            ),
            patch(
                "openburrow.daemon.github._api", return_value=_ok("https://github.com/o/r/pull/9")
            ) as api,
        ):
            result = await open_pull_request(
                token="tok",
                repo="owner/repo",
                head_branch="b",
                base_branch="main",
                title="t",
                body="b",
            )
        assert result.ok is True
        assert result.url == "https://github.com/o/r/pull/9"
        assert api.await_count == 1


class TestPostCommitStatus:
    async def test_invalid_state_is_refused_locally(self) -> None:
        result = await post_commit_status(token="t", repo="o/r", commit_sha="abc", state="green")
        assert result.ok is False
        assert "invalid status state" in result.detail

    async def test_empty_sha_is_refused(self) -> None:
        result = await post_commit_status(token="t", repo="o/r", commit_sha="", state="success")
        assert result.ok is False

    async def test_unparseable_repo_is_refused(self) -> None:
        result = await post_commit_status(
            token="t", repo="not-a-slug", commit_sha="a", state="success"
        )
        assert result.ok is False
        assert "unparseable" in result.detail

    async def test_gh_api_call_used_first(self) -> None:
        with patch("openburrow.daemon.github._gh", return_value=_ok("")) as gh:
            result = await post_commit_status(
                token="t",
                repo="o/r",
                commit_sha="abc123",
                state="failure",
                description="session failed",
            )
        assert result.ok is True
        gh.assert_awaited_once()
        assert gh.await_args is not None  # narrows the Optional for the subscript below
        argv = gh.await_args.args[0]
        assert "repos/o/r/statuses/abc123" in argv
        assert "state=failure" in argv


class TestParseRepo:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("owner/repo", ("owner", "repo")),
            ("https://github.com/owner/repo", ("owner", "repo")),
            ("https://github.com/owner/repo.git", ("owner", "repo")),
            ("git@github.com:owner/repo", ("owner", "repo")),
            ("owner", None),
            ("", None),
        ],
    )
    def test_forms(self, raw: str, expected: tuple[str, str] | None) -> None:
        from openburrow.daemon.github import _parse_repo

        assert _parse_repo(raw) == expected


class TestEmailDigest:
    def test_no_recipients_sends_nothing(self) -> None:
        report: dict[str, Any] = {"session_name": "s"}
        assert send_email_digest(report, host="localhost", recipients=[], sender="f@x") == 0

    def test_renders_before_sending(self) -> None:
        """The body is the standup text — the same one `burrow standup` shows."""
        report = {
            "session_name": "night-shift",
            "lanes": [{"lane_id": "lane_a", "highlights": ["implemented the parser"]}],
            "conversation": [{"at": "2026-09-16T10:00:00", "who": "alice", "what": "hello"}],
            "counts": {"negotiations": 2, "collisions_avoided": 1},
            "governance": [],
        }
        text = render_standup_text(report)
        assert "night-shift" in text
        assert "implemented the parser" in text
        assert "2 run, 1 collisions avoided" in text

    def test_smtp_failure_is_reported_not_raised(self) -> None:
        report: dict[str, Any] = {"session_name": "s"}
        with patch("smtplib.SMTP", side_effect=ConnectionRefusedError("no relay")):
            sent = send_email_digest(
                report,
                host="localhost",
                recipients=["a@example.com"],
                sender="f@x",
                port=2525,
                use_tls=False,
            )
        assert sent == 0
