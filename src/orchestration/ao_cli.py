"""AO CLI wrapper for orchestration layer.

This module provides typed wrappers around the `ao` CLI commands used by the
orchestration layer to manage AO sessions, send messages, and list sessions.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path


# Default timeout for all AO CLI commands (in seconds)
DEFAULT_TIMEOUT = 10


@dataclass
class AOSession:
    """Represents an active AO session.

    Attributes:
        session_id: Unique identifier for the AO session
        project: Project identifier (e.g., 'owner/repo')
        status: Current session status ('running', 'idle', etc.)
        branch: Optional branch name associated with the session
        pr: Optional PR URL associated with the session
    """

    session_id: str
    project: str
    status: str
    branch: str | None = None
    pr: str | None = None


class AOCommandError(Exception):
    """Raised when an AO CLI command fails.

    Attributes:
        stderr: The standard error output from the failed command
        returncode: The exit code returned by the command
    """

    def __init__(self, message: str, stderr: str, returncode: int) -> None:
        super().__init__(message)
        self.stderr = stderr
        self.returncode = returncode


def _run_ao_command(args: list[str], timeout: int = DEFAULT_TIMEOUT) -> subprocess.CompletedProcess:
    """Run an AO CLI command with default timeout.

    Args:
        args: List of command arguments (including 'ao')
        timeout: Seconds before raising TimeoutExpired (default: DEFAULT_TIMEOUT)

    Returns:
        CompletedProcess instance

    Raises:
        AOCommandError: If the command returns non-zero exit code or times out
    """
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=Path.home(),
        )
    except subprocess.TimeoutExpired as e:
        raise AOCommandError(
            f"Command timed out after {timeout} seconds (timeout)",
            stderr=str(e),
            returncode=-1,
        )

    if result.returncode != 0:
        raise AOCommandError(
            result.stderr.strip() or f"Command failed with exit code {result.returncode}",
            stderr=result.stderr.strip(),
            returncode=result.returncode,
        )

    return result


def ao_spawn(project: str, issue: str, *, branch: str | None = None) -> str:
    """Spawn a new AO session for a project with the given issue.

    Args:
        project: Project identifier (e.g., 'owner/repo')
        issue: Issue or task description to spawn
        branch: Optional branch name to create/use

    Returns:
        Session ID string from AO CLI stdout

    Raises:
        AOCommandError: If the spawn command fails
        subprocess.TimeoutExpired: If the command exceeds 10s timeout
    """
    args = ["ao", "spawn", project, issue]

    if branch is not None:
        args.extend(["--branch", branch])

    # Spawn creates a worktree + tmux session; allow up to 60s
    result = _run_ao_command(args, timeout=60)
    return result.stdout.strip()


def ao_send(session_id: str, message: str) -> None:
    """Send a message to an existing AO session.

    Args:
        session_id: The session ID to send the message to
        message: The message text to send

    Raises:
        AOCommandError: If the send command fails
        subprocess.TimeoutExpired: If the command exceeds 10s timeout
    """
    args = ["ao", "send", session_id, message]
    _run_ao_command(args)


def ao_kill(session_id: str) -> None:
    """Terminate an existing AO session.

    Args:
        session_id: The session ID to terminate

    Raises:
        AOCommandError: If the kill command fails
        subprocess.TimeoutExpired: If the command exceeds 10s timeout
    """
    args = ["ao", "kill", session_id]
    _run_ao_command(args)


def ao_list(project: str | None = None) -> list[AOSession]:
    """List AO sessions, optionally filtered by project.

    Args:
        project: Optional project identifier to filter sessions

    Returns:
        List of AOSession objects parsed from CLI JSON output

    Raises:
        AOCommandError: If the list command fails or returns malformed JSON
        subprocess.TimeoutExpired: If the command exceeds 10s timeout
    """
    args = ["ao", "list"]

    if project is not None:
        args.append(project)

    result = _run_ao_command(args)

    # Handle empty output as empty list
    if not result.stdout.strip():
        return []

    try:
        session_data = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise AOCommandError(
            f"Failed to parse JSON output: {e}",
            stderr=str(e),
            returncode=0,
        )

    sessions = []
    for item in session_data:
        sessions.append(
            AOSession(
                session_id=item["session_id"],
                project=item["project"],
                status=item["status"],
                branch=item.get("branch"),
                pr=item.get("pr"),
            )
        )

    return sessions