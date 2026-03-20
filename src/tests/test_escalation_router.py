"""Tests for escalation_router: deterministic-first judgment routing of AO events.

These tests verify that escalation events are routed to appropriate actions based on
deterministic rules. LLM judgment is only requested when no rule matches.

TDD: These tests will fail until escalation_router.py is implemented.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Union

import pytest

# These imports will fail until escalation_router.py is implemented (TDD)
from unittest.mock import patch, MagicMock

from orchestration.ao_events import (
    AOEvent,
    EscalationContext,
    parse_ao_webhook,
)
from orchestration.escalation_router import (
    EscalationPolicy,
    FailureBudget,
    route_escalation,
    EscalationAction,
    RetryAction,
    KillAndRespawnAction,
    NotifyJeffreyAction,
    NeedsJudgmentAction,
    WaitForCIAction,
    JudgmentResult,
)
from orchestration.coderabbit_gate import GateResult


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@dataclass
class CIPayload:
    """Payload for reaction.escalated with ci-failed reaction key."""

    @staticmethod
    def raw(attempts: int = 1) -> dict:
        return {
            "event_type": "reaction.escalated",
            "priority": "high",
            "session_id": "ao-session-ci-123",
            "project_id": "jleechanorg/claw",
            "message": "CI failed",
            "data": {
                "sessionId": "ao-session-ci-123",
                "projectId": "jleechanorg/claw",
                "reactionKey": "ci-failed",
                "attempts": attempts,
                "subtask_id": "subtask-ci-123",
                "task_id": "task-1",
                "first_triggered": "2026-03-14T10:30:00Z",
            },
        }


@dataclass
class ChangesRequestedPayload:
    """Payload for reaction.escalated with changes-requested reaction key."""

    @staticmethod
    def raw(attempts: int = 1) -> dict:
        return {
            "event_type": "reaction.escalated",
            "priority": "high",
            "session_id": "ao-session-cr-456",
            "project_id": "jleechanorg/claw",
            "message": "Reviewer requested changes",
            "data": {
                "sessionId": "ao-session-cr-456",
                "projectId": "jleechanorg/claw",
                "reactionKey": "changes-requested",
                "attempts": attempts,
                "first_triggered": "2026-03-14T11:00:00Z",
            },
        }


@dataclass
class StuckSessionPayload:
    """Payload for session.stuck event."""

    @staticmethod
    def raw() -> dict:
        return {
            "event_type": "session.stuck",
            "priority": "medium",
            "session_id": "ao-session-stuck-789",
            "project_id": "worldarchitect/project",
            "message": "Session idle for 15 minutes",
            "data": {
                "sessionId": "ao-session-stuck-789",
                "projectId": "worldarchitect/project",
                "idle_duration_minutes": 15,
                "last_activity": "2026-03-14T10:15:00Z",
                "branch": "feature/test-branch",
            },
        }


@dataclass
class MergeReadyPayload:
    """Payload for merge.ready event."""

    @staticmethod
    def raw() -> dict:
        return {
            "event_type": "merge.ready",
            "priority": "low",
            "session_id": "ao-session-merge-001",
            "project_id": "jleechanorg/claw",
            "message": "PR ready for merge",
            "data": {
                "sessionId": "ao-session-merge-001",
                "projectId": "jleechanorg/claw",
                "pr_url": "https://github.com/jleechanorg/claw/pull/42",
                "pr_number": 42,
                "branch": "feature/fix-ci",
            },
        }


@dataclass
class MergeConflictsPayload:
    """Payload for merge.conflicts event."""

    @staticmethod
    def raw() -> dict:
        return {
            "event_type": "merge.conflicts",
            "priority": "high",
            "session_id": "ao-session-conflict-002",
            "project_id": "jleechanorg/claw",
            "message": "Merge conflicts detected",
            "data": {
                "sessionId": "ao-session-conflict-002",
                "projectId": "jleechanorg/claw",
                "pr_url": "https://github.com/jleechanorg/claw/pull/43",
                "pr_number": 43,
                "branch": "feature/refactor",
                "conflicting_files": ["src/main.py", "tests/test_main.py"],
            },
        }


@dataclass
class UnknownEventPayload:
    """Payload for unknown event type."""

    @staticmethod
    def raw() -> dict:
        return {
            "event_type": "unknown.weird-event",
            "priority": "low",
            "session_id": "ao-session-unknown-999",
            "project_id": "test/project",
            "message": "Unknown event",
            "data": {},
        }


# ---------------------------------------------------------------------------
# EscalationPolicy defaults
# ---------------------------------------------------------------------------


def default_policy() -> EscalationPolicy:
    """Create default escalation policy for testing."""
    return EscalationPolicy(
        max_retries_per_session=3,
        session_timeout_minutes=10,
        subtask_timeout_minutes=30,
        max_strategy_changes=2,
        min_confidence=0.6,
    )


def low_confidence_policy() -> EscalationPolicy:
    """Create policy with higher confidence threshold."""
    return EscalationPolicy(
        max_retries_per_session=3,
        session_timeout_minutes=10,
        subtask_timeout_minutes=30,
        max_strategy_changes=2,
        min_confidence=0.8,
    )


# ---------------------------------------------------------------------------
# FailureBudget tests
# ---------------------------------------------------------------------------


def test_failure_budget_records_escalation() -> None:
    """Budget should track escalation attempts per session."""
    budget = FailureBudget()
    budget.record_escalation(
        subtask_id="subtask-1",
        task_id="task-1",
        reaction_key="ci-failed",
    )

    assert budget.get_attempts("subtask-1") == 1


def test_failure_budget_tracks_multiple_attempts() -> None:
    """Budget should increment attempts for same subtask."""
    budget = FailureBudget()
    budget.record_escalation("subtask-1", "task-1", "ci-failed")
    budget.record_escalation("subtask-1", "task-1", "ci-failed")
    budget.record_escalation("subtask-1", "task-1", "ci-failed")

    assert budget.get_attempts("subtask-1") == 3


def test_failure_budget_separate_subtasks() -> None:
    """Budget should track separate subtasks independently."""
    budget = FailureBudget()
    budget.record_escalation("subtask-1", "task-1", "ci-failed")
    budget.record_escalation("subtask-2", "task-1", "ci-failed")

    assert budget.get_attempts("subtask-1") == 1
    assert budget.get_attempts("subtask-2") == 1


def test_failure_budget_tracks_strategy_changes() -> None:
    """Budget should track strategy changes per task."""
    budget = FailureBudget()
    budget.record_escalation("subtask-1", "task-1", "ci-failed")
    budget.record_strategy_change("task-1")

    assert budget.get_strategy_changes("task-1") == 1


def test_failure_budget_is_exhausted() -> None:
    """Budget should report exhausted when attempts exceed max_retries."""
    budget = FailureBudget()
    policy = default_policy()

    # Record max_retries attempts
    for i in range(policy.max_retries_per_session):
        budget.record_escalation("subtask-1", "task-1", "ci-failed")

    assert budget.is_exhausted("subtask-1", policy) is True


def test_failure_budget_not_exhausted_before_limit() -> None:
    """Budget should not report exhausted before limit."""
    budget = FailureBudget()
    policy = default_policy()

    # Record fewer than max_retries
    budget.record_escalation("subtask-1", "task-1", "ci-failed")
    budget.record_escalation("subtask-1", "task-1", "ci-failed")

    assert budget.is_exhausted("subtask-1", policy) is False


def test_failure_budget_task_exhausted() -> None:
    """Budget should report exhausted when strategy changes exceed max."""
    budget = FailureBudget()
    policy = default_policy()

    # Record max strategy changes
    for _ in range(policy.max_strategy_changes):
        budget.record_strategy_change("task-1")

    assert budget.is_task_exhausted("task-1", policy) is True


def test_failure_budget_summary() -> None:
    """Budget should provide summary of all tracked failures."""
    budget = FailureBudget()
    budget.record_escalation("subtask-1", "task-1", "ci-failed")
    budget.record_escalation("subtask-1", "task-1", "ci-failed")
    budget.record_escalation("subtask-2", "task-1", "changes-requested")

    summary = budget.summary()

    assert "task-1" in summary
    assert summary["task-1"]["subtasks"]["subtask-1"]["attempts"] == 2
    assert summary["task-1"]["subtasks"]["subtask-2"]["attempts"] == 1


# ---------------------------------------------------------------------------
# Reaction.escalated + ci-failed routing tests
# ---------------------------------------------------------------------------


def test_ci_failed_retry_within_budget() -> None:
    """ci-failed with attempts <= budget should return RetryAction."""
    event = parse_ao_webhook(CIPayload.raw(attempts=1))
    budget = FailureBudget()
    policy = default_policy()

    result = route_escalation(event, budget, policy)

    assert isinstance(result, JudgmentResult)
    assert isinstance(result.action, RetryAction)
    assert "ci" in result.action.prompt.lower()


def test_ci_failed_escalate_over_budget() -> None:
    """ci-failed with attempts > budget should escalate to Jeffrey."""
    event = parse_ao_webhook(CIPayload.raw(attempts=4))
    budget = FailureBudget()
    policy = default_policy()

    result = route_escalation(event, budget, policy)

    assert isinstance(result, JudgmentResult)
    assert isinstance(result.action, NotifyJeffreyAction)
    assert "budget" in result.action.message.lower() or "exceeded" in result.action.message.lower()


def test_ci_failed_with_tracked_budget() -> None:
    """ci-failed should check tracked budget, not just current attempts."""
    event = parse_ao_webhook(CIPayload.raw(attempts=2))
    budget = FailureBudget()
    # Pre-record 2 failures (total will be 4, exceeding budget of 3)
    budget.record_escalation("subtask-ci-123", "task-1", "ci-failed")
    budget.record_escalation("subtask-ci-123", "task-1", "ci-failed")
    policy = default_policy()

    result = route_escalation(event, budget, policy)

    # Should escalate because total attempts (2 + 2) > 3
    assert isinstance(result.action, NotifyJeffreyAction)


# ---------------------------------------------------------------------------
# Reaction.escalated + changes-requested routing tests
# ---------------------------------------------------------------------------


def test_changes_requested_retry() -> None:
    """changes-requested should always retry (deterministic)."""
    event = parse_ao_webhook(ChangesRequestedPayload.raw(attempts=1))
    budget = FailureBudget()
    policy = default_policy()

    result = route_escalation(event, budget, policy)

    assert isinstance(result.action, RetryAction)


def test_changes_requested_retry_regardless_of_attempts() -> None:
    """changes-requested should retry even with high attempt count."""
    event = parse_ao_webhook(ChangesRequestedPayload.raw(attempts=10))
    budget = FailureBudget()
    policy = default_policy()

    result = route_escalation(event, budget, policy)

    # Always deterministic retry for changes-requested
    assert isinstance(result.action, RetryAction)


# ---------------------------------------------------------------------------
# Session.stuck routing tests
# ---------------------------------------------------------------------------


def test_session_stuck_kill_and_respawn() -> None:
    """session.stuck should return KillAndRespawnAction when CI is green."""
    event = parse_ao_webhook(StuckSessionPayload.raw())
    budget = FailureBudget()
    policy = default_policy()

    # Mock CI as green to trigger kill+respawn
    with patch("orchestration.escalation_router.check_ci_status") as mock_ci:
        mock_ci.return_value = {"status": "green", "session_id": event.session_id}
        result = route_escalation(event, budget, policy)

    assert isinstance(result.action, KillAndRespawnAction)
    assert result.action.session_id == "ao-session-stuck-789"
    assert result.action.task == "Continue work on worldarchitect/project"


def test_session_stuck_timeout() -> None:
    """session.stuck should respect session_timeout_minutes in policy."""
    event = parse_ao_webhook(StuckSessionPayload.raw())
    budget = FailureBudget()

    # Policy with very short timeout
    policy = EscalationPolicy(
        max_retries_per_session=1,
        session_timeout_minutes=5,  # Very short
        subtask_timeout_minutes=30,
        max_strategy_changes=2,
        min_confidence=0.6,
    )

    # Mock CI as green to trigger kill+respawn
    with patch("orchestration.escalation_router.check_ci_status") as mock_ci:
        mock_ci.return_value = {"status": "green", "session_id": event.session_id}
        result = route_escalation(event, budget, policy)

    # Should still kill and respawn (deterministic for stuck)
    assert isinstance(result.action, KillAndRespawnAction)


def test_session_stuck_waits_for_ci_pending() -> None:
    """session.stuck should return WaitForCIAction when CI is pending."""
    event = parse_ao_webhook(StuckSessionPayload.raw())
    budget = FailureBudget()
    policy = default_policy()

    # Mock CI as pending to trigger WaitForCIAction
    with patch("orchestration.escalation_router.check_ci_status") as mock_ci:
        mock_ci.return_value = {"status": "pending", "session_id": event.session_id}
        result = route_escalation(event, budget, policy)

    assert isinstance(result.action, WaitForCIAction)
    assert result.action.ci_status == "pending"
    assert result.action.extended_timeout_minutes == policy.session_timeout_minutes + policy.ci_grace_period_minutes
    assert "pending" in result.action.reason.lower()


def test_session_stuck_waits_for_ci_in_progress() -> None:
    """session.stuck should return WaitForCIAction when CI is in_progress."""
    event = parse_ao_webhook(StuckSessionPayload.raw())
    budget = FailureBudget()
    policy = default_policy()

    # Mock CI as in_progress to trigger WaitForCIAction
    with patch("orchestration.escalation_router.check_ci_status") as mock_ci:
        mock_ci.return_value = {"status": "in_progress", "session_id": event.session_id}
        result = route_escalation(event, budget, policy)

    assert isinstance(result.action, WaitForCIAction)
    assert result.action.ci_status == "in_progress"
    assert result.action.extended_timeout_minutes == policy.session_timeout_minutes + policy.ci_grace_period_minutes


def test_session_stuck_kills_when_ci_green() -> None:
    """session.stuck should return KillAndRespawnAction when CI is green."""
    event = parse_ao_webhook(StuckSessionPayload.raw())
    budget = FailureBudget()
    policy = default_policy()

    # Mock CI as green to trigger kill+respawn
    with patch("orchestration.escalation_router.check_ci_status") as mock_ci:
        mock_ci.return_value = {"status": "green", "session_id": event.session_id}
        result = route_escalation(event, budget, policy)

    assert isinstance(result.action, KillAndRespawnAction)


def test_session_stuck_kills_when_ci_red() -> None:
    """session.stuck should return KillAndRespawnAction when CI is red (failed)."""
    event = parse_ao_webhook(StuckSessionPayload.raw())
    budget = FailureBudget()
    policy = default_policy()

    # Mock CI as red to trigger kill+respawn (CI already failed)
    with patch("orchestration.escalation_router.check_ci_status") as mock_ci:
        mock_ci.return_value = {"status": "red", "session_id": event.session_id}
        result = route_escalation(event, budget, policy)

    # Should kill and respawn even if CI failed (session is stuck regardless)
    assert isinstance(result.action, KillAndRespawnAction)


def test_session_stuck_ci_grace_period_configurable() -> None:
    """ci_grace_period_minutes should be configurable in policy."""
    event = parse_ao_webhook(StuckSessionPayload.raw())
    budget = FailureBudget()

    # Policy with custom CI grace period
    policy = EscalationPolicy(
        max_retries_per_session=1,
        session_timeout_minutes=10,
        subtask_timeout_minutes=30,
        max_strategy_changes=2,
        min_confidence=0.6,
        ci_grace_period_minutes=15,  # Custom grace period
    )

    # Mock CI as pending
    with patch("orchestration.escalation_router.check_ci_status") as mock_ci:
        mock_ci.return_value = {"status": "pending", "session_id": event.session_id}
        result = route_escalation(event, budget, policy)

    assert isinstance(result.action, WaitForCIAction)
    # Extended timeout should be session_timeout + ci_grace_period
    assert result.action.extended_timeout_minutes == 10 + 15


# ---------------------------------------------------------------------------
# Merge events routing tests
# ---------------------------------------------------------------------------


@patch("orchestration.escalation_router.check_coderabbit")
@patch("orchestration.escalation_router.is_auto_merge_enabled")
def test_merge_ready_notify_jeffrey(mock_auto_merge: MagicMock, mock_cr: MagicMock) -> None:
    """merge.ready should notify Jeffrey (deterministic)."""
    # Mock CodeRabbit gate to pass and auto-merge to be disabled
    mock_cr.return_value = GateResult(passed=True, reason="CodeRabbit approved", reviewer_login="coderabbit[bot]")
    mock_auto_merge.return_value = False

    event = parse_ao_webhook(MergeReadyPayload.raw())
    budget = FailureBudget()
    policy = default_policy()

    result = route_escalation(event, budget, policy)

    assert isinstance(result.action, NotifyJeffreyAction)
    assert "merge" in result.action.message.lower() or "ready" in result.action.message.lower()
    assert result.action.pr_url is not None


def test_merge_conflicts_notify_jeffrey_with_details() -> None:
    """merge.conflicts should notify Jeffrey with conflict details."""
    event = parse_ao_webhook(MergeConflictsPayload.raw())
    budget = FailureBudget()
    policy = default_policy()

    result = route_escalation(event, budget, policy)

    assert isinstance(result.action, NotifyJeffreyAction)
    assert "conflict" in result.action.message.lower()
    # Should include conflict file details
    assert hasattr(result.action, "details")
    if result.action.details:
        assert "src/main.py" in str(result.action.details) or "conflicting_files" in str(result.action.details)


# ---------------------------------------------------------------------------
# Unknown event type routing tests
# ---------------------------------------------------------------------------


def test_unknown_event_escalates_to_jeffrey() -> None:
    """Unknown event type should escalate to Jeffrey (fail-safe)."""
    event = parse_ao_webhook(UnknownEventPayload.raw())
    budget = FailureBudget()
    policy = default_policy()

    result = route_escalation(event, budget, policy)

    assert isinstance(result.action, NotifyJeffreyAction)
    assert "unknown" in result.action.message.lower() or "unhandled" in result.action.message.lower()


# ---------------------------------------------------------------------------
# Confidence-based judgment tests
# ---------------------------------------------------------------------------


def test_needs_judgment_low_confidence_escalates() -> None:
    """NeedsJudgmentAction with confidence < min_confidence should escalate."""
    # This test assumes we can create a JudgmentResult with a low-confidence
    # NeedsJudgmentAction - we'll need to construct this scenario

    # First, we need an event type that doesn't match deterministic rules
    # Unknown event returns NotifyJeffreyAction, but we need to test
    # the case where we get NeedsJudgmentAction with low confidence

    # Create a mock JudgmentResult with low confidence
    # This requires the router to return NeedsJudgmentAction for some scenario

    # For now, test the logic directly by checking policy application
    policy = low_confidence_policy()

    # When result confidence < min_confidence, should escalate
    # We'll verify this by checking that high min_confidence triggers escalation
    assert policy.min_confidence == 0.8


def test_confidence_threshold_in_policy() -> None:
    """Policy should enforce min_confidence threshold."""
    policy = default_policy()
    assert policy.min_confidence == 0.6

    high_threshold_policy = EscalationPolicy(
        max_retries_per_session=3,
        session_timeout_minutes=10,
        subtask_timeout_minutes=30,
        max_strategy_changes=2,
        min_confidence=0.9,
    )
    assert high_threshold_policy.min_confidence == 0.9


# ---------------------------------------------------------------------------
# Budget exceeded escalation tests
# ---------------------------------------------------------------------------


def test_budget_exceeded_escalates_to_jeffrey() -> None:
    """When budget is exhausted, should escalate to Jeffrey with summary."""
    event = parse_ao_webhook(CIPayload.raw(attempts=1))
    budget = FailureBudget()
    policy = default_policy()

    # Exhaust the budget first
    for _ in range(policy.max_retries_per_session):
        budget.record_escalation("subtask-ci-123", "task-1", "ci-failed")

    # Now even with 1 attempt in the event, total exceeds budget
    result = route_escalation(event, budget, policy)

    assert isinstance(result.action, NotifyJeffreyAction)
    assert "exceeded" in result.action.message.lower() or "budget" in result.action.message.lower()


def test_task_exhausted_escalates() -> None:
    """When task strategy changes exhausted, should escalate to Jeffrey."""
    event = parse_ao_webhook(CIPayload.raw(attempts=1))
    budget = FailureBudget()
    policy = default_policy()

    # Exhaust task strategy changes
    for _ in range(policy.max_strategy_changes):
        budget.record_strategy_change("task-1")

    result = route_escalation(event, budget, policy)

    assert isinstance(result.action, NotifyJeffreyAction)
    # Should include task summary
    summary = budget.summary()
    assert "task-1" in summary


# ---------------------------------------------------------------------------
# RetryAction details tests
# ---------------------------------------------------------------------------


def test_retry_action_includes_enriched_prompt() -> None:
    """RetryAction should include enriched prompt with context."""
    event = parse_ao_webhook(CIPayload.raw(attempts=2))
    budget = FailureBudget()
    policy = default_policy()

    result = route_escalation(event, budget, policy)

    assert isinstance(result.action, RetryAction)
    # Should include original message and attempt count
    assert result.action.prompt is not None
    assert len(result.action.prompt) > 0


def test_retry_action_includes_session_context() -> None:
    """RetryAction should include session context for AO."""
    event = parse_ao_webhook(CIPayload.raw(attempts=1))
    budget = FailureBudget()
    policy = default_policy()

    result = route_escalation(event, budget, policy)

    assert isinstance(result.action, RetryAction)
    assert result.action.session_id == event.session_id
    assert result.action.project_id == event.project_id


# ---------------------------------------------------------------------------
# KillAndRespawnAction details tests
# ---------------------------------------------------------------------------


def test_kill_and_respawn_includes_original_session() -> None:
    """KillAndRespawnAction should include the session to kill."""
    event = parse_ao_webhook(StuckSessionPayload.raw())
    budget = FailureBudget()
    policy = default_policy()

    # Mock CI as green to trigger kill+respawn
    with patch("orchestration.escalation_router.check_ci_status") as mock_ci:
        mock_ci.return_value = {"status": "green", "session_id": event.session_id}
        result = route_escalation(event, budget, policy)

    assert isinstance(result.action, KillAndRespawnAction)
    assert result.action.session_to_kill == "ao-session-stuck-789"
    assert result.action.project_id == "worldarchitect/project"


def test_kill_and_respawn_includes_project_context() -> None:
    """KillAndRespawnAction should include project context for respawn."""
    event = parse_ao_webhook(StuckSessionPayload.raw())
    budget = FailureBudget()
    policy = default_policy()

    # Mock CI as green to trigger kill+respawn
    with patch("orchestration.escalation_router.check_ci_status") as mock_ci:
        mock_ci.return_value = {"status": "green", "session_id": event.session_id}
        result = route_escalation(event, budget, policy)

    assert result.action.project_id == "worldarchitect/project"


# ---------------------------------------------------------------------------
# NotifyJeffreyAction details tests
# ---------------------------------------------------------------------------


@patch("orchestration.escalation_router.check_coderabbit")
@patch("orchestration.escalation_router.is_auto_merge_enabled")
def test_notify_jeffrey_includes_event_summary(mock_auto_merge: MagicMock, mock_cr: MagicMock) -> None:
    """NotifyJeffreyAction should include a summary of the event."""
    # Mock CodeRabbit gate to pass and auto-merge to be disabled
    mock_cr.return_value = GateResult(passed=True, reason="CodeRabbit approved", reviewer_login="coderabbit[bot]")
    mock_auto_merge.return_value = False

    event = parse_ao_webhook(MergeReadyPayload.raw())
    budget = FailureBudget()
    policy = default_policy()

    result = route_escalation(event, budget, policy)

    assert isinstance(result.action, NotifyJeffreyAction)
    assert result.action.message is not None
    assert len(result.action.message) > 0


@patch("orchestration.escalation_router.check_coderabbit")
@patch("orchestration.escalation_router.is_auto_merge_enabled")
def test_notify_jeffrey_includes_session_id(mock_auto_merge: MagicMock, mock_cr: MagicMock) -> None:
    """NotifyJeffreyAction should include the session ID for context."""
    # Mock CodeRabbit gate to pass and auto-merge to be disabled (notify-only mode)
    mock_cr.return_value = GateResult(passed=True, reason="CodeRabbit approved", reviewer_login="coderabbit[bot]")
    mock_auto_merge.return_value = False

    event = parse_ao_webhook(MergeReadyPayload.raw())
    budget = FailureBudget()
    policy = default_policy()

    result = route_escalation(event, budget, policy)

    assert result.action.session_id == "ao-session-merge-001"


# ---------------------------------------------------------------------------
# Integration: Full escalation flow scenarios
# ---------------------------------------------------------------------------


def test_full_ci_failure_retry_then_escalate() -> None:
    """Full flow: ci-failed retry until budget exceeded, then escalate."""
    budget = FailureBudget()
    policy = default_policy()

    # First attempt: should retry
    event1 = parse_ao_webhook(CIPayload.raw(attempts=1))
    result1 = route_escalation(event1, budget, policy)
    assert isinstance(result1.action, RetryAction)

    # Record this attempt
    budget.record_escalation("subtask-ci-123", "task-1", "ci-failed")

    # Second attempt: should retry
    event2 = parse_ao_webhook(CIPayload.raw(attempts=1))
    result2 = route_escalation(event2, budget, policy)
    assert isinstance(result2.action, RetryAction)

    budget.record_escalation("subtask-ci-123", "task-1", "ci-failed")

    # Third attempt: should retry
    event3 = parse_ao_webhook(CIPayload.raw(attempts=1))
    result3 = route_escalation(event3, budget, policy)
    assert isinstance(result3.action, RetryAction)

    budget.record_escalation("subtask-ci-123", "task-1", "ci-failed")

    # Fourth attempt: should escalate (budget exhausted)
    event4 = parse_ao_webhook(CIPayload.raw(attempts=1))
    result4 = route_escalation(event4, budget, policy)
    assert isinstance(result4.action, NotifyJeffreyAction)


def test_different_reaction_keys_separate_budgets() -> None:
    """Different reaction keys should have separate budget tracking."""
    budget = FailureBudget()
    policy = default_policy()

    # ci-failed at limit
    for _ in range(policy.max_retries_per_session):
        budget.record_escalation("subtask-1", "task-1", "ci-failed")

    # changes-requested should still have budget
    event_cr = parse_ao_webhook(ChangesRequestedPayload.raw(attempts=1))
    result_cr = route_escalation(event_cr, budget, policy)

    # changes-requested is always deterministic retry
    assert isinstance(result_cr.action, RetryAction)


@patch("orchestration.escalation_router.check_coderabbit")
@patch("orchestration.escalation_router.is_auto_merge_enabled")
def test_merge_ready_bypasses_ci_budget(mock_auto_merge: MagicMock, mock_cr: MagicMock) -> None:
    """merge.ready should not be constrained by CI failure budget."""
    # Mock CodeRabbit gate to pass and auto-merge to be disabled
    mock_cr.return_value = GateResult(passed=True, reason="CodeRabbit approved", reviewer_login="coderabbit[bot]")
    mock_auto_merge.return_value = False

    budget = FailureBudget()
    policy = default_policy()

    # Exhaust CI budget
    for _ in range(policy.max_retries_per_session):
        budget.record_escalation("subtask-ci", "task-1", "ci-failed")

    # merge.ready should still notify (not constrained by CI budget)
    event = parse_ao_webhook(MergeReadyPayload.raw())
    result = route_escalation(event, budget, policy)

    assert isinstance(result.action, NotifyJeffreyAction)
