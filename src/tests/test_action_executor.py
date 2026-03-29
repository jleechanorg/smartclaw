"""Tests for action_executor: translate router decisions to AO/Slack calls.

These tests verify that escalation actions are executed correctly:
- RetryAction calls ao_send with enriched message
- KillAndRespawnAction calls ao_kill then ao_spawn
- NotifyJeffreyAction sends Slack DM with summary
- NeedsJudgmentAction returns event for LLM processing (no side effect)
- Failed ao_send falls back to NotifyJeffreyAction
- All actions log to ~/.openclaw/state/action_log.jsonl

TDD: These tests will fail until action_executor.py is implemented.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Union
from unittest.mock import MagicMock, patch

import pytest

# These imports will fail until action_executor.py is implemented (TDD)
from orchestration.ao_events import AOEvent, EscalationContext
from orchestration.escalation_router import (
    EscalationAction,
    RetryAction,
    KillAndRespawnAction,
    NotifyJeffreyAction,
    NeedsJudgmentAction,
)

# Import the module under test - will fail until implemented
from orchestration.action_executor import (
    execute_action,
    send_guidance_via_mcp_mail,
    ActionResult,
    SlackNotifier,
    ActionLogEntry,
)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


# Path to action log - will use temp directory for tests
ACTION_LOG_PATH = "/tmp/test_action_log.jsonl"


@dataclass
class MockAOCli:
    """Mock AO CLI for testing action execution."""

    calls: list = field(default_factory=list)
    should_fail_send: bool = False
    should_fail_kill: bool = False
    should_fail_spawn: bool = False
    spawn_return_session: str = "new-session-123"

    def send(self, session_id: str, message: str) -> None:
        """Mock ao_send - records call or fails if configured."""
        self.calls.append(("send", session_id, message))
        if self.should_fail_send:
            from orchestration.ao_cli import AOCommandError
            raise AOCommandError("Send failed", "mock error", 1)

    def kill(self, session_id: str) -> None:
        """Mock ao_kill - records call or fails if configured."""
        self.calls.append(("kill", session_id))
        if self.should_fail_kill:
            from orchestration.ao_cli import AOCommandError
            raise AOCommandError("Kill failed", "mock error", 1)

    def spawn(self, project: str, issue: str, *, branch: str | None = None) -> str:
        """Mock ao_spawn - records call or fails if configured."""
        self.calls.append(("spawn", project, issue, branch))
        if self.should_fail_spawn:
            from orchestration.ao_cli import AOCommandError
            raise AOCommandError("Spawn failed", "mock error", 1)
        return self.spawn_return_session


@dataclass
class MockSlackNotifier:
    """Mock Slack notifier for testing notifications."""

    messages: list = field(default_factory=list)
    should_fail: bool = False

    def send_dm(self, message: str, channel: str | None = None) -> bool:
        """Mock Slack DM sending - records message or fails if configured."""
        self.messages.append({"type": "dm", "message": message, "channel": channel})
        if self.should_fail:
            return False
        return True


@pytest.fixture
def mock_ao_cli():
    """Create a fresh MockAOCli for each test."""
    return MockAOCli()


@pytest.fixture
def mock_slack_notifier():
    """Create a fresh MockSlackNotifier for each test."""
    return MockSlackNotifier()


@pytest.fixture
def action_log_path(tmp_path):
    """Create a temporary action log path for each test."""
    return str(tmp_path / "action_log.jsonl")


# ---------------------------------------------------------------------------
# RetryAction tests
# ---------------------------------------------------------------------------


class TestRetryAction:
    """Tests for executing RetryAction."""

    def test_retry_action_calls_ao_send_with_enriched_message(
        self, mock_ao_cli, mock_slack_notifier, action_log_path
    ):
        """RetryAction should call ao_send with the enriched prompt."""
        action = RetryAction(
            session_id="session-123",
            project_id="jleechanorg/claw",
            prompt="Previous attempt failed with CI. Please analyze and try again.",
            reason="CI failed (attempt 2/3)",
        )

        result = execute_action(
            action,
            cli=mock_ao_cli,
            notifier=mock_slack_notifier,
            action_log_path=action_log_path,
        )

        # Verify ao_send was called with correct session and enriched prompt
        assert len(mock_ao_cli.calls) == 1
        call_type, session_id, message = mock_ao_cli.calls[0]
        assert call_type == "send"
        assert session_id == "session-123"
        assert "Previous attempt failed with CI" in message

        # Verify action result
        assert result.success is True
        assert result.action_type == "RetryAction"
        assert "session-123" in result.details.get("session_id", "")

    def test_retry_action_includes_reason_in_message(
        self, mock_ao_cli, mock_slack_notifier, action_log_path
    ):
        """RetryAction should include the reason in the message to AO."""
        action = RetryAction(
            session_id="session-456",
            project_id="jleechanorg/claw",
            prompt="Fix the failing test.",
            reason="changes-requested (deterministic retry)",
        )

        result = execute_action(
            action,
            cli=mock_ao_cli,
            notifier=mock_slack_notifier,
            action_log_path=action_log_path,
        )

        call_type, session_id, message = mock_ao_cli.calls[0]
        assert call_type == "send"
        assert "changes-requested" in message

    def test_retry_action_fallback_to_notify_on_ao_send_failure(
        self, mock_ao_cli, mock_slack_notifier, action_log_path
    ):
        """Failed ao_send should fall back to NotifyJeffreyAction."""
        # Configure mock to fail on send
        mock_ao_cli.should_fail_send = True

        action = RetryAction(
            session_id="session-fail",
            project_id="jleechanorg/claw",
            prompt="Try again",
            reason="CI failed",
        )

        result = execute_action(
            action,
            cli=mock_ao_cli,
            notifier=mock_slack_notifier,
            action_log_path=action_log_path,
        )

        # Verify ao_send was attempted
        assert len(mock_ao_cli.calls) == 1
        assert mock_ao_cli.calls[0][0] == "send"

        # Verify Slack DM was sent as fallback
        assert len(mock_slack_notifier.messages) == 1
        dm = mock_slack_notifier.messages[0]
        assert dm["type"] == "dm"
        assert "session-fail" in dm["message"]
        assert "failed" in dm["message"].lower()

        # Verify action result indicates fallback was triggered
        assert result.success is False
        assert result.action_type == "RetryAction"
        assert "fallback" in result.details.get("fallback_triggered", "").lower()


# ---------------------------------------------------------------------------
# KillAndRespawnAction tests
# ---------------------------------------------------------------------------


class TestKillAndRespawnAction:
    """Tests for executing KillAndRespawnAction."""

    def test_kill_and_respawn_calls_kill_then_spawn(
        self, mock_ao_cli, mock_slack_notifier, action_log_path
    ):
        """KillAndRespawnAction should call ao_kill then ao_spawn."""
        action = KillAndRespawnAction(
            session_id="old-session-789",
            session_to_kill="old-session-789",
            project_id="worldarchitect/project",
            reason="Session idle for 15 minutes (timeout: 10)",
            task="Continue work on worldarchitect/project",
        )

        result = execute_action(
            action,
            cli=mock_ao_cli,
            notifier=mock_slack_notifier,
            action_log_path=action_log_path,
        )

        # Verify kill was called first, then spawn
        assert len(mock_ao_cli.calls) == 2
        kill_call = mock_ao_cli.calls[0]
        spawn_call = mock_ao_cli.calls[1]

        assert kill_call[0] == "kill"
        assert kill_call[1] == "old-session-789"

        assert spawn_call[0] == "spawn"
        assert spawn_call[1] == "worldarchitect/project"

        # Verify result
        assert result.success is True
        assert result.action_type == "KillAndRespawnAction"
        assert result.details.get("new_session_id") == "new-session-123"

    def test_kill_and_respawn_preserves_reason_in_details(
        self, mock_ao_cli, mock_slack_notifier, action_log_path
    ):
        """KillAndRespawnAction should include the reason in result details."""
        action = KillAndRespawnAction(
            session_id="stuck-session",
            session_to_kill="stuck-session",
            project_id="jleechanorg/claw",
            reason="Session idle for 20 minutes",
            task="Continue work on jleechanorg/claw",
        )

        result = execute_action(
            action,
            cli=mock_ao_cli,
            notifier=mock_slack_notifier,
            action_log_path=action_log_path,
        )

        assert result.details.get("reason") == "Session idle for 20 minutes"
        assert result.details.get("session_killed") == "stuck-session"


# ---------------------------------------------------------------------------
# NotifyJeffreyAction tests
# ---------------------------------------------------------------------------


class TestNotifyJeffreyAction:
    """Tests for executing NotifyJeffreyAction."""

    def test_notify_jeffrey_sends_slack_dm(
        self, mock_ao_cli, mock_slack_notifier, action_log_path
    ):
        """NotifyJeffreyAction should send a Slack DM with the message."""
        action = NotifyJeffreyAction(
            session_id="session-abc",
            message="CI failed after 3 attempts. Human intervention required.",
            pr_url="https://github.com/jleechanorg/claw/pull/42",
            details={"attempts": 3, "reaction_key": "ci-failed"},
        )

        result = execute_action(
            action,
            cli=mock_ao_cli,
            notifier=mock_slack_notifier,
            action_log_path=action_log_path,
        )

        # Verify Slack DM was sent
        assert len(mock_slack_notifier.messages) == 1
        dm = mock_slack_notifier.messages[0]
        assert dm["type"] == "dm"
        assert "session-abc" in dm["message"]
        assert "CI failed" in dm["message"]
        assert "PR #42" in dm["message"] or "pull/42" in dm["message"]

        # Verify no AO CLI calls were made
        assert len(mock_ao_cli.calls) == 0

        # Verify result
        assert result.success is True
        assert result.action_type == "NotifyJeffreyAction"

    def test_notify_jeffrey_includes_pr_url(
        self, mock_ao_cli, mock_slack_notifier, action_log_path
    ):
        """NotifyJeffreyAction should include PR URL in the message."""
        action = NotifyJeffreyAction(
            session_id="session-xyz",
            message="PR is ready for merge",
            pr_url="https://github.com/jleechanorg/claw/pull/100",
        )

        result = execute_action(
            action,
            cli=mock_ao_cli,
            notifier=mock_slack_notifier,
            action_log_path=action_log_path,
        )

        dm = mock_slack_notifier.messages[0]
        assert "pull/100" in dm["message"]

    def test_notify_jeffrey_includes_details(
        self, mock_ao_cli, mock_slack_notifier, action_log_path
    ):
        """NotifyJeffreyAction should include details dict in the message."""
        action = NotifyJeffreyAction(
            session_id="session-details",
            message="Merge conflicts detected",
            details={"conflicting_files": ["src/main.py", "src/cli.py"]},
        )

        result = execute_action(
            action,
            cli=mock_ao_cli,
            notifier=mock_slack_notifier,
            action_log_path=action_log_path,
        )

        dm = mock_slack_notifier.messages[0]
        assert "main.py" in dm["message"] or "conflicting" in dm["message"].lower()


# ---------------------------------------------------------------------------
# NeedsJudgmentAction tests
# ---------------------------------------------------------------------------


class TestNeedsJudgmentAction:
    """Tests for executing NeedsJudgmentAction."""

    def test_needs_judgment_returns_event_for_llm(
        self, mock_ao_cli, mock_slack_notifier, action_log_path
    ):
        """NeedsJudgmentAction should return event for LLM processing, no side effects."""
        # Create a mock AOEvent for the NeedsJudgmentAction
        event = AOEvent(
            event_type="reaction.escalated",
            priority="high",
            session_id="session-llm",
            project_id="jleechanorg/claw",
            message="Review needed for this complex case",
            data={
                "sessionId": "session-llm",
                "projectId": "jleechanorg/claw",
                "reactionKey": "unknown",
                "attempts": 1,
            },
        )

        action = NeedsJudgmentAction(
            event=event,
            context={"current_strategy": "retry", "attempts": 1},
            options=["retry_different_approach", "escalate", "ask_context"],
        )

        result = execute_action(
            action,
            cli=mock_ao_cli,
            notifier=mock_slack_notifier,
            action_log_path=action_log_path,
        )

        # Verify no AO CLI calls were made (no side effects)
        assert len(mock_ao_cli.calls) == 0

        # Verify no Slack messages were sent (no side effects)
        assert len(mock_slack_notifier.messages) == 0

        # Verify result contains event for LLM processing
        assert result.success is True
        assert result.action_type == "NeedsJudgmentAction"
        assert result.details.get("event") is not None
        assert result.details.get("llm_context") is not None

    def test_needs_judgment_includes_options(
        self, mock_ao_cli, mock_slack_notifier, action_log_path
    ):
        """NeedsJudgmentAction should include options for LLM to choose from."""
        event = AOEvent(
            event_type="reaction.escalated",
            priority="medium",
            session_id="session-options",
            project_id="jleechanorg/claw",
            message="Complex case requiring judgment",
            data={},
        )

        action = NeedsJudgmentAction(
            event=event,
            context={"budget_remaining": 2},
            options=["retry", "parallel_retry", "escalate"],
        )

        result = execute_action(
            action,
            cli=mock_ao_cli,
            notifier=mock_slack_notifier,
            action_log_path=action_log_path,
        )

        options = result.details.get("options", [])
        assert len(options) == 3
        assert "retry" in options


# ---------------------------------------------------------------------------
# Action logging tests
# ---------------------------------------------------------------------------


class TestActionLogging:
    """Tests for action logging to action_log.jsonl."""

    def test_all_actions_log_to_jsonl(
        self, mock_ao_cli, mock_slack_notifier, action_log_path
    ):
        """All actions should append an entry to action_log.jsonl."""
        # Execute a RetryAction
        action = RetryAction(
            session_id="log-test-1",
            project_id="jleechanorg/claw",
            prompt="Test prompt",
            reason="test",
        )
        execute_action(
            action,
            cli=mock_ao_cli,
            notifier=mock_slack_notifier,
            action_log_path=action_log_path,
        )

        # Execute a NotifyJeffreyAction
        action2 = NotifyJeffreyAction(
            session_id="log-test-2",
            message="Test notification",
        )
        execute_action(
            action2,
            cli=mock_ao_cli,
            notifier=mock_slack_notifier,
            action_log_path=action_log_path,
        )

        # Verify log file exists and has entries
        log_path = Path(action_log_path)
        assert log_path.exists()

        # Parse log entries
        entries = []
        with open(log_path) as f:
            for line in f:
                if line.strip():
                    entries.append(json.loads(line))

        assert len(entries) == 2
        assert entries[0]["action_type"] == "RetryAction"
        assert entries[1]["action_type"] == "NotifyJeffreyAction"

    def test_log_entry_contains_timestamp_and_details(
        self, mock_ao_cli, mock_slack_notifier, action_log_path
    ):
        """Log entries should contain timestamp and action details."""
        action = RetryAction(
            session_id="log-details",
            project_id="test/project",
            prompt="Test",
            reason="test-reason",
        )
        execute_action(
            action,
            cli=mock_ao_cli,
            notifier=mock_slack_notifier,
            action_log_path=action_log_path,
        )

        log_path = Path(action_log_path)
        with open(log_path) as f:
            entry = json.loads(f.readline())

        assert "timestamp" in entry
        assert "action_type" in entry
        assert entry["action_type"] == "RetryAction"
        assert entry["session_id"] == "log-details"
        assert entry["reason"] == "test-reason"

    def test_failed_actions_also_logged(
        self, mock_ao_cli, mock_slack_notifier, action_log_path
    ):
        """Failed actions should also be logged."""
        # Configure mock to fail send
        mock_ao_cli.should_fail_send = True

        action = RetryAction(
            session_id="fail-log",
            project_id="test/project",
            prompt="Will fail",
            reason="test",
        )
        result = execute_action(
            action,
            cli=mock_ao_cli,
            notifier=mock_slack_notifier,
            action_log_path=action_log_path,
        )

        # Verify logged even though failed
        log_path = Path(action_log_path)
        with open(log_path) as f:
            entry = json.loads(f.readline())

        assert entry["action_type"] == "RetryAction"
        assert entry["success"] is False


# ---------------------------------------------------------------------------
# Edge cases and error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    """Tests for error handling in action execution."""

    def test_kill_failure_does_not_prevent_spawn(
        self, mock_ao_cli, mock_slack_notifier, action_log_path
    ):
        """If ao_kill fails, ao_spawn should still be attempted."""
        mock_ao_cli.should_fail_kill = True

        action = KillAndRespawnAction(
            session_id="fail-kill",
            session_to_kill="fail-kill",
            project_id="test/project",
            reason="stuck",
            task="Continue work on test/project",
        )

        result = execute_action(
            action,
            cli=mock_ao_cli,
            notifier=mock_slack_notifier,
            action_log_path=action_log_path,
        )

        # Both kill and spawn should have been attempted
        assert len(mock_ao_cli.calls) == 2

        # Result should reflect partial success
        assert result.success is True  # Spawn succeeded
        assert "kill" in result.details.get("warning", "").lower()

    def test_spawn_failure_notifies_jeffrey(
        self, mock_ao_cli, mock_slack_notifier, action_log_path
    ):
        """If ao_spawn fails, Jeffrey should be notified."""
        mock_ao_cli.should_fail_spawn = True

        action = KillAndRespawnAction(
            session_id="fail-spawn",
            session_to_kill="fail-spawn",
            project_id="test/project",
            reason="stuck",
            task="Continue work on test/project",
        )

        result = execute_action(
            action,
            cli=mock_ao_cli,
            notifier=mock_slack_notifier,
            action_log_path=action_log_path,
        )

        # Should have attempted kill and spawn, then notified
        assert len(mock_ao_cli.calls) == 2
        assert len(mock_slack_notifier.messages) == 1
        assert result.success is False


# ---------------------------------------------------------------------------
# _send_slack_dm and _send_email_notification unit tests
# ---------------------------------------------------------------------------


class TestSendSlackDm:
    """Unit tests for _send_slack_dm helper."""

    def test_returns_false_without_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPENCLAW_SLACK_BOT_TOKEN", raising=False)
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        from orchestration.action_executor import _send_slack_dm
        assert _send_slack_dm("hello") is False

    def test_returns_true_on_ok_response(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENCLAW_SLACK_BOT_TOKEN", "xoxb-fake")
        from unittest.mock import MagicMock, patch
        from orchestration.action_executor import _send_slack_dm

        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = b'{"ok": true}'

        with patch("orchestration.action_executor.urlopen", return_value=mock_resp):
            assert _send_slack_dm("hello") is True

    def test_returns_false_on_network_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENCLAW_SLACK_BOT_TOKEN", "xoxb-fake")
        from unittest.mock import patch
        from orchestration.action_executor import _send_slack_dm

        with patch("orchestration.action_executor.urlopen", side_effect=OSError("network down")):
            assert _send_slack_dm("hello") is False


class TestSendEmailNotification:
    """Unit tests for _send_email_notification helper."""

    def test_returns_false_without_to_address(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NOTIFY_EMAIL_TO", raising=False)
        from orchestration.action_executor import _send_email_notification
        assert _send_email_notification("hello") is False

    def test_sends_via_starttls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NOTIFY_EMAIL_TO", "jleechan@example.com")
        monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
        monkeypatch.setenv("SMTP_PORT", "587")
        monkeypatch.setenv("SMTP_USER", "user")
        monkeypatch.setenv("SMTP_PASSWORD", "pass")
        from unittest.mock import MagicMock, patch
        from orchestration.action_executor import _send_email_notification

        mock_smtp = MagicMock()
        mock_smtp.__enter__ = lambda s: s
        mock_smtp.__exit__ = MagicMock(return_value=False)

        with patch("orchestration.action_executor.smtplib.SMTP", return_value=mock_smtp):
            result = _send_email_notification("alert: CI failed")
        assert result is True
        mock_smtp.starttls.assert_called_once()
        mock_smtp.sendmail.assert_called_once()

    def test_sends_via_ssl_on_port_465(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NOTIFY_EMAIL_TO", "jleechan@example.com")
        monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
        monkeypatch.setenv("SMTP_PORT", "465")
        from unittest.mock import MagicMock, patch
        from orchestration.action_executor import _send_email_notification

        mock_smtp = MagicMock()
        mock_smtp.__enter__ = lambda s: s
        mock_smtp.__exit__ = MagicMock(return_value=False)

        with patch("orchestration.action_executor.smtplib.SMTP_SSL", return_value=mock_smtp):
            result = _send_email_notification("alert")
        assert result is True
        mock_smtp.sendmail.assert_called_once()

    def test_returns_false_on_smtp_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NOTIFY_EMAIL_TO", "jleechan@example.com")
        monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
        import smtplib as _smtplib
        from unittest.mock import patch
        from orchestration.action_executor import _send_email_notification

        with patch("orchestration.action_executor.smtplib.SMTP", side_effect=_smtplib.SMTPException("auth failed")):
            assert _send_email_notification("alert") is False


class TestSendGuidanceViaMcpMail:
    """Tests for send_guidance_via_mcp_mail helper."""

    def test_uses_configured_project_key_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SMARTCLAW_PROJECT_KEY", "smartclaw-guidance")
        monkeypatch.delenv("OPENCLAW_PROJECT_KEY", raising=False)

        from unittest.mock import patch
        import orchestration.action_executor as _mod

        with patch.object(_mod, "log_guidance_sent") as mock_log:
            with patch("subprocess.run") as mock_run:
                mock_run.return_value.returncode = 0
                assert send_guidance_via_mcp_mail(
                    session_id="session-1",
                    guidance_type="guidance",
                    action_type="retry",
                ) is True

        mock_log.assert_called_once()
        cmd = mock_run.call_args[0][0]
        pidx = cmd.index("--project-key")
        assert cmd[pidx + 1] == "smartclaw-guidance"


class TestParallelNotification:
    """Integration test: RealNotifier fires Slack + email in parallel."""

    def test_real_notifier_calls_both_channels(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """send_dm should invoke _send_slack_dm and _send_email_notification in parallel."""
        from unittest.mock import patch
        import orchestration.action_executor as _mod

        slack_called: list[str] = []
        email_called: list[str] = []

        with patch.object(_mod, "_send_slack_dm", side_effect=lambda msg, ch=None: slack_called.append(msg) or True):
            with patch.object(_mod, "_send_email_notification", side_effect=lambda msg: email_called.append(msg) or True):
                # Instantiate a fresh RealNotifier by calling execute_action with no notifier
                # and a NeedsJudgmentAction (which does not call send_dm), so we test send_dm directly.
                # Instead, directly invoke _send_slack_dm + _send_email_notification through the notifier.
                notifier_instance = _mod._make_real_notifier()
                notifier_instance.send_dm("test escalation message")

        assert slack_called == ["test escalation message"]
        assert email_called == ["test escalation message"]

    def test_real_notifier_succeeds_if_only_slack_works(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NOTIFY_EMAIL_TO", raising=False)
        from unittest.mock import patch
        import orchestration.action_executor as _mod

        with patch.object(_mod, "_send_slack_dm", return_value=True):
            notifier = _mod._make_real_notifier()
            assert notifier.send_dm("alert") is True

    def test_real_notifier_succeeds_if_only_email_works(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NOTIFY_EMAIL_TO", "jleechan@example.com")
        from unittest.mock import patch
        import orchestration.action_executor as _mod

        with patch.object(_mod, "_send_slack_dm", return_value=False):
            with patch.object(_mod, "_send_email_notification", return_value=True):
                notifier = _mod._make_real_notifier()
                assert notifier.send_dm("alert") is True


class TestWaitForCIAction:
    """Tests for WaitForCIAction execution."""

    def test_wait_for_ci_logs_action(self, tmp_path: Path) -> None:
        """WaitForCIAction should log the action and return success."""
        import orchestration.action_executor as _mod
        from orchestration.escalation_router import WaitForCIAction

        action_log_path = str(tmp_path / "action_log.jsonl")

        action = WaitForCIAction(
            session_id="test-session-123",
            project_id="test/project",
            reason="Session idle for 10 minutes, but CI is pending. Extended timeout to 15 minutes.",
            ci_status="pending",
            extended_timeout_minutes=15,
        )

        # Create mock cli and notifier
        mock_cli = MagicMock()
        mock_notifier = MagicMock()

        result = _mod.execute_action(
            action,
            cli=mock_cli,
            notifier=mock_notifier,
            action_log_path=action_log_path,
        )

        assert result.success is True
        assert result.action_type == "WaitForCIAction"
        assert result.details["ci_status"] == "pending"
        assert result.details["extended_timeout_minutes"] == 15

        # Verify log file was written
        import json

        with open(action_log_path) as f:
            log_entry = json.loads(f.readline())
            assert log_entry["action_type"] == "WaitForCIAction"
            assert log_entry["session_id"] == "test-session-123"

    def test_wait_for_ci_does_not_call_cli(self) -> None:
        """WaitForCIAction should not call any CLI methods."""
        import orchestration.action_executor as _mod
        from orchestration.escalation_router import WaitForCIAction

        action = WaitForCIAction(
            session_id="test-session-123",
            project_id="test/project",
            reason="CI is pending",
            ci_status="pending",
            extended_timeout_minutes=15,
        )

        mock_cli = MagicMock()
        mock_notifier = MagicMock()

        _mod.execute_action(action, cli=mock_cli, notifier=mock_notifier)

        # Verify no CLI methods were called
        mock_cli.send.assert_not_called()
        mock_cli.kill.assert_not_called()
        mock_cli.spawn.assert_not_called()
