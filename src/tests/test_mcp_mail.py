"""Tests for mcp_mail module."""
from __future__ import annotations

from unittest.mock import patch

from orchestration.mcp_mail import fetch_inbox, send_mail


class TestSendMail:
    """Tests for send_mail()."""

    @patch("orchestration.mcp_mail.subprocess.run")
    def test_successful_send(self, mock_run) -> None:
        mock_run.return_value.returncode = 0
        result = send_mail(to="coder-1", subject="Review", body_md="# OK")
        assert result is True
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "mcp-agent-mail.send_message" in cmd
        assert "--to" in cmd
        assert "coder-1" in cmd

    @patch("orchestration.mcp_mail.subprocess.run")
    def test_failed_send(self, mock_run) -> None:
        mock_run.return_value.returncode = 1
        mock_run.return_value.stderr = "connection refused"
        result = send_mail(to="coder-1", subject="Review", body_md="# Fail")
        assert result is False

    @patch("orchestration.mcp_mail.subprocess.run")
    def test_timeout(self, mock_run) -> None:
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="mcporter", timeout=10)
        result = send_mail(to="coder-1", subject="Review", body_md="# Timeout")
        assert result is False

    @patch("orchestration.mcp_mail.subprocess.run")
    def test_mcporter_not_found(self, mock_run) -> None:
        mock_run.side_effect = FileNotFoundError("mcporter")
        result = send_mail(to="coder-1", subject="Review", body_md="# Missing")
        assert result is False

    @patch("orchestration.mcp_mail.subprocess.run")
    def test_custom_sender_and_project(self, mock_run) -> None:
        mock_run.return_value.returncode = 0
        send_mail(to="x", subject="s", body_md="b", sender="coder", project="myproj")
        cmd = mock_run.call_args[0][0]
        assert "--sender-name" in cmd
        idx = cmd.index("--sender-name")
        assert cmd[idx + 1] == "coder"
        assert "--project-key" in cmd
        pidx = cmd.index("--project-key")
        assert cmd[pidx + 1] == "myproj"

    @patch("orchestration.mcp_mail.subprocess.run")
    def test_defaults_to_legacy_project_when_env_unset(self, mock_run, monkeypatch) -> None:
        monkeypatch.delenv("SMARTCLAW_PROJECT_KEY", raising=False)
        monkeypatch.delenv("OPENCLAW_PROJECT_KEY", raising=False)
        mock_run.return_value.returncode = 0
        send_mail(to="coder-1", subject="Review", body_md="# OK")
        cmd = mock_run.call_args[0][0]
        pidx = cmd.index("--project-key")
        assert cmd[pidx + 1] == "jleechanclaw"

    @patch("orchestration.mcp_mail.subprocess.run")
    def test_uses_configured_project_key_from_env(self, mock_run, monkeypatch) -> None:
        monkeypatch.setenv("SMARTCLAW_PROJECT_KEY", "smartclaw-dev")
        monkeypatch.delenv("OPENCLAW_PROJECT_KEY", raising=False)
        mock_run.return_value.returncode = 0
        send_mail(to="coder-1", subject="Review", body_md="# OK")
        cmd = mock_run.call_args[0][0]
        pidx = cmd.index("--project-key")
        assert cmd[pidx + 1] == "smartclaw-dev"


class TestFetchInbox:
    """Tests for fetch_inbox()."""

    @patch("orchestration.mcp_mail.subprocess.run")
    def test_successful_fetch(self, mock_run) -> None:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = '[{"subject": "hello"}]'
        result = fetch_inbox("reviewer")
        assert len(result) == 1
        assert result[0]["subject"] == "hello"

    @patch("orchestration.mcp_mail.subprocess.run")
    def test_empty_inbox(self, mock_run) -> None:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "[]"
        result = fetch_inbox("reviewer")
        assert result == []

    @patch("orchestration.mcp_mail.subprocess.run")
    def test_fetch_failure(self, mock_run) -> None:
        mock_run.return_value.returncode = 1
        mock_run.return_value.stdout = ""
        result = fetch_inbox("reviewer")
        assert result == []

    @patch("orchestration.mcp_mail.subprocess.run")
    def test_fetch_timeout(self, mock_run) -> None:
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="mcporter", timeout=10)
        result = fetch_inbox("reviewer")
        assert result == []

    @patch("orchestration.mcp_mail.subprocess.run")
    def test_uses_configured_project_key_from_env(self, mock_run, monkeypatch) -> None:
        monkeypatch.delenv("SMARTCLAW_PROJECT_KEY", raising=False)
        monkeypatch.setenv("OPENCLAW_PROJECT_KEY", "openclaw-dev")
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "[]"
        fetch_inbox("reviewer")
        cmd = mock_run.call_args[0][0]
        pidx = cmd.index("--project-key")
        assert cmd[pidx + 1] == "openclaw-dev"
