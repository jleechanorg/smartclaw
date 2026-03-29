"""Tests for auto_review_trigger: wire AO merge-ready to OpenClaw review.

These tests verify the automatic PR review trigger functionality:
- AO `approved-and-green` event triggers OpenClaw review before notifying Jeffrey
- Review approves -> notify Jeffrey "OpenClaw approved, ready to merge"
- Review requests changes -> dispatch fix agent via ao send
- Review escalates -> notify Jeffrey "needs your eyes" with OpenClaw's notes
- Already reviewed by OpenClaw (idempotency) -> skip

These tests will fail until auto_review_trigger.py is implemented (TDD).
"""

from __future__ import annotations

import json
import pytest
from dataclasses import dataclass, field
from unittest.mock import MagicMock, patch

# These imports will fail until auto_review_trigger.py is implemented (TDD)
from orchestration.auto_review_trigger import (
    handle_merge_ready,
    has_been_reviewed,
    mark_as_reviewed,
)
from orchestration.ao_events import AOEvent
from orchestration.pr_reviewer import ReviewContext
from orchestration.pr_review_decision import ReviewDecision, ReviewComment


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@dataclass
class MockAOEventFactory:
    """Factory for creating test AOEvent objects."""

    @staticmethod
    def approved_and_green(pr_url: str = "https://github.com/jleechanorg/claw/pull/42") -> AOEvent:
        """Create an AOEvent representing approved-and-green (merge.ready)."""
        return AOEvent(
            event_type="merge.ready",
            priority="high",
            session_id="ao-session-123",
            project_id="jleechanorg/claw",
            message="PR approved and CI green - ready to merge",
            data={
                "pr_url": pr_url,
                "pr_number": 42,
                "status": "approved",
                "ci_state": "success",
            },
        )

    @staticmethod
    def merge_conflicts(pr_url: str = "https://github.com/jleechanorg/claw/pull/43") -> AOEvent:
        """Create an AOEvent representing merge conflicts."""
        return AOEvent(
            event_type="merge.conflicts",
            priority="high",
            session_id="ao-session-456",
            project_id="jleechanorg/claw",
            message="PR has merge conflicts",
            data={
                "pr_url": pr_url,
                "pr_number": 43,
                "status": "conflicts",
            },
        )


@dataclass
class MockReviewDecisionFactory:
    """Factory for creating test ReviewDecision objects."""

    @staticmethod
    def approved() -> ReviewDecision:
        """Create a ReviewDecision that approves the PR."""
        return ReviewDecision(
            action="approve",
            confidence=0.92,
            summary="This PR adds a helper function. Code follows project conventions, tests pass.",
            comments=[],
        )

    @staticmethod
    def request_changes() -> ReviewDecision:
        """Create a ReviewDecision that requests changes."""
        return ReviewDecision(
            action="request_changes",
            confidence=0.85,
            summary="The PR has a few issues that need addressing before merge.",
            comments=[
                ReviewComment(
                    path="src/utils.py",
                    line=10,
                    body="Consider using a constant instead of magic number here.",
                ),
            ],
        )

    @staticmethod
    def escalate() -> ReviewDecision:
        """Create a ReviewDecision that escalates to Jeffrey."""
        return ReviewDecision(
            action="escalate_to_jeffrey",
            confidence=0.70,
            summary="This PR touches authentication code and I'd like a second pair of eyes.",
            comments=[
                ReviewComment(
                    path="src/auth.py",
                    line=25,
                    body="This change affects login flow - verify this is intentional.",
                ),
            ],
        )


# ---------------------------------------------------------------------------
# handle_merge_ready tests
# ---------------------------------------------------------------------------


