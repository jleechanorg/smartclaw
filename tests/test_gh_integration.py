"""Tests for orchestration.gh_integration — GitHub SCM logic ported from TS."""

import json
import subprocess
from unittest.mock import patch, MagicMock
from dataclasses import dataclass
from pathlib import Path

import pytest

from orchestration.gh_integration import (
    PRInfo,
    CIStatus,
    ReviewDecision,
    MergeReadiness,
    BOT_AUTHORS,
    gh,
    detect_pr,
    get_pr_state,
    get_pr_summary,
    get_ci_checks,
    get_ci_summary,
    get_reviews,
    get_review_decision,
    get_pending_comments,
    get_automated_comments,
    get_merge_readiness,
    merge_pr,
    close_pr,
)


# ---------------------------------------------------------------------------
# Helper: mock gh CLI output
# ---------------------------------------------------------------------------


def mock_gh_result(stdout: str):
    """Create a mock CompletedProcess for subprocess.run."""
    return subprocess.CompletedProcess(args=["gh"], returncode=0, stdout=stdout, stderr="")


def mock_gh_error(msg: str = "error"):
    return subprocess.CalledProcessError(returncode=1, cmd=["gh"], stderr=msg)


# ---------------------------------------------------------------------------
# PRInfo dataclass
# ---------------------------------------------------------------------------


class TestPRInfo:
    def test_fields(self):
        pr = PRInfo(number=42, url="https://github.com/o/r/pull/42",
                    title="Fix bug", owner="o", repo="r",
                    branch="fix-bug", base_branch="main", is_draft=False)
        assert pr.number == 42
        assert pr.repo == "r"


# ---------------------------------------------------------------------------
# BOT_AUTHORS
# ---------------------------------------------------------------------------


class TestBotAuthors:
    def test_contains_github_actions(self):
        assert "github-actions[bot]" in BOT_AUTHORS

    def test_contains_dependabot(self):
        assert "dependabot[bot]" in BOT_AUTHORS

    def test_contains_codecov(self):
        assert "codecov[bot]" in BOT_AUTHORS


# ---------------------------------------------------------------------------
# gh() wrapper
# ---------------------------------------------------------------------------


class TestGh:
    @patch("orchestration.gh_integration.subprocess.run")
    def test_returns_stdout(self, mock_run):
        mock_run.return_value = mock_gh_result('{"ok": true}')
        result = gh(["pr", "list"])
        assert result == '{"ok": true}'

    @patch("orchestration.gh_integration.subprocess.run",
           side_effect=subprocess.CalledProcessError(1, "gh", stderr="fail"))
    def test_raises_on_error(self, mock_run):
        with pytest.raises(RuntimeError, match="gh .* failed"):
            gh(["pr", "list"])


# ---------------------------------------------------------------------------
# detect_pr()
# ---------------------------------------------------------------------------


class TestDetectPR:
    @patch("orchestration.gh_integration.gh")
    def test_finds_pr(self, mock_gh):
        mock_gh.return_value = json.dumps([{
            "number": 123, "url": "https://github.com/o/r/pull/123",
            "title": "My PR", "headRefName": "feat-x",
            "baseRefName": "main", "isDraft": False,
        }])
        pr = detect_pr("feat-x", "o/r")
        assert pr is not None
        assert pr.number == 123
        assert pr.branch == "feat-x"

    @patch("orchestration.gh_integration.gh")
    def test_no_pr_found(self, mock_gh):
        mock_gh.return_value = "[]"
        pr = detect_pr("nonexistent-branch", "o/r")
        assert pr is None

    @patch("orchestration.gh_integration.gh", side_effect=RuntimeError("fail"))
    def test_error_returns_none(self, mock_gh):
        pr = detect_pr("feat-x", "o/r")
        assert pr is None

    def test_invalid_repo_format(self):
        with pytest.raises(ValueError, match="expected.*owner/repo"):
            detect_pr("feat-x", "invalid-repo")


# ---------------------------------------------------------------------------
# get_pr_state()
# ---------------------------------------------------------------------------


