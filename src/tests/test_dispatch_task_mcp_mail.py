"""Tests for dispatch_task MCP mail registration."""

import pytest
from unittest.mock import patch, MagicMock

from orchestration.dispatch_task import register_agent_mcp_mail


def test_register_agent_mcp_mail_success() -> None:
    """Test successful MCP mail registration."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        
        result = register_agent_mcp_mail(
            session_name="ao-session-pr-237",
            project="jleechanclaw",
            pr_number="237",
            agent_cli="claude",
        )
        
        assert result is True
        mock_run.assert_called_once()


def test_register_agent_mcp_mail_failure() -> None:
    """Test MCP mail registration failure is non-blocking."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stderr="Connection refused")
        
        result = register_agent_mcp_mail(
            session_name="ao-session-pr-237",
            project="jleechanclaw",
            pr_number="237",
            agent_cli="claude",
        )
        
        # Should return False but not raise
        assert result is False


def test_register_agent_mcp_mail_exception() -> None:
    """Test MCP mail registration handles exceptions gracefully."""
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = Exception("Connection refused")
        
        # Should not raise
        result = register_agent_mcp_mail(
            session_name="ao-session-pr-237",
            project="jleechanclaw",
            pr_number="237",
            agent_cli="claude",
        )
        
        assert result is False


def test_register_agent_mcp_mail_no_pr() -> None:
    """Test MCP mail registration without PR number."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        
        result = register_agent_mcp_mail(
            session_name="ao-session-general",
            project="jleechanclaw",
            pr_number=None,
            agent_cli="codex",
        )
        
        assert result is True
        # Verify call was made with correct args
        call_args = mock_run.call_args[0][0]
        assert "--name" in call_args
        assert "ao-session-general" in call_args


def test_register_agent_mcp_mail_cursor() -> None:
    """Test MCP mail registration with cursor agent."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        
        result = register_agent_mcp_mail(
            session_name="ao-session-pr-240",
            project="worldarchitect",
            pr_number="240",
            agent_cli="cursor",
        )
        
        assert result is True
        # Verify cursor maps to cursor-agent
        call_args = mock_run.call_args[0][0]
        assert "--program" in call_args


def test_register_agent_mcp_mail_project_key_env_override() -> None:
    """Test MCP project key can be overridden by environment."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        with patch.dict("os.environ", {"SMARTCLAW_PROJECT_KEY": "portable-project"}, clear=False):
            result = register_agent_mcp_mail(
                session_name="ao-session-pr-250",
                project="worldarchitect",
                pr_number="250",
                agent_cli="claude",
            )

        assert result is True
        call_args = mock_run.call_args[0][0]
        assert "--project-key" in call_args
        project_key_idx = call_args.index("--project-key") + 1
        assert call_args[project_key_idx] == "portable-project"