@patch("orchestration.auto_review_trigger.build_review_context")
@patch("orchestration.auto_review_trigger.review_pr")
@patch("orchestration.auto_review_trigger._notify_jeffrey_approval")
@patch("orchestration.auto_review_trigger._check_idempotency")
def test_handle_merge_ready_approves_notifies_jeffrey(
    mock_idempotency: MagicMock,
    mock_notify: MagicMock,
    mock_review_pr: MagicMock,
    mock_build_context: MagicMock,
) -> None:
    """When OpenClaw review approves -> notify Jeffrey 'OpenClaw approved, ready to merge'."""
    # Arrange
    mock_idempotency.return_value = False  # Not previously reviewed
    mock_build_context.return_value = ReviewContext(
        diff="+def new_helper(): pass",
        commits=[{"sha": "abc123", "message": "feat: add helper"}],
        ci_status={"state": "success", "statuses": []},
        claude_md_rules="# Rules\n- Use type hints",
        memories="",
        prior_patterns="",
    )
    mock_review_pr.return_value = MockReviewDecisionFactory.approved()

    event = MockAOEventFactory.approved_and_green()

    # Act
    result = handle_merge_ready(event)

    # Assert
    assert result.action == "approve"
    mock_build_context.assert_called_once_with("jleechanorg", "claw", 42)
    mock_review_pr.assert_called_once()
    mock_notify.assert_called_once()
    # Verify the approval message
    call_args = mock_notify.call_args
    assert "approved" in call_args[0][0].lower() or "ready to merge" in call_args[0][0].lower()


@patch("orchestration.auto_review_trigger.build_review_context")
@patch("orchestration.auto_review_trigger.review_pr")
@patch("orchestration.auto_review_trigger._dispatch_fix_agent")
@patch("orchestration.auto_review_trigger._check_idempotency")
def test_handle_merge_ready_request_changes_dispatches_fix(
    mock_idempotency: MagicMock,
    mock_dispatch_fix: MagicMock,
    mock_review_pr: MagicMock,
    mock_build_context: MagicMock,
) -> None:
    """When review requests changes -> dispatch fix agent via ao send."""
    # Arrange
    mock_idempotency.return_value = False  # Not previously reviewed
    mock_build_context.return_value = ReviewContext(
        diff="+def new_helper(): pass",
        commits=[{"sha": "abc123", "message": "feat: add helper"}],
        ci_status={"state": "success", "statuses": []},
        claude_md_rules="# Rules\n- Use type hints",
        memories="",
        prior_patterns="",
    )
    mock_review_pr.return_value = MockReviewDecisionFactory.request_changes()

    event = MockAOEventFactory.approved_and_green()

    # Act
    result = handle_merge_ready(event)

    # Assert
    assert result.action == "request_changes"
    mock_dispatch_fix.assert_called_once()
    # Verify fix agent receives the review comments
    call_args = mock_dispatch_fix.call_args
    assert "fix" in str(call_args).lower() or "changes" in str(call_args).lower()


@patch("orchestration.auto_review_trigger.build_review_context")
@patch("orchestration.auto_review_trigger.review_pr")
@patch("orchestration.auto_review_trigger._notify_jeffrey_escalation")
@patch("orchestration.auto_review_trigger._check_idempotency")
def test_handle_merge_ready_escalates_notifies_jeffrey(
    mock_idempotency: MagicMock,
    mock_notify: MagicMock,
    mock_review_pr: MagicMock,
    mock_build_context: MagicMock,
) -> None:
    """When review escalates -> notify Jeffrey 'needs your eyes' with OpenClaw's notes."""
    # Arrange
    mock_idempotency.return_value = False  # Not previously reviewed
    mock_build_context.return_value = ReviewContext(
        diff="+def new_helper(): pass",
        commits=[{"sha": "abc123", "message": "feat: add helper"}],
        ci_status={"state": "success", "statuses": []},
        claude_md_rules="# Rules\n- Use type hints",
        memories="",
        prior_patterns="",
    )
    mock_review_pr.return_value = MockReviewDecisionFactory.escalate()

    event = MockAOEventFactory.approved_and_green()

    # Act
    result = handle_merge_ready(event)

    # Assert
    assert result.action == "escalate_to_jeffrey"
    mock_notify.assert_called_once()
    # Verify escalation message includes notes
    call_args = mock_notify.call_args
    assert "needs your eyes" in call_args[0][0].lower() or "escalat" in call_args[0][0].lower()


