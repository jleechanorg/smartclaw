"""MCP mail helpers for inter-agent communication.

Thin wrapper around mcporter CLI for sending/receiving MCP agent mail.
Used by reviewer_agent to send findings to coder agents and by coder
agents to signal "PR ready for review".
"""

from __future__ import annotations

import logging
import os
import subprocess

logger = logging.getLogger(__name__)

LEGACY_DEFAULT_PROJECT_KEY = ""
SEND_TIMEOUT = 10  # seconds


def get_default_project_key() -> str:
    """Return the MCP project key default.

    Env overrides are checked in order so deployments can switch between
    SmartClaw-specific and broader OpenClaw configuration without code changes.
    """
    return (
        os.environ.get("SMARTCLAW_PROJECT_KEY", "").strip()
        or os.environ.get("OPENCLAW_PROJECT_KEY", "").strip()
        or LEGACY_DEFAULT_PROJECT_KEY
    )


def send_mail(
    to: str,
    subject: str,
    body_md: str,
    sender: str = "claude",
    project: str | None = None,
) -> bool:
    """Send a message via MCP agent mail.

    Args:
        to: Recipient agent name (e.g., 'coder-pr-268')
        subject: Message subject line
        body_md: Markdown body content
        sender: Sender agent name (defaults to 'claude' - a registered sender)
        project: Project key for MCP mail routing. If omitted, uses the
            configured default project key.

    Returns:
        True if send succeeded, False otherwise.
    """
    project = project or get_default_project_key()
    cmd = [
        "mcporter", "call", "mcp-agent-mail.send_message",
        "--project-key", project,
        "--to", to,
        "--subject", subject,
        "--body-md", body_md,
        "--sender-name", sender,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=SEND_TIMEOUT,
        )
        if result.returncode == 0:
            logger.info("Sent MCP mail to %s: %s", to, subject)
            return True
        logger.warning("MCP mail send failed (rc=%d): %s", result.returncode, result.stderr)
        return False
    except subprocess.TimeoutExpired:
        logger.warning("MCP mail send timed out after %ds", SEND_TIMEOUT)
        return False
    except FileNotFoundError:
        logger.warning("mcporter not found — MCP mail unavailable")
        return False


def fetch_inbox(
    agent_name: str,
    project: str | None = None,
    limit: int = 5,
) -> list[dict]:
    """Fetch inbox messages for an agent via MCP mail.

    Args:
        agent_name: Agent name to fetch inbox for
        project: Project key for MCP mail routing. If omitted, uses the
            configured default project key.
        limit: Maximum number of messages to fetch (default: 5)

    Returns:
        List of message dicts, or empty list on failure.
    """
    import json

    project = project or get_default_project_key()
    cmd = [
        "mcporter", "call", "mcp-agent-mail.fetch_inbox",
        "--project-key", project,
        "--agent-name", agent_name,
        "--limit", str(limit),
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=SEND_TIMEOUT,
        )
        if result.returncode == 0 and result.stdout.strip():
            payload = json.loads(result.stdout)
            if isinstance(payload, list) and all(isinstance(item, dict) for item in payload):
                return payload
            logger.warning("Unexpected MCP inbox payload type: %s", type(payload).__name__)
            return []
        return []
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError) as e:
        logger.warning("MCP mail fetch failed: %s", e)
        return []
