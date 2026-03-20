"""Guidance tracker: tracks MCP mail guidance delivery and acknowledgment.

This module tracks guidance messages sent to agents via MCP mail and detects
when agents ignore guidance (2+ unacknowledged messages triggers auto-bead filing).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Default path for guidance log
DEFAULT_STATE_DIR = os.path.expanduser("~/.openclaw/state")
DEFAULT_GUIDANCE_LOG = "guidance_log.jsonl"


@dataclass
class GuidanceRecord:
    """Record of guidance sent to an agent."""

    timestamp: str
    session_id: str
    guidance_type: str  # "guidance", "abort", "strategy_override"
    acknowledged: bool
    acknowledgment_timestamp: str | None = None
    acknowledgment_reason: str | None = None


def _get_guidance_log_path() -> Path:
    """Get the path to the guidance log file."""
    state_dir = Path(os.path.expanduser(DEFAULT_STATE_DIR))
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / DEFAULT_GUIDANCE_LOG


def log_guidance_sent(
    session_id: str,
    guidance_type: str,
    payload: dict | None = None,
) -> None:
    """Log that guidance was sent to an agent.

    Args:
        session_id: The agent's session ID
        guidance_type: Type of guidance ("guidance", "abort", "strategy_override")
        payload: Optional additional payload
    """
    log_path = _get_guidance_log_path()
    
    record = GuidanceRecord(
        timestamp=datetime.now(timezone.utc).isoformat(),
        session_id=session_id,
        guidance_type=guidance_type,
        acknowledged=False,
    )
    
    try:
        with open(log_path, "a") as f:
            f.write(json.dumps(asdict(record)) + "\n")
        logger.debug(f"Logged guidance sent to {session_id}: {guidance_type}")
    except Exception as e:
        logger.warning(f"Failed to log guidance sent: {e}")


def log_guidance_acknowledged(
    session_id: str,
    reason: str | None = None,
) -> bool:
    """Log that guidance was acknowledged by an agent.

    Args:
        session_id: The agent's session ID
        reason: Optional reason for acknowledgment

    Returns:
        True if acknowledgment was logged, False otherwise
    """
    log_path = _get_guidance_log_path()
    
    if not log_path.exists():
        return False
    
    # Find the oldest unacknowledged guidance for this session
    try:
        records = []
        with open(log_path) as f:
            for line in f:
                if line.strip():
                    records.append(json.loads(line))
        
        # Find oldest unacknowledged for this session
        for record in records:
            if record.get("session_id") == session_id and not record.get("acknowledged"):
                record["acknowledged"] = True
                record["acknowledgment_timestamp"] = datetime.now(timezone.utc).isoformat()
                if reason:
                    record["acknowledgment_reason"] = reason
                
                # Rewrite file with updated record
                with open(log_path, "w") as f:
                    for r in records:
                        f.write(json.dumps(r) + "\n")
                return True
    except Exception as e:
        logger.warning(f"Failed to log acknowledgment: {e}")
    
    return False


def check_ignored_guidance(
    session_id: str,
    max_age_minutes: int = 10,
) -> list[dict]:
    """Check for ignored (unacknowledged) guidance older than threshold.

    Args:
        session_id: The agent's session ID
        max_age_minutes: Consider guidance ignored after this many minutes

    Returns:
        List of ignored guidance records
    """
    log_path = _get_guidance_log_path()
    
    if not log_path.exists():
        return []
    
    ignored = []
    now = datetime.now(timezone.utc)
    
    try:
        with open(log_path) as f:
            for line in f:
                if not line.strip():
                    continue
                record = json.loads(line)
                
                if record.get("session_id") != session_id:
                    continue
                if record.get("acknowledged"):
                    continue
                
                # Check age
                try:
                    sent_time = datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00"))
                    age_minutes = (now - sent_time).total_seconds() / 60
                    if age_minutes >= max_age_minutes:
                        ignored.append(record)
                except Exception:
                    continue
    except Exception as e:
        logger.warning(f"Failed to check ignored guidance: {e}")
    
    return ignored


def count_ignored_guidance(session_id: str) -> int:
    """Count the number of ignored guidance messages for a session.

    Args:
        session_id: The agent's session ID

    Returns:
        Number of unacknowledged guidance messages
    """
    return len(check_ignored_guidance(session_id))


def auto_file_bead_for_ignored(session_id: str) -> str | None:
    """Auto-file a bead if agent has ignored 2+ guidance messages.

    Args:
        session_id: The agent's session ID

    Returns:
        Bead ID if created, None otherwise
    """
    ignored_count = count_ignored_guidance(session_id)
    
    if ignored_count >= 2:
        try:
            import subprocess
            result = subprocess.run(
                ["br", "create", "--type", "bug", "--priority", "1",
                 f"Agent {session_id} ignored {ignored_count} guidance messages"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                # Extract bead ID from output
                for line in result.stdout.split("\n"):
                    if "ORCH-" in line:
                        logger.info(f"Auto-filed bead for ignored guidance: {line.strip()}")
                        return line.strip()
        except Exception as e:
            logger.warning(f"Failed to auto-file bead: {e}")
    
    return None
