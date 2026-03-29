"""Tests for coderabbit_gate: CodeRabbit review status checker for PR merge gates.

These tests follow TDD: they will fail until coderabbit_gate.py is implemented.
"""

from __future__ import annotations

import json
import pytest
from dataclasses import dataclass
from unittest.mock import patch, MagicMock

# These imports will fail until coderabbit_gate.py is implemented (TDD)
from orchestration.coderabbit_gate import (
    check_coderabbit,
    GateResult,
    CodeRabbitGateError,
)


# ---------------------------------------------------------------------------
# Test fixtures and helpers
# ---------------------------------------------------------------------------


CODERABBIT_LOGIN = "coderabbitai[bot]"


@dataclass
class MockReview:
    """Mock review data from GitHub API."""

    author: str
    state: str  # "APPROVED", "CHANGES_REQUESTED", "COMMENTED", "PENDING"


def make_gh_reviews_response(reviews: list[MockReview]) -> str:
    """Convert mock reviews to JSON string as gh CLI would return."""
    return json.dumps({
        "reviews": [
            {
                "author": {"login": r.author},
                "state": r.state,
                "body": None,
                "submittedAt": "2026-03-14T10:00:00Z",
            }
            for r in reviews
        ]
    })


def make_gh_pr_response(pr_number: int, repo: str) -> str:
    """Make a basic PR view response."""
    owner, repo_name = repo.split("/")
    return json.dumps({
        "number": pr_number,
        "url": f"https://github.com/{repo}/pull/{pr_number}",
        "title": "Test PR",
        "headRefName": "feature/test",
        "baseRefName": "main",
        "isDraft": False,
    })


# ---------------------------------------------------------------------------
# Test: PR with CodeRabbit approval -> passes
# ---------------------------------------------------------------------------


@patch("subprocess.run")
def test_coderabbit_approved_passes(mock_run: MagicMock) -> None:
    """PR with CodeRabbit approval should pass the gate."""
    # Setup: CodeRabbit approved
    mock_run.return_value = MagicMock(
        stdout=make_gh_reviews_response([
            MockReview(author="coderabbitai[bot]", state="APPROVED"),
        ]),
        returncode=0,
    )

    result = check_coderabbit("jleechanorg", "test-repo", 42)

    assert result.passed is True
    assert result.reviewer_login == "coderabbitai[bot]"
    assert "approved" in result.reason.lower()


@patch("subprocess.run")
def test_coderabbit_approved_with_other_reviews(mock_run: MagicMock) -> None:
    """CodeRabbit approval passes even with other reviewer approvals."""
    mock_run.return_value = MagicMock(
        stdout=make_gh_reviews_response([
            MockReview(author="human-reviewer", state="APPROVED"),
            MockReview(author="coderabbitai[bot]", state="APPROVED"),
            MockReview(author="another-bot", state="APPROVED"),
        ]),
        returncode=0,
    )

    result = check_coderabbit("jleechanorg", "test-repo", 42)

    assert result.passed is True
    assert result.reviewer_login == "coderabbitai[bot]"


# ---------------------------------------------------------------------------
# Test: PR with CodeRabbit rate-limited -> passes (acceptable)
# ---------------------------------------------------------------------------


@patch("subprocess.run")
def test_coderabbit_rate_limited_passes(mock_run: MagicMock) -> None:
    """PR where CodeRabbit is rate-limited should pass (acceptable)."""
    # Rate-limited shows as COMMENTED or PENDING in some cases
    mock_run.return_value = MagicMock(
        stdout=make_gh_reviews_response([
            MockReview(author="coderabbitai[bot]", state="COMMENTED"),
        ]),
        returncode=0,
    )

    result = check_coderabbit("jleechanorg", "test-repo", 42)

    # Rate-limited is acceptable (passes)
    assert result.passed is True
    assert "coderabbit" in result.reason.lower() or "rate" in result.reason.lower()


# ---------------------------------------------------------------------------
# Test: PR with CodeRabbit "changes requested" -> blocks
# ---------------------------------------------------------------------------


@patch("subprocess.run")
def test_coderabbit_changes_requested_blocks(mock_run: MagicMock) -> None:
    """PR with CodeRabbit 'changes requested' should block the gate."""
    mock_run.return_value = MagicMock(
        stdout=make_gh_reviews_response([
            MockReview(author="coderabbitai[bot]", state="CHANGES_REQUESTED"),
        ]),
        returncode=0,
    )

    result = check_coderabbit("jleechanorg", "test-repo", 42)

    assert result.passed is False
    assert result.reviewer_login == "coderabbitai[bot]"
    assert "changes" in result.reason.lower()


@patch("subprocess.run")
def test_coderabbit_changes_requested_with_approval(mock_run: MagicMock) -> None:
    """CHANGES_REQUESTED takes precedence over APPROVED from same reviewer."""
    mock_run.return_value = MagicMock(
        stdout=make_gh_reviews_response([
            MockReview(author="coderabbitai[bot]", state="APPROVED"),
            MockReview(author="coderabbitai[bot]", state="CHANGES_REQUESTED"),
        ]),
        returncode=0,
    )

    result = check_coderabbit("jleechanorg", "test-repo", 42)

    # Latest review state should win
    assert result.passed is False
    assert "changes" in result.reason.lower()