class TestGetPRState:
    @patch("orchestration.gh_integration.gh")
    def test_open(self, mock_gh):
        mock_gh.return_value = json.dumps({"state": "OPEN"})
        pr = PRInfo(number=1, url="", title="", owner="o", repo="r",
                    branch="b", base_branch="main", is_draft=False)
        assert get_pr_state(pr) == "open"

    @patch("orchestration.gh_integration.gh")
    def test_merged(self, mock_gh):
        mock_gh.return_value = json.dumps({"state": "MERGED"})
        pr = PRInfo(number=1, url="", title="", owner="o", repo="r",
                    branch="b", base_branch="main", is_draft=False)
        assert get_pr_state(pr) == "merged"

    @patch("orchestration.gh_integration.gh")
    def test_closed(self, mock_gh):
        mock_gh.return_value = json.dumps({"state": "CLOSED"})
        pr = PRInfo(number=1, url="", title="", owner="o", repo="r",
                    branch="b", base_branch="main", is_draft=False)
        assert get_pr_state(pr) == "closed"


# ---------------------------------------------------------------------------
# get_ci_checks()
# ---------------------------------------------------------------------------


class TestGetCIChecks:
    @patch("orchestration.gh_integration.gh")
    def test_parses_checks(self, mock_gh):
        mock_gh.return_value = json.dumps([
            {"name": "build", "state": "SUCCESS", "link": "https://ci/1",
             "startedAt": "2026-01-01T00:00:00Z", "completedAt": "2026-01-01T00:05:00Z"},
            {"name": "lint", "state": "FAILURE", "link": "https://ci/2",
             "startedAt": "", "completedAt": ""},
        ])
        pr = PRInfo(number=1, url="", title="", owner="o", repo="r",
                    branch="b", base_branch="main", is_draft=False)
        checks = get_ci_checks(pr)
        assert len(checks) == 2
        assert checks[0]["status"] == "passed"
        assert checks[1]["status"] == "failed"

    @patch("orchestration.gh_integration.gh")
    def test_pending_state(self, mock_gh):
        mock_gh.return_value = json.dumps([
            {"name": "build", "state": "PENDING", "link": "", "startedAt": "", "completedAt": ""},
        ])
        pr = PRInfo(number=1, url="", title="", owner="o", repo="r",
                    branch="b", base_branch="main", is_draft=False)
        checks = get_ci_checks(pr)
        assert checks[0]["status"] == "pending"

    @patch("orchestration.gh_integration.gh")
    def test_unknown_state_fail_closed(self, mock_gh):
        """Unknown CI state should fail closed (report as failed)."""
        mock_gh.return_value = json.dumps([
            {"name": "mystery", "state": "WEIRD", "link": "", "startedAt": "", "completedAt": ""},
        ])
        pr = PRInfo(number=1, url="", title="", owner="o", repo="r",
                    branch="b", base_branch="main", is_draft=False)
        checks = get_ci_checks(pr)
        assert checks[0]["status"] == "failed"

    @patch("orchestration.gh_integration.gh", side_effect=RuntimeError("fail"))
    def test_error_propagates(self, mock_gh):
        """CI check fetch failure must propagate, not silently return []."""
        pr = PRInfo(number=1, url="", title="", owner="o", repo="r",
                    branch="b", base_branch="main", is_draft=False)
        with pytest.raises(RuntimeError):
            get_ci_checks(pr)


# ---------------------------------------------------------------------------
# get_ci_summary()
# ---------------------------------------------------------------------------


