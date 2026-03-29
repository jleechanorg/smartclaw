"""Tests for escalation_handler: end-to-end escalation flow integration.

These tests verify the full escalation flow from AO webhook to action execution:
- AO webhook → parse → route → execute → verify AO command issued
- CI escalation → retry → retry → budget exceeded → Jeffrey notified
- Stuck session → kill + respawn → verify new session created
- Merge ready + CodeRabbit approved → Jeffrey notified "ready to merge"

TDD: These tests will fail until escalation_handler.py is implemented.
"""

from __future__ import annotations

import json
import pytest
from dataclasses import dataclass, field
from pathlib import Path

# These imports will fail until escalation_handler.py is implemented (TDD)
from orchestration.escalation_handler import (
    handle_escalation,
    load_escalation_policy,
    EscalationHandlerError,
)
import orchestration.escalation_handler as _esc_handler
from orchestration.escalation_router import EscalationPolicy


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_module_budget() -> None:
    """Reset module-level _budget between tests to prevent state pollution."""
    _esc_handler._budget = None
    yield
    _esc_handler._budget = None


@dataclass
class MockAOCli:
    calls: list = field(default_factory=list)
    should_fail_send: bool = False
    spawn_return_session: str = "new-session-456"

    def send(self, session_id: str, message: str) -> None:
        self.calls.append(("send", session_id, message))
        if self.should_fail_send:
            from orchestration.ao_cli import AOCommandError
            raise AOCommandError("Send failed", "mock error", 1)

    def kill(self, session_id: str) -> None:
        self.calls.append(("kill", session_id))

    def spawn(self, project: str, issue: str, *, branch: str | None = None) -> str:
        self.calls.append(("spawn", project, issue, branch))
        return self.spawn_return_session


@dataclass
class MockSlackNotifier:
    messages: list = field(default_factory=list)

    def send_dm(self, message: str, channel: str | None = None) -> bool:
        self.messages.append({"message": message, "channel": channel})
        return True


# ---------------------------------------------------------------------------
# Payload factories
# ---------------------------------------------------------------------------


def ci_failure_payload(attempts: int = 1) -> dict:
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


def stuck_session_payload() -> dict:
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
        },
    }