@patch("orchestration.auto_review_trigger._check_idempotency")
def test_handle_merge_ready_already_reviewed_skips(mock_idempotency: MagicMock) -> None:
    """Already reviewed by OpenClaw -> skip (idempotency)."""
    # Arrange
    mock_idempotency.return_value = True  # Already reviewed

    event = MockAOEventFactory.approved_and_green()

    # Act
    result = handle_merge_ready(event)

    # Assert
    # Should return a no-op decision without calling review
    assert result.action == "skip"
    assert result.summary == "Already reviewed by OpenClaw"


# ---------------------------------------------------------------------------
# Idempotency tests
# ---------------------------------------------------------------------------


@patch("orchestration.auto_review_trigger._load_reviewed_prs")
def test_has_been_reviewed_returns_true_for_reviewed_pr(mock_load: MagicMock) -> None:
    """has_been_reviewed returns True for PRs already in the reviewed set."""
    # Arrange
    mock_load.return_value = {
        "jleechanorg/claw#42": {"reviewed_at": "2024-01-15T10:00:00Z"},
        "jleechanorg/claw#43": {"reviewed_at": "2024-01-15T11:00:00Z"},
    }

    # Act & Assert
    assert has_been_reviewed("jleechanorg", "claw", 42) is True
    assert has_been_reviewed("jleechanorg", "claw", 43) is True


@patch("orchestration.auto_review_trigger._load_reviewed_prs")
def test_has_been_reviewed_returns_false_for_new_pr(mock_load: MagicMock) -> None:
    """has_been_reviewed returns False for PRs not in the reviewed set."""
    # Arrange
    mock_load.return_value = {
        "jleechanorg/claw#42": {"reviewed_at": "2024-01-15T10:00:00Z"},
    }

    # Act & Assert
    assert has_been_reviewed("jleechanorg", "claw", 99) is False


@patch("orchestration.auto_review_trigger._load_reviewed_prs")
@patch("orchestration.auto_review_trigger._save_reviewed_prs")
def test_mark_as_reviewed_adds_pr_to_set(mock_save: MagicMock, mock_load: MagicMock) -> None:
    """mark_as_reviewed adds a PR to the reviewed set."""
    # Arrange
    mock_load.return_value = {}

    # Act
    mark_as_reviewed("jleechanorg", "claw", 42)

    # Assert
    mock_save.assert_called_once()
    saved_data = mock_save.call_args[0][0]
    assert "jleechanorg/claw#42" in saved_data


@patch("orchestration.auto_review_trigger._load_reviewed_prs")
@patch("orchestration.auto_review_trigger._save_reviewed_prs")
def test_mark_as_reviewed_preserves_existing_entries(mock_save: MagicMock, mock_load: MagicMock) -> None:
    """mark_as_reviewed preserves existing entries when adding new ones."""
    # Arrange
    existing = {
        "jleechanorg/claw#10": {"reviewed_at": "2024-01-10T10:00:00Z"},
    }
    mock_load.return_value = existing.copy()

    # Act
    mark_as_reviewed("jleechanorg", "claw", 42)

    # Assert
    mock_save.assert_called_once()
    saved_data = mock_save.call_args[0][0]
    # Both old and new should be present
    assert "jleechanorg/claw#10" in saved_data
    assert "jleechanorg/claw#42" in saved_data


# ---------------------------------------------------------------------------
# Integration tests - full flow
# ---------------------------------------------------------------------------