class TestGetCISummary:
    @patch("orchestration.gh_integration.get_ci_checks")
    def test_all_passing(self, mock_checks):
        mock_checks.return_value = [
            {"name": "build", "status": "passed"},
            {"name": "lint", "status": "passed"},
        ]
        pr = PRInfo(number=1, url="", title="", owner="o", repo="r",
                    branch="b", base_branch="main", is_draft=False)
        assert get_ci_summary(pr) == CIStatus.PASSING

    @patch("orchestration.gh_integration.get_ci_checks")
    def test_one_failing(self, mock_checks):
        mock_checks.return_value = [
            {"name": "build", "status": "passed"},
            {"name": "lint", "status": "failed"},
        ]
        pr = PRInfo(number=1, url="", title="", owner="o", repo="r",
                    branch="b", base_branch="main", is_draft=False)
        assert get_ci_summary(pr) == CIStatus.FAILING

    @patch("orchestration.gh_integration.get_ci_checks")
    def test_pending(self, mock_checks):
        mock_checks.return_value = [
            {"name": "build", "status": "pending"},
        ]
        pr = PRInfo(number=1, url="", title="", owner="o", repo="r",
                    branch="b", base_branch="main", is_draft=False)
        assert get_ci_summary(pr) == CIStatus.PENDING

    @patch("orchestration.gh_integration.get_ci_checks")
    def test_empty_checks(self, mock_checks):
        mock_checks.return_value = []
        pr = PRInfo(number=1, url="", title="", owner="o", repo="r",
                    branch="b", base_branch="main", is_draft=False)
        assert get_ci_summary(pr) == CIStatus.NONE

    @patch("orchestration.gh_integration.get_ci_checks", side_effect=RuntimeError("fail"))
    @patch("orchestration.gh_integration.get_pr_state", return_value="open")
    def test_error_fail_closed(self, mock_state, mock_checks):
        """When CI check fetch fails for open PR, report as failing (fail-closed)."""
        pr = PRInfo(number=1, url="", title="", owner="o", repo="r",
                    branch="b", base_branch="main", is_draft=False)
        assert get_ci_summary(pr) == CIStatus.FAILING

    @patch("orchestration.gh_integration.get_ci_checks", side_effect=RuntimeError("fail"))
    @patch("orchestration.gh_integration.get_pr_state", return_value="merged")
    def test_merged_pr_returns_none(self, mock_state, mock_checks):
        """Merged PRs with fetch error should return NONE, not failing."""
        pr = PRInfo(number=1, url="", title="", owner="o", repo="r",
                    branch="b", base_branch="main", is_draft=False)
        assert get_ci_summary(pr) == CIStatus.NONE


# ---------------------------------------------------------------------------
# get_reviews()
# ---------------------------------------------------------------------------


class TestGetReviews:
    @patch("orchestration.gh_integration.gh")
    def test_parses_reviews(self, mock_gh):
        mock_gh.return_value = json.dumps({"reviews": [
            {"author": {"login": "reviewer1"}, "state": "APPROVED",
             "body": "LGTM", "submittedAt": "2026-01-01T00:00:00Z"},
        ]})
        pr = PRInfo(number=1, url="", title="", owner="o", repo="r",
                    branch="b", base_branch="main", is_draft=False)
        reviews = get_reviews(pr)
        assert len(reviews) == 1
        assert reviews[0]["state"] == "approved"
        assert reviews[0]["author"] == "reviewer1"


# ---------------------------------------------------------------------------
# get_review_decision()
# ---------------------------------------------------------------------------


class TestGetReviewDecision:
    @patch("orchestration.gh_integration.gh")
    def test_approved(self, mock_gh):
        mock_gh.return_value = json.dumps({"reviewDecision": "APPROVED"})
        pr = PRInfo(number=1, url="", title="", owner="o", repo="r",
                    branch="b", base_branch="main", is_draft=False)
        assert get_review_decision(pr) == ReviewDecision.APPROVED

    @patch("orchestration.gh_integration.gh")
    def test_changes_requested(self, mock_gh):
        mock_gh.return_value = json.dumps({"reviewDecision": "CHANGES_REQUESTED"})
        pr = PRInfo(number=1, url="", title="", owner="o", repo="r",
                    branch="b", base_branch="main", is_draft=False)
        assert get_review_decision(pr) == ReviewDecision.CHANGES_REQUESTED

    @patch("orchestration.gh_integration.gh")
    def test_none(self, mock_gh):
        mock_gh.return_value = json.dumps({"reviewDecision": ""})
        pr = PRInfo(number=1, url="", title="", owner="o", repo="r",
                    branch="b", base_branch="main", is_draft=False)
        assert get_review_decision(pr) == ReviewDecision.NONE


# ---------------------------------------------------------------------------
# get_pending_comments()
# ---------------------------------------------------------------------------