# ---------------------------------------------------------------------------
# Test: PR with no CodeRabbit review -> passes (not required)
# ---------------------------------------------------------------------------


@patch("subprocess.run")
def test_no_coderabbit_review_passes(mock_run: MagicMock) -> None:
    """PR with no CodeRabbit review should pass (not required)."""
    mock_run.return_value = MagicMock(
        stdout=make_gh_reviews_response([
            MockReview(author="human-reviewer", state="APPROVED"),
        ]),
        returncode=0,
    )

    result = check_coderabbit("jleechanorg", "test-repo", 42)

    assert result.passed is True
    assert result.reviewer_login is None
    assert "no review" in result.reason.lower() or "not required" in result.reason.lower()


@patch("subprocess.run")
def test_empty_reviews_passes(mock_run: MagicMock) -> None:
    """PR with no reviews at all should pass (CodeRabbit not required)."""
    mock_run.return_value = MagicMock(
        stdout=json.dumps({"reviews": []}),
        returncode=0,
    )

    result = check_coderabbit("jleechanorg", "test-repo", 42)

    assert result.passed is True
    assert "no review" in result.reason.lower() or "not required" in result.reason.lower()


# ---------------------------------------------------------------------------
# Test: GitHub API error -> warn, don't block (fail-open)
# ---------------------------------------------------------------------------


@patch("subprocess.run")
def test_github_api_error_warns_doesnt_block(mock_run: MagicMock) -> None:
    """GitHub API error should warn but not block (fail-open)."""
    # Simulate gh CLI failure
    mock_run.side_effect = Exception("GitHub API rate limit exceeded")

    result = check_coderabbit("jleechanorg", "test-repo", 42)

    # Fail-open: gate passes with warning
    assert result.passed is True
    assert "error" in result.reason.lower() or "warning" in result.reason.lower()


@patch("subprocess.run")
def test_gh_cli_not_found_fail_open(mock_run: MagicMock) -> None:
    """gh CLI not found should fail-open (pass with warning)."""
    mock_run.side_effect = FileNotFoundError("gh not found")

    result = check_coderabbit("jleechanorg", "test-repo", 42)

    # Fail-open
    assert result.passed is True
    assert "error" in result.reason.lower() or "warning" in result.reason.lower()


@patch("subprocess.run")
def test_invalid_json_response_fail_open(mock_run: MagicMock) -> None:
    """Invalid JSON response should fail-open."""
    mock_run.return_value = MagicMock(
        stdout="not valid json",
        returncode=0,
    )

    result = check_coderabbit("jleechanorg", "test-repo", 42)

    # Fail-open
    assert result.passed is True


# ---------------------------------------------------------------------------
# Test: GateResult dataclass structure
# ---------------------------------------------------------------------------


def test_gate_result_dataclass() -> None:
    """Verify GateResult has required fields."""
    result = GateResult(
        passed=True,
        reason="Test reason",
        reviewer_login="coderabbitai[bot]",
    )

    assert result.passed is True
    assert result.reason == "Test reason"
    assert result.reviewer_login == "coderabbitai[bot]"


def test_gate_result_optional_reviewer() -> None:
    """GateResult reviewer_login can be None."""
    result = GateResult(
        passed=True,
        reason="No CodeRabbit review",
        reviewer_login=None,
    )

    assert result.reviewer_login is None


# ---------------------------------------------------------------------------
# Test: Multiple CodeRabbit reviews - latest wins
# ---------------------------------------------------------------------------


@patch("subprocess.run")
def test_multiple_coderabbit_reviews_latest_wins(mock_run: MagicMock) -> None:
    """When multiple reviews exist, latest CodeRabbit review counts."""
    # Reviews are returned in order, last one should take precedence
    mock_run.return_value = MagicMock(
        stdout=make_gh_reviews_response([
            MockReview(author="coderabbitai[bot]", state="APPROVED"),
            MockReview(author="coderabbitai[bot]", state="CHANGES_REQUESTED"),
            MockReview(author="human-reviewer", state="APPROVED"),
        ]),
        returncode=0,
    )

    result = check_coderabbit("jleechanorg", "test-repo", 42)

    # CHANGES_REQUESTED is the latest from CodeRabbit
    assert result.passed is False


@patch("subprocess.run")
def test_coderabbit_dismissed_review_ignored(mock_run: MagicMock) -> None:
    """Dismissed reviews should be ignored."""
    mock_run.return_value = MagicMock(
        stdout=make_gh_reviews_response([
            MockReview(author="coderabbitai[bot]", state="DISMISSED"),
            MockReview(author="coderabbitai[bot]", state="APPROVED"),
        ]),
        returncode=0,
    )

    result = check_coderabbit("jleechanorg", "test-repo", 42)

    # APPROVED is the valid review
    assert result.passed is True