def merge_ready_payload(pr_number: int = 42) -> dict:
    return {
        "event_type": "merge.ready",
        "priority": "low",
        "session_id": "ao-session-merge-001",
        "project_id": "jleechanorg/claw",
        "message": "PR ready for merge",
        "data": {
            "sessionId": "ao-session-merge-001",
            "projectId": "jleechanorg/claw",
            "pr_url": f"https://github.com/jleechanorg/claw/pull/{pr_number}",
            "pr_number": pr_number,
            "branch": "feature/fix-ci",
        },
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_ao_cli():
    return MockAOCli()


@pytest.fixture
def mock_slack_notifier():
    return MockSlackNotifier()


@pytest.fixture
def temp_action_log(tmp_path):
    return str(tmp_path / "action_log.jsonl")


@pytest.fixture
def default_policy() -> EscalationPolicy:
    return EscalationPolicy(
        max_retries_per_session=3,
        session_timeout_minutes=10,
        subtask_timeout_minutes=30,
        max_strategy_changes=2,
        min_confidence=0.6,
    )


# ---------------------------------------------------------------------------
# Main tests
# ---------------------------------------------------------------------------


def test_webhook_to_ao_command_issued(
    mock_ao_cli, mock_slack_notifier, temp_action_log, default_policy
):
    """AO webhook → parse → route → execute → verify AO command issued."""
    payload = ci_failure_payload(attempts=1)

    result = handle_escalation(
        raw_payload=payload,
        cli=mock_ao_cli,
        notifier=mock_slack_notifier,
        action_log_path=temp_action_log,
        policy=default_policy,
    )

    assert len(mock_ao_cli.calls) >= 1
    assert mock_ao_cli.calls[0][0] == "send"
    assert result.success is True
    assert result.action_type == "RetryAction"


def test_ci_retry_until_budget_exceeded_notifies_jeffrey(
    mock_ao_cli, mock_slack_notifier, temp_action_log, default_policy
):
    """CI escalation → retry → retry → budget exceeded → Jeffrey notified."""
    # First: retry
    result1 = handle_escalation(
        raw_payload=ci_failure_payload(attempts=1),
        cli=mock_ao_cli,
        notifier=mock_slack_notifier,
        action_log_path=temp_action_log,
        policy=default_policy,
    )
    assert result1.action_type == "RetryAction"

    # Second: retry
    result2 = handle_escalation(
        raw_payload=ci_failure_payload(attempts=2),
        cli=mock_ao_cli,
        notifier=mock_slack_notifier,
        action_log_path=temp_action_log,
        policy=default_policy,
    )
    assert result2.action_type == "RetryAction"

    # Third: retry
    result3 = handle_escalation(
        raw_payload=ci_failure_payload(attempts=3),
        cli=mock_ao_cli,
        notifier=mock_slack_notifier,
        action_log_path=temp_action_log,
        policy=default_policy,
    )
    assert result3.action_type == "RetryAction"

    # Fourth: budget exceeded - notify Jeffrey
    result4 = handle_escalation(
        raw_payload=ci_failure_payload(attempts=4),
        cli=mock_ao_cli,
        notifier=mock_slack_notifier,
        action_log_path=temp_action_log,
        policy=default_policy,
    )
    assert result4.action_type == "NotifyJeffreyAction"
    assert len(mock_slack_notifier.messages) >= 1
    notify_msg = mock_slack_notifier.messages[-1]["message"]
    assert "ci" in notify_msg.lower() or "budget" in notify_msg.lower()


def test_stuck_session_kill_and_respawn_creates_new_session(
    mock_ao_cli, mock_slack_notifier, temp_action_log, default_policy
):
    """Stuck session → kill + respawn → verify new session created."""
    payload = stuck_session_payload()

    result = handle_escalation(
        raw_payload=payload,
        cli=mock_ao_cli,
        notifier=mock_slack_notifier,
        action_log_path=temp_action_log,
        policy=default_policy,
    )

    assert len(mock_ao_cli.calls) == 2
    assert mock_ao_cli.calls[0] == ("kill", "ao-session-stuck-789")
    assert mock_ao_cli.calls[1][0] == "spawn"
    assert mock_ao_cli.calls[1][1] == "worldarchitect/project"
    assert mock_ao_cli.calls[1][2] == "Continue work on worldarchitect/project"

    assert result.success is True
    assert result.action_type == "KillAndRespawnAction"
    assert result.details.get("new_session_id") is not None


def test_merge_ready_notifies_jeffrey_ready_to_merge(
    mock_ao_cli, mock_slack_notifier, temp_action_log, default_policy
):
    """Merge ready + CodeRabbit approved → Jeffrey notified 'ready to merge'."""
    payload = merge_ready_payload(pr_number=42)

    result = handle_escalation(
        raw_payload=payload,
        cli=mock_ao_cli,
        notifier=mock_slack_notifier,
        action_log_path=temp_action_log,
        policy=default_policy,
    )

    assert len(mock_slack_notifier.messages) >= 1
    notify_msg = mock_slack_notifier.messages[0]["message"]
    assert "ready" in notify_msg.lower() or "merge" in notify_msg.lower()
    assert "42" in notify_msg or "pull/42" in notify_msg
    assert len(mock_ao_cli.calls) == 0


def test_invalid_payload_raises_error(
    mock_ao_cli, mock_slack_notifier, temp_action_log, default_policy
):
    """Invalid webhook payload should raise EscalationHandlerError."""
    with pytest.raises(EscalationHandlerError):
        handle_escalation(
            raw_payload={"event_type": "reaction.escalated"},
            cli=mock_ao_cli,
            notifier=mock_slack_notifier,
            action_log_path=temp_action_log,
            policy=default_policy,
        )


def test_empty_payload_raises_error(
    mock_ao_cli, mock_slack_notifier, temp_action_log, default_policy
):
    """Empty payload should raise EscalationHandlerError."""
    with pytest.raises(EscalationHandlerError):
        handle_escalation(
            raw_payload={},
            cli=mock_ao_cli,
            notifier=mock_slack_notifier,
            action_log_path=temp_action_log,
            policy=default_policy,
        )


def test_ao_send_failure_triggers_fallback(
    mock_ao_cli, mock_slack_notifier, temp_action_log, default_policy
):
    """When ao_send fails, should fallback to notify Jeffrey."""
    mock_ao_cli.should_fail_send = True

    result = handle_escalation(
        raw_payload=ci_failure_payload(attempts=1),
        cli=mock_ao_cli,
        notifier=mock_slack_notifier,
        action_log_path=temp_action_log,
        policy=default_policy,
    )

    assert len(mock_ao_cli.calls) >= 1
    assert len(mock_slack_notifier.messages) >= 1


def test_actions_logged_to_jsonl(
    mock_ao_cli, mock_slack_notifier, temp_action_log, default_policy
):
    """All actions should be logged to the action log file."""
    handle_escalation(
        raw_payload=ci_failure_payload(attempts=1),
        cli=mock_ao_cli,
        notifier=mock_slack_notifier,
        action_log_path=temp_action_log,
        policy=default_policy,
    )

    log_path = Path(temp_action_log)
    assert log_path.exists()

    entries = []
    with open(log_path) as f:
        for line in f:
            if line.strip():
                entries.append(json.loads(line))

    assert len(entries) >= 1
    assert entries[0]["action_type"] == "RetryAction"


def test_log_entry_includes_event_details(
    mock_ao_cli, mock_slack_notifier, temp_action_log, default_policy
):
    """Log entries should include event details."""
    handle_escalation(
        raw_payload=merge_ready_payload(pr_number=99),
        cli=mock_ao_cli,
        notifier=mock_slack_notifier,
        action_log_path=temp_action_log,
        policy=default_policy,
    )

    log_path = Path(temp_action_log)
    with open(log_path) as f:
        entry = json.loads(f.readline())

    assert "timestamp" in entry
    assert entry["action_type"] == "NotifyJeffreyAction"
    assert entry["session_id"] == "ao-session-merge-001"


def test_budget_persisted_to_disk_after_escalation(
    mock_ao_cli, mock_slack_notifier, temp_action_log, default_policy, tmp_path
):
    """After handle_escalation with a budget_path, the attempt must survive a process restart."""
    from orchestration.failure_budget import FailureBudget as PersistentFailureBudget

    budget_path = tmp_path / "failure_budget.json"

    handle_escalation(
        raw_payload=ci_failure_payload(attempts=1),
        cli=mock_ao_cli,
        notifier=mock_slack_notifier,
        action_log_path=temp_action_log,
        policy=default_policy,
        budget_path=budget_path,
    )

    # Simulate process restart: load a fresh budget from disk
    fresh_budget = PersistentFailureBudget(budget_path=budget_path)
    assert fresh_budget.get_attempts("subtask-ci-123") >= 1, (
        "Escalation attempt must be persisted to disk (not just in-memory)"
    )


def test_load_default_policy(tmp_path):
    """Should return default policy when no custom config exists."""
    policy = load_escalation_policy(state_dir=str(tmp_path / "nonexistent"))

    assert policy.max_retries_per_session == 3
    assert policy.session_timeout_minutes == 10
    assert policy.subtask_timeout_minutes == 30


def test_load_custom_policy(tmp_path):
    """Should load custom policy from state directory."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    policy_file = state_dir / "escalation_policy.json"

    custom_policy = {"max_retries_per_session": 5, "session_timeout_minutes": 15}
    policy_file.write_text(json.dumps(custom_policy))

    policy = load_escalation_policy(state_dir=str(state_dir))

    assert policy.max_retries_per_session == 5
    assert policy.session_timeout_minutes == 15


def test_multiple_different_escalations(
    mock_ao_cli, mock_slack_notifier, temp_action_log, default_policy
):
    """Multiple different escalations should be handled independently."""
    handle_escalation(
        raw_payload=ci_failure_payload(attempts=1),
        cli=mock_ao_cli,
        notifier=mock_slack_notifier,
        action_log_path=temp_action_log,
        policy=default_policy,
    )

    handle_escalation(
        raw_payload=merge_ready_payload(pr_number=55),
        cli=mock_ao_cli,
        notifier=mock_slack_notifier,
        action_log_path=temp_action_log,
        policy=default_policy,
    )

    handle_escalation(
        raw_payload=stuck_session_payload(),
        cli=mock_ao_cli,
        notifier=mock_slack_notifier,
        action_log_path=temp_action_log,
        policy=default_policy,
    )

    assert len(mock_ao_cli.calls) == 3
    assert len(mock_slack_notifier.messages) == 1


def test_strict_policy_escalates_earlier(
    mock_ao_cli, mock_slack_notifier, temp_action_log
):
    """Stricter policy should escalate earlier."""
    strict_policy = EscalationPolicy(
        max_retries_per_session=1,
        session_timeout_minutes=5,
        subtask_timeout_minutes=15,
        max_strategy_changes=1,
        min_confidence=0.8,
    )

    # First: retry (1 <= 1)
    result1 = handle_escalation(
        raw_payload=ci_failure_payload(attempts=1),
        cli=mock_ao_cli,
        notifier=mock_slack_notifier,
        action_log_path=temp_action_log,
        policy=strict_policy,
    )
    assert result1.action_type == "RetryAction"

    # Second: escalate (2 > 1)
    result2 = handle_escalation(
        raw_payload=ci_failure_payload(attempts=2),
        cli=mock_ao_cli,
        notifier=mock_slack_notifier,
        action_log_path=temp_action_log,
        policy=strict_policy,
    )
    assert result2.action_type == "NotifyJeffreyAction"