class TestGetPendingComments:
    @patch("orchestration.gh_integration.gh")
    def test_uses_review_threads_first_100_query(self, mock_gh):
        mock_gh.return_value = json.dumps({
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {"totalCount": 0, "nodes": []},
                    }
                }
            }
        })
        pr = PRInfo(number=1, url="", title="", owner="o", repo="r",
                    branch="b", base_branch="main", is_draft=False)
        get_pending_comments(pr)
        call_args = mock_gh.call_args[0][0]
        query_arg = next(arg for arg in call_args if arg.startswith("query=query"))
        assert "reviewThreads(first: 100)" in query_arg
        assert "totalCount" in query_arg

    @patch("orchestration.gh_integration.gh")
    def test_filters_resolved(self, mock_gh):
        mock_gh.return_value = json.dumps({"data": {"repository": {"pullRequest": {
            "reviewThreads": {"totalCount": 2, "nodes": [
                {"isResolved": True, "comments": {"nodes": [
                    {"id": "1", "author": {"login": "human"}, "body": "fix this",
                     "path": "a.py", "line": 10, "url": "https://...", "createdAt": "2026-01-01T00:00:00Z"},
                ]}},
                {"isResolved": False, "comments": {"nodes": [
                    {"id": "2", "author": {"login": "human"}, "body": "another issue",
                     "path": "b.py", "line": 20, "url": "https://...", "createdAt": "2026-01-01T00:00:00Z"},
                ]}},
            ]},
        }}}})
        pr = PRInfo(number=1, url="", title="", owner="o", repo="r",
                    branch="b", base_branch="main", is_draft=False)
        comments = get_pending_comments(pr)
        assert len(comments) == 1
        assert comments[0]["body"] == "another issue"

    @patch("orchestration.gh_integration.gh")
    def test_filters_bots(self, mock_gh):
        mock_gh.return_value = json.dumps({"data": {"repository": {"pullRequest": {
            "reviewThreads": {"totalCount": 1, "nodes": [
                {"isResolved": False, "comments": {"nodes": [
                    {"id": "1", "author": {"login": "github-actions[bot]"}, "body": "auto msg",
                     "path": "a.py", "line": 10, "url": "https://...", "createdAt": "2026-01-01T00:00:00Z"},
                ]}},
            ]},
        }}}})
        pr = PRInfo(number=1, url="", title="", owner="o", repo="r",
                    branch="b", base_branch="main", is_draft=False)
        comments = get_pending_comments(pr)
        assert len(comments) == 0

    @patch("orchestration.gh_integration.gh")
    def test_bot_heavy_thread_with_hidden_human_comment_fails_closed(self, mock_gh):
        """When all visible comments are bots but more exist unfetched, include thread (fail-closed)."""
        mock_gh.return_value = json.dumps({"data": {"repository": {"pullRequest": {
            "reviewThreads": {"totalCount": 1, "nodes": [
                {"isResolved": False, "comments": {"totalCount": 55, "nodes": [
                    {"id": "1", "author": {"login": "github-actions[bot]"}, "body": "auto msg",
                     "path": "a.py", "line": 10, "url": "https://...", "createdAt": "2026-01-01T00:00:00Z"},
                ]}},
            ]},
        }}}})
        pr = PRInfo(number=1, url="", title="", owner="o", repo="r",
                    branch="b", base_branch="main", is_draft=False)
        comments = get_pending_comments(pr)
        # Should NOT be filtered out — human comment may be hidden beyond page boundary
        assert len(comments) == 1

    @patch("orchestration.gh_integration.gh")
    def test_all_bot_thread_with_all_fetched_is_filtered(self, mock_gh):
        """When all comments are bots and all are fetched, thread is correctly filtered out."""
        mock_gh.return_value = json.dumps({"data": {"repository": {"pullRequest": {
            "reviewThreads": {"totalCount": 1, "nodes": [
                {"isResolved": False, "comments": {"totalCount": 1, "nodes": [
                    {"id": "1", "author": {"login": "github-actions[bot]"}, "body": "auto msg",
                     "path": "a.py", "line": 10, "url": "https://...", "createdAt": "2026-01-01T00:00:00Z"},
                ]}},
            ]},
        }}}})
        pr = PRInfo(number=1, url="", title="", owner="o", repo="r",
                    branch="b", base_branch="main", is_draft=False)
        comments = get_pending_comments(pr)
        assert len(comments) == 0

    @patch("orchestration.gh_integration.gh")
    def test_pagination_overflow_fails_closed(self, mock_gh):
        """When totalCount > fetched nodes, fail closed with specific message."""
        mock_gh.return_value = json.dumps({"data": {"repository": {"pullRequest": {
            "reviewThreads": {"totalCount": 150, "nodes": [
                {"isResolved": False, "comments": {"totalCount": 1, "nodes": [
                    {"id": "1", "author": {"login": "human"}, "body": "issue",
                     "path": "a.py", "line": 1, "url": "https://...", "createdAt": "2026-01-01T00:00:00Z"},
                ]}},
            ]},
        }}}})
        pr = PRInfo(number=1, url="", title="", owner="o", repo="r",
                    branch="b", base_branch="main", is_draft=False)
        with pytest.raises(RuntimeError, match="150 review threads"):
            get_pending_comments(pr)

    @patch("orchestration.gh_integration.gh")
    def test_missing_totalcount_fails_closed(self, mock_gh):
        """When totalCount field is absent from response, fail closed (not silently pass)."""
        mock_gh.return_value = json.dumps({"data": {"repository": {"pullRequest": {
            "reviewThreads": {"nodes": [
                {"isResolved": False, "comments": {"totalCount": 1, "nodes": [
                    {"id": "1", "author": {"login": "human"}, "body": "issue",
                     "path": "a.py", "line": 1, "url": "https://...", "createdAt": "2026-01-01T00:00:00Z"},
                ]}},
            ]},
        }}}})
        pr = PRInfo(number=1, url="", title="", owner="o", repo="r",
                    branch="b", base_branch="main", is_draft=False)
        with pytest.raises(RuntimeError, match="totalCount"):
            get_pending_comments(pr)

    @patch("orchestration.gh_integration.gh")
    def test_overflow_error_not_rewrapped(self, mock_gh):
        """Overflow RuntimeError should propagate with specific message, not generic wrapper."""
        mock_gh.return_value = json.dumps({"data": {"repository": {"pullRequest": {
            "reviewThreads": {"totalCount": 150, "nodes": [
                {"isResolved": False, "comments": {"totalCount": 1, "nodes": [
                    {"id": "1", "author": {"login": "human"}, "body": "issue",
                     "path": "a.py", "line": 1, "url": "https://...", "createdAt": "2026-01-01T00:00:00Z"},
                ]}},
            ]},
        }}}})
        pr = PRInfo(number=1, url="", title="", owner="o", repo="r",
                    branch="b", base_branch="main", is_draft=False)
        with pytest.raises(RuntimeError, match="150 review threads"):
            get_pending_comments(pr)

    @patch("orchestration.gh_integration.gh", side_effect=RuntimeError("fail"))
    def test_error_propagates(self, mock_gh):
        pr = PRInfo(number=1, url="", title="", owner="o", repo="r",
                    branch="b", base_branch="main", is_draft=False)
        with pytest.raises(RuntimeError, match="Unable to load reviewThreads"):
            get_pending_comments(pr)