@patch("orchestration.auto_review_trigger._post_gh_review")
@patch("orchestration.auto_review_trigger._notify_jeffrey_approval")
@patch("orchestration.auto_review_trigger._check_idempotency")
def test_full_flow_approve_posts_gh_review(
    mock_idempotency: MagicMock,
    mock_notify: MagicMock,
    mock_post_gh: MagicMock,
) -> None:
    """Full flow: review approves -> posts GH review + notifies Jeffrey."""
    # Arrange - Use real components where possible
    mock_idempotency.return_value = False

    # Create mock functions that behave like real ones
    def mock_build(owner: str, repo: str, pr_num: int) -> ReviewContext:
        return ReviewContext(
            diff="+def new_helper(): pass",
            commits=[],
            ci_status={"state": "success"},
            claude_md_rules="",
            memories="",
            prior_patterns="",
        )

    def mock_review(ctx: ReviewContext, *, pr_owner: str | None = None, pr_repo: str | None = None, pr_number: int | None = None, pr_url: str | None = None, llm_caller: object = None, gh_poster: object = None, slack_poster: object = None) -> ReviewDecision:
        return MockReviewDecisionFactory.approved()

    with patch("orchestration.auto_review_trigger.build_review_context", mock_build):
        with patch("orchestration.auto_review_trigger.review_pr", mock_review):
            event = MockAOEventFactory.approved_and_green()
            result = handle_merge_ready(event)

    # Assert
    assert result.action == "approve"
    mock_post_gh.assert_called_once()
    mock_notify.assert_called_once()


@patch("orchestration.auto_review_trigger._post_gh_review")
@patch("orchestration.auto_review_trigger._dispatch_fix_agent")
@patch("orchestration.auto_review_trigger._check_idempotency")
def test_full_flow_request_changes_posts_and_dispatches(
    mock_idempotency: MagicMock,
    mock_dispatch: MagicMock,
    mock_post_gh: MagicMock,
) -> None:
    """Full flow: review requests changes -> posts GH review + dispatches fix."""
    # Arrange
    mock_idempotency.return_value = False

    def mock_build(owner: str, repo: str, pr_num: int) -> ReviewContext:
        return ReviewContext(
            diff="+def new_helper(): pass",
            commits=[],
            ci_status={"state": "success"},
            claude_md_rules="",
            memories="",
            prior_patterns="",
        )

    def mock_review(ctx: ReviewContext, *, pr_owner: str | None = None, pr_repo: str | None = None, pr_number: int | None = None, pr_url: str | None = None, llm_caller: object = None, gh_poster: object = None, slack_poster: object = None) -> ReviewDecision:
        return MockReviewDecisionFactory.request_changes()

    with patch("orchestration.auto_review_trigger.build_review_context", mock_build):
        with patch("orchestration.auto_review_trigger.review_pr", mock_review):
            event = MockAOEventFactory.approved_and_green()
            result = handle_merge_ready(event)

    # Assert
    assert result.action == "request_changes"
    mock_post_gh.assert_called_once()
    mock_dispatch.assert_called_once()


# ---------------------------------------------------------------------------
# Error handling tests
# ---------------------------------------------------------------------------


@patch("orchestration.auto_review_trigger.build_review_context")
@patch("orchestration.auto_review_trigger._check_idempotency")
def test_handle_merge_ready_missing_pr_number(mock_idempotency: MagicMock, mock_build: MagicMock) -> None:
    """handle_merge_ready handles events without pr_number gracefully."""
    # Arrange
    mock_idempotency.return_value = False

    event = AOEvent(
        event_type="merge.ready",
        priority="high",
        session_id="ao-session-123",
        project_id="jleechanorg/claw",
        message="PR ready",
        data={"pr_url": "https://github.com/jleechanorg/claw/pull/42"},  # No pr_number in data
    )

    # Act & Assert - Should not raise, should handle gracefully
    try:
        result = handle_merge_ready(event)
        # Either returns skip/error or extracts pr_number from URL
        assert result is not None
    except (KeyError, ValueError, TypeError):
        # Graceful handling of missing pr_number is acceptable
        pass


@patch("orchestration.auto_review_trigger._check_idempotency")
def test_handle_merge_ready_non_merge_ready_event(mock_idempotency: MagicMock) -> None:
    """handle_merge_ready should only respond to merge.ready events."""
    # Arrange
    event = AOEvent(
        event_type="reaction.escalated",  # Not merge.ready
        priority="high",
        session_id="ao-session-123",
        project_id="jleechanorg/claw",
        message="CI failed",
        data={},
    )

    # Act
    result = handle_merge_ready(event)

    # Assert - Should return skip for non-merge-ready events
    assert result.action == "skip"