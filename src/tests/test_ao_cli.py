"""Tests for ao_cli: wrapping ao CLI commands for orchestration layer.

These tests use mocking to verify the subprocess calls without requiring
the actual `ao` CLI to be installed. The tests verify:
- Correct argument construction for each command
- Timeout handling (10s default)
- Error handling with AOCommandError
- JSON parsing for ao_list output
"""

from __future__ import annotations

import json
import subprocess
import pytest
from unittest.mock import MagicMock, patch
from dataclasses import dataclass

# These imports will fail until ao_cli.py is implemented (TDD)
from orchestration.ao_cli import (
    ao_spawn,
    ao_send,
    ao_kill,
    ao_list,
    AOCommandError,
    AOSession,
)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@dataclass
class MockSession:
    """Represents a mock AO session for testing."""
    session_id: str
    project: str
    status: str
    branch: str | None = None
    pr: str | None = None


def make_session_json(sessions: list[MockSession]) -> str:
    """Convert mock sessions to JSON string as ao CLI would output."""
    return json.dumps([
        {
            "session_id": s.session_id,
            "project": s.project,
            "status": s.status,
            "branch": s.branch,
            "pr": s.pr,
        }
        for s in sessions
    ])


# ---------------------------------------------------------------------------
# ao_spawn tests
# ---------------------------------------------------------------------------


@patch("orchestration.ao_cli.subprocess.run")
def test_ao_spawn_basic(mock_run: MagicMock) -> None:
    """ao_spawn should call ao with correct project and issue args."""
    # Arrange
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "ao-session-abc123"
    mock_result.stderr = ""
    mock_run.return_value = mock_result

    # Act
    result = ao_spawn("jleechanorg/claw", "Fix auth middleware")

    # Assert
    mock_run.assert_called_once()
    call_args = mock_run.call_args

    # Verify command includes correct args
    assert "ao" in call_args[0][0]
    assert "spawn" in call_args[0][0]
    assert "jleechanorg/claw" in call_args[0][0]
    assert "Fix auth middleware" in call_args[0][0]

    # Verify spawn uses 60s timeout (worktree + tmux setup takes ~7s)
    assert call_args[1]["timeout"] == 60

    # Verify returns session ID from stdout
    assert result == "ao-session-abc123"


@patch("orchestration.ao_cli.subprocess.run")
def test_ao_spawn_with_branch(mock_run: MagicMock) -> None:
    """ao_spawn should accept optional branch parameter."""
    # Arrange
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "ao-session-xyz789"
    mock_result.stderr = ""
    mock_run.return_value = mock_result

    # Act
    result = ao_spawn("jleechanorg/claw", "Add feature", branch="feature/new")

    # Assert
    call_args = mock_run.call_args
    assert "feature/new" in call_args[0][0]
    assert result == "ao-session-xyz789"


@patch("orchestration.ao_cli.subprocess.run")
def test_ao_spawn_error_includes_stderr(mock_run: MagicMock) -> None:
    """Non-zero exit should raise AOCommandError with stderr."""
    # Arrange
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = ""
    mock_result.stderr = "Project not found: jleechanorg/nonexistent"
    mock_run.return_value = mock_result

    # Act & Assert
    with pytest.raises(AOCommandError) as exc_info:
        ao_spawn("jleechanorg/nonexistent", "Test issue")

    assert "Project not found" in str(exc_info.value)
    assert exc_info.value.returncode == 1


# ---------------------------------------------------------------------------
# ao_send tests
# ---------------------------------------------------------------------------


@patch("orchestration.ao_cli.subprocess.run")
def test_ao_send_basic(mock_run: MagicMock) -> None:
    """ao_send should call ao with correct session and message args."""
    # Arrange
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "Message sent"
    mock_result.stderr = ""
    mock_run.return_value = mock_result

    # Act
    ao_send("ao-session-123", "Please fix the failing test")

    # Assert
    mock_run.assert_called_once()
    call_args = mock_run.call_args

    assert "ao" in call_args[0][0]
    assert "send" in call_args[0][0]
    assert "ao-session-123" in call_args[0][0]
    assert "Please fix the failing test" in call_args[0][0]
    assert call_args[1]["timeout"] == 10


@patch("orchestration.ao_cli.subprocess.run")
def test_ao_send_error_handling(mock_run: MagicMock) -> None:
    """ao_send should raise AOCommandError on non-zero exit."""
    # Arrange
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = ""
    mock_result.stderr = "Session not found: ao-session-999"
    mock_run.return_value = mock_result

    # Act & Assert
    with pytest.raises(AOCommandError) as exc_info:
        ao_send("ao-session-999", "Hello")

    assert "Session not found" in str(exc_info.value)
    assert exc_info.value.returncode == 1


# ---------------------------------------------------------------------------
# ao_kill tests
# ---------------------------------------------------------------------------


@patch("orchestration.ao_cli.subprocess.run")
def test_ao_kill_basic(mock_run: MagicMock) -> None:
    """ao_kill should call ao kill with session_id."""
    # Arrange
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "Session terminated"
    mock_result.stderr = ""
    mock_run.return_value = mock_result

    # Act
    ao_kill("ao-session-456")

    # Assert
    mock_run.assert_called_once()
    call_args = mock_run.call_args

    assert "ao" in call_args[0][0]
    assert "kill" in call_args[0][0]
    assert "ao-session-456" in call_args[0][0]
    assert call_args[1]["timeout"] == 10