# ---------------------------------------------------------------------------
# get_merge_readiness()
# ---------------------------------------------------------------------------


class TestGetMergeReadiness:
    @patch("orchestration.gh_integration.gh")
    @patch("orchestration.gh_integration.get_pr_state", return_value="open")
    @patch("orchestration.gh_integration.get_ci_summary", return_value=CIStatus.PASSING)
    @patch("orchestration.gh_integration.get_pending_comments", return_value=[])
    def test_ready_to_merge(self, mock_pending, mock_ci, mock_state, mock_gh):
        mock_gh.return_value = json.dumps({
            "mergeable": "MERGEABLE", "reviewDecision": "APPROVED",
            "mergeStateStatus": "CLEAN", "isDraft": False,
        })
        pr = PRInfo(number=1, url="", title="", owner="o", repo="r",
                    branch="b", base_branch="main", is_draft=False)
        result = get_merge_readiness(pr)
        assert result.mergeable is True
        assert result.blockers == []

    @patch("orchestration.gh_integration.gh")
    @patch("orchestration.gh_integration.get_pr_state", return_value="open")
    @patch("orchestration.gh_integration.get_ci_summary", return_value=CIStatus.FAILING)
    @patch("orchestration.gh_integration.get_pending_comments", return_value=[])
    def test_ci_failing_blocks(self, mock_pending, mock_ci, mock_state, mock_gh):
        mock_gh.return_value = json.dumps({
            "mergeable": "MERGEABLE", "reviewDecision": "APPROVED",
            "mergeStateStatus": "CLEAN", "isDraft": False,
        })
        pr = PRInfo(number=1, url="", title="", owner="o", repo="r",
                    branch="b", base_branch="main", is_draft=False)
        result = get_merge_readiness(pr)
        assert result.mergeable is False
        assert any("CI" in b for b in result.blockers)

    @patch("orchestration.gh_integration.get_pr_state", return_value="merged")
    def test_merged_pr_is_ready(self, mock_state):
        pr = PRInfo(number=1, url="", title="", owner="o", repo="r",
                    branch="b", base_branch="main", is_draft=False)
        result = get_merge_readiness(pr)
        assert result.mergeable is True

    @patch("orchestration.gh_integration.gh")
    @patch("orchestration.gh_integration.get_pr_state", return_value="open")
    @patch("orchestration.gh_integration.get_ci_summary", return_value=CIStatus.PASSING)
    @patch("orchestration.gh_integration.get_pending_comments", return_value=[])
    def test_draft_blocks(self, mock_pending, mock_ci, mock_state, mock_gh):
        mock_gh.return_value = json.dumps({
            "mergeable": "MERGEABLE", "reviewDecision": "APPROVED",
            "mergeStateStatus": "CLEAN", "isDraft": True,
        })
        pr = PRInfo(number=1, url="", title="", owner="o", repo="r",
                    branch="b", base_branch="main", is_draft=False)
        result = get_merge_readiness(pr)
        assert result.mergeable is False
        assert any("draft" in b.lower() for b in result.blockers)

    @patch("orchestration.gh_integration.gh")
    @patch("orchestration.gh_integration.get_pr_state", return_value="open")
    @patch("orchestration.gh_integration.get_ci_summary", return_value=CIStatus.PASSING)
    @patch("orchestration.gh_integration.get_pending_comments", return_value=[])
    def test_conflicts_block(self, mock_pending, mock_ci, mock_state, mock_gh):
        mock_gh.return_value = json.dumps({
            "mergeable": "CONFLICTING", "reviewDecision": "APPROVED",
            "mergeStateStatus": "CLEAN", "isDraft": False,
        })
        pr = PRInfo(number=1, url="", title="", owner="o", repo="r",
                    branch="b", base_branch="main", is_draft=False)
        result = get_merge_readiness(pr)
        assert result.mergeable is False
        assert any("conflict" in b.lower() for b in result.blockers)

    @patch("orchestration.gh_integration.gh")
    @patch("orchestration.gh_integration.get_pr_state", return_value="open")
    @patch("orchestration.gh_integration.get_ci_summary", return_value=CIStatus.PASSING)
    @patch(
        "orchestration.gh_integration.get_pending_comments",
        return_value=[
            {
                "id": "thread-1",
                "author": "human",
                "body": "Please fix this first",
                "path": "a.py",
                "line": 10,
                "is_resolved": False,
                "created_at": "2026-01-01T00:00:00Z",
                "url": "https://example.com/comment/1",
            }
        ],
    )
    def test_unresolved_threads_block_merge(self, mock_pending, mock_ci, mock_state, mock_gh):
        mock_gh.return_value = json.dumps({
            "mergeable": "MERGEABLE", "reviewDecision": "APPROVED",
            "mergeStateStatus": "CLEAN", "isDraft": False,
        })
        pr = PRInfo(number=1, url="", title="", owner="o", repo="r",
                    branch="b", base_branch="main", is_draft=False)
        result = get_merge_readiness(pr)
        assert result.mergeable is False
        assert any("unresolved" in b.lower() and "thread" in b.lower() for b in result.blockers)

    @patch("orchestration.gh_integration.gh")
    @patch("orchestration.gh_integration.get_pr_state", return_value="open")
    @patch("orchestration.gh_integration.get_ci_summary", return_value=CIStatus.PASSING)
    @patch("orchestration.gh_integration.get_pending_comments", side_effect=RuntimeError("graphql timeout"))
    def test_unresolved_thread_check_failure_blocks_merge(self, mock_pending, mock_ci, mock_state, mock_gh):
        mock_gh.return_value = json.dumps({
            "mergeable": "MERGEABLE", "reviewDecision": "APPROVED",
            "mergeStateStatus": "CLEAN", "isDraft": False,
        })
        pr = PRInfo(number=1, url="", title="", owner="o", repo="r",
                    branch="b", base_branch="main", is_draft=False)
        result = get_merge_readiness(pr)
        assert result.mergeable is False
        assert any("unable to verify" in b.lower() and "thread" in b.lower() for b in result.blockers)


# ---------------------------------------------------------------------------
# merge_pr()
# ---------------------------------------------------------------------------


class TestMergePR:
    @patch("orchestration.gh_integration.gh")
    def test_squash_merge(self, mock_gh):
        pr = PRInfo(number=1, url="", title="", owner="o", repo="r",
                    branch="b", base_branch="main", is_draft=False)
        merge_pr(pr)
        call_args = mock_gh.call_args[0][0]
        assert "--squash" in call_args
        assert "--delete-branch" in call_args

    @patch("orchestration.gh_integration.gh")
    def test_rebase_merge(self, mock_gh):
        pr = PRInfo(number=1, url="", title="", owner="o", repo="r",
                    branch="b", base_branch="main", is_draft=False)
        merge_pr(pr, method="rebase")
        call_args = mock_gh.call_args[0][0]
        assert "--rebase" in call_args

    @patch("orchestration.gh_integration.gh")
    def test_merge_commit(self, mock_gh):
        pr = PRInfo(number=1, url="", title="", owner="o", repo="r",
                    branch="b", base_branch="main", is_draft=False)
        merge_pr(pr, method="merge")
        call_args = mock_gh.call_args[0][0]
        assert "--merge" in call_args


# ---------------------------------------------------------------------------
# close_pr()
# ---------------------------------------------------------------------------