@patch("orchestration.ao_cli.subprocess.run")
def test_ao_kill_error_handling(mock_run: MagicMock) -> None:
    """ao_kill should raise AOCommandError on non-zero exit."""
    # Arrange
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = ""
    mock_result.stderr = "Cannot kill session: session already terminated"
    mock_run.return_value = mock_result

    # Act & Assert
    with pytest.raises(AOCommandError) as exc_info:
        ao_kill("ao-session-dead")

    assert "Cannot kill session" in str(exc_info.value)
    assert exc_info.value.returncode == 1


# ---------------------------------------------------------------------------
# ao_list tests
# ---------------------------------------------------------------------------


@patch("orchestration.ao_cli.subprocess.run")
def test_ao_list_all_sessions(mock_run: MagicMock) -> None:
    """ao_list with no project should return all sessions."""
    # Arrange
    sessions = [
        MockSession("ao-session-1", "jleechanorg/claw", "running", "main"),
        MockSession("ao-session-2", "worldarchitect/ai", "idle", "feature/x"),
    ]
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = make_session_json(sessions)
    mock_result.stderr = ""
    mock_run.return_value = mock_result

    # Act
    result = ao_list()

    # Assert
    mock_run.assert_called_once()
    call_args = mock_run.call_args

    # Should call ao list without project filter
    assert "ao" in call_args[0][0]
    assert "list" in call_args[0][0]
    assert call_args[1]["timeout"] == 10

    # Should parse into AOSession objects
    assert len(result) == 2
    assert result[0].session_id == "ao-session-1"
    assert result[0].project == "jleechanorg/claw"
    assert result[0].status == "running"
    assert result[0].branch == "main"
    assert result[1].session_id == "ao-session-2"
    assert result[1].project == "worldarchitect/ai"


@patch("orchestration.ao_cli.subprocess.run")
def test_ao_list_filtered_by_project(mock_run: MagicMock) -> None:
    """ao_list with project should filter sessions by project."""
    # Arrange
    sessions = [
        MockSession("ao-session-1", "jleechanorg/claw", "running", "main"),
    ]
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = make_session_json(sessions)
    mock_result.stderr = ""
    mock_run.return_value = mock_result

    # Act
    result = ao_list("jleechanorg/claw")

    # Assert
    call_args = mock_run.call_args

    # Should include project in command
    assert "jleechanorg/claw" in call_args[0][0]

    # Should parse correctly
    assert len(result) == 1
    assert result[0].session_id == "ao-session-1"


@patch("orchestration.ao_cli.subprocess.run")
def test_ao_list_empty(mock_run: MagicMock) -> None:
    """ao_list should return empty list when no sessions exist."""
    # Arrange
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "[]"
    mock_result.stderr = ""
    mock_run.return_value = mock_result

    # Act
    result = ao_list()

    # Assert
    assert result == []


@patch("orchestration.ao_cli.subprocess.run")
def test_ao_list_error_handling(mock_run: MagicMock) -> None:
    """ao_list should raise AOCommandError on non-zero exit."""
    # Arrange
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = ""
    mock_result.stderr = "Failed to list sessions: API rate limited"
    mock_run.return_value = mock_result

    # Act & Assert
    with pytest.raises(AOCommandError) as exc_info:
        ao_list()

    assert "API rate limited" in str(exc_info.value)
    assert exc_info.value.returncode == 1


@patch("orchestration.ao_cli.subprocess.run")
def test_ao_list_malformed_json(mock_run: MagicMock) -> None:
    """ao_list should raise AOCommandError when JSON is malformed."""
    # Arrange
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "not valid json"
    mock_result.stderr = ""
    mock_run.return_value = mock_result

    # Act & Assert
    with pytest.raises(AOCommandError) as exc_info:
        ao_list()

    assert "json" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# Timeout tests
# ---------------------------------------------------------------------------


@patch("orchestration.ao_cli.subprocess.run")
def test_default_timeout_is_10_seconds(mock_run: MagicMock) -> None:
    """send/kill/list use 10s timeout; spawn uses 60s (worktree setup)."""
    # Arrange
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = ""
    mock_result.stderr = ""
    mock_run.return_value = mock_result

    # Act - call each function
    ao_spawn("test/project", "test issue")
    ao_send("ao-session-1", "test message")
    ao_kill("ao-session-1")
    ao_list()

    calls = mock_run.call_args_list
    # spawn (first call) uses 60s; others use 10s
    assert calls[0][1]["timeout"] == 60
    for call in calls[1:]:
        assert call[1]["timeout"] == 10


@patch("orchestration.ao_cli.subprocess.run")
def test_timeout_triggers_subprocess_timeout(mock_run: MagicMock) -> None:
    """subprocess.run timeout should propagate as AOCommandError."""
    # Arrange
    mock_run.side_effect = subprocess.TimeoutExpired("ao spawn", 10)

    # Act & Assert
    with pytest.raises(AOCommandError) as exc_info:
        ao_spawn("test/project", "test issue")

    assert "timeout" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# AOSession dataclass tests
# ---------------------------------------------------------------------------


def test_aosession_dataclass_fields() -> None:
    """AOSession should have expected fields."""
    session = AOSession(
        session_id="ao-session-123",
        project="jleechanorg/claw",
        status="running",
        branch="main",
        pr="https://github.com/jleechanorg/claw/pull/42",
    )

    assert session.session_id == "ao-session-123"
    assert session.project == "jleechanorg/claw"
    assert session.status == "running"
    assert session.branch == "main"
    assert session.pr == "https://github.com/jleechanorg/claw/pull/42"


def test_aosession_optional_fields() -> None:
    """AOSession should allow None for optional branch and pr."""
    session = AOSession(
        session_id="ao-session-456",
        project="jleechanorg/claw",
        status="idle",
    )

    assert session.session_id == "ao-session-456"
    assert session.branch is None
    assert session.pr is None