class TestClosePR:
    @patch("orchestration.gh_integration.gh")
    def test_closes_pr(self, mock_gh):
        pr = PRInfo(number=42, url="", title="", owner="o", repo="r",
                    branch="b", base_branch="main", is_draft=False)
        close_pr(pr)
        call_args = mock_gh.call_args[0][0]
        assert "close" in call_args
        assert "42" in call_args


# ---------------------------------------------------------------------------
# get_pr_summary()
# ---------------------------------------------------------------------------


class TestGetPRSummary:
    @patch("orchestration.gh_integration.gh")
    def test_returns_summary(self, mock_gh):
        mock_gh.return_value = json.dumps({
            "state": "OPEN", "title": "Fix bug",
            "additions": 50, "deletions": 10,
        })
        pr = PRInfo(number=1, url="", title="", owner="o", repo="r",
                    branch="b", base_branch="main", is_draft=False)
        summary = get_pr_summary(pr)
        assert summary["state"] == "open"
        assert summary["title"] == "Fix bug"
        assert summary["additions"] == 50
        assert summary["deletions"] == 10

    @patch("orchestration.gh_integration.gh")
    def test_merged_state(self, mock_gh):
        mock_gh.return_value = json.dumps({
            "state": "MERGED", "title": "Done",
            "additions": 100, "deletions": 0,
        })
        pr = PRInfo(number=1, url="", title="", owner="o", repo="r",
                    branch="b", base_branch="main", is_draft=False)
        summary = get_pr_summary(pr)
        assert summary["state"] == "merged"


# ---------------------------------------------------------------------------
# get_automated_comments()
# ---------------------------------------------------------------------------


class TestGetAutomatedComments:
    @patch("orchestration.gh_integration.gh")
    def test_returns_bot_comments(self, mock_gh):
        mock_gh.return_value = json.dumps([
            {"id": 1, "user": {"login": "codecov[bot]"}, "body": "Coverage report",
             "path": "a.py", "line": 10, "original_line": None,
             "created_at": "2026-01-01T00:00:00Z", "html_url": "https://..."},
            {"id": 2, "user": {"login": "human-dev"}, "body": "Looks good",
             "path": "b.py", "line": 5, "original_line": None,
             "created_at": "2026-01-01T00:00:00Z", "html_url": "https://..."},
        ])
        pr = PRInfo(number=1, url="", title="", owner="o", repo="r",
                    branch="b", base_branch="main", is_draft=False)
        comments = get_automated_comments(pr)
        assert len(comments) == 1
        assert comments[0]["bot_name"] == "codecov[bot]"

    @patch("orchestration.gh_integration.gh")
    def test_severity_detection(self, mock_gh):
        mock_gh.return_value = json.dumps([
            {"id": 1, "user": {"login": "sonarcloud[bot]"},
             "body": "Bug: potential issue found in this code",
             "path": "a.py", "line": 10, "original_line": None,
             "created_at": "2026-01-01T00:00:00Z", "html_url": "https://..."},
        ])
        pr = PRInfo(number=1, url="", title="", owner="o", repo="r",
                    branch="b", base_branch="main", is_draft=False)
        comments = get_automated_comments(pr)
        assert comments[0]["severity"] == "error"

    @patch("orchestration.gh_integration.gh", side_effect=RuntimeError("fail"))
    def test_error_returns_empty(self, mock_gh):
        pr = PRInfo(number=1, url="", title="", owner="o", repo="r",
                    branch="b", base_branch="main", is_draft=False)
        comments = get_automated_comments(pr)
        assert comments == []

