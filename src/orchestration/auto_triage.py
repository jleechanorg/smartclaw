"""Auto-triage: proactive Slack DM when same error class escalates repeatedly.

This module implements the convergence intelligence feature:
- Scans outcomes.jsonl for repeated escalations (NotifyJeffreyAction)
- Sends proactive Slack DM when same error_class escalates 2x+ in 7 days

This helps Jeffrey catch systemic issues before they become persistent problems.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)

# Default paths
DEFAULT_STATE_DIR = "~/.openclaw/state"
DEFAULT_OUTCOMES_FILE = "outcomes.jsonl"


# Data structures
@dataclass
class RepeatedEscalation:
    """An error class that has escalated multiple times."""

    error_class: str
    escalation_count: int
    prs: list[str]  # List of PR URLs
    first_escalation: str  # ISO timestamp
    last_escalation: str  # ISO timestamp


class SlackNotifier(Protocol):
    """Protocol for Slack notification."""

    def send_dm(self, message: str, channel: str) -> bool:
        """Send a direct message."""
        ...


def scan_repeated_escalations(
    window_days: int = 7,
    threshold: int = 2,
    outcomes_path: str | None = None,
) -> list[RepeatedEscalation]:
    """Scan outcomes.jsonl for error classes that have escalated repeatedly.

    Args:
        window_days: Number of days to look back (default 7)
        threshold: Minimum escalations to trigger notification (default 2)
        outcomes_path: Optional path to outcomes.jsonl

    Returns:
        List of RepeatedEscalation objects for error classes exceeding threshold
    """
    if outcomes_path is None:
        state_dir = os.path.expanduser(DEFAULT_STATE_DIR)
        outcomes_path = os.path.join(state_dir, DEFAULT_OUTCOMES_FILE)

    outcomes_file = Path(outcomes_path)
    if not outcomes_file.exists():
        logger.debug(f"Outcomes file not found: {outcomes_path}")
        return []

    # Track escalations by error_class
    # An escalation is when the result is "escalated" or action was NotifyJeffreyAction
    error_class_escalations: dict[str, list[dict]] = {}

    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)

    try:
        with open(outcomes_file, "r") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Check if this is an escalation
                result = entry.get("result", "")
                action = entry.get("action", "")

                # Escalation indicators: result="escalated" or action="NotifyJeffreyAction"
                is_escalation = result == "escalated" or action == "NotifyJeffreyAction"

                if not is_escalation:
                    continue

                # Check timestamp
                timestamp_str = entry.get("timestamp", "")
                if not timestamp_str:
                    continue

                try:
                    timestamp = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    continue

                if timestamp < cutoff:
                    continue

                error_class = entry.get("error_class", "unknown")
                pr_url = entry.get("pr_url", entry.get("pr", "unknown"))

                if error_class not in error_class_escalations:
                    error_class_escalations[error_class] = []

                error_class_escalations[error_class].append({
                    "pr": pr_url,
                    "timestamp": timestamp_str,
                })

    except Exception as e:
        logger.warning(f"Failed to scan outcomes: {e}")
        return []

    # Filter to only those exceeding threshold
    repeated: list[RepeatedEscalation] = []
    for error_class, escalations in error_class_escalations.items():
        if len(escalations) >= threshold:
            # Sort by timestamp
            sorted_escalations = sorted(escalations, key=lambda x: x.get("timestamp", ""))
            prs = [e["pr"] for e in sorted_escalations]
            timestamps = [e["timestamp"] for e in sorted_escalations if e.get("timestamp")]

            repeated.append(RepeatedEscalation(
                error_class=error_class,
                escalation_count=len(escalations),
                prs=prs,
                first_escalation=timestamps[0] if timestamps else "",
                last_escalation=timestamps[-1] if timestamps else "",
            ))

    # Sort by escalation count (most frequent first)
    repeated.sort(key=lambda x: x.escalation_count, reverse=True)

    return repeated


def notify_repeated_escalation(
    escalation: RepeatedEscalation,
    notifier: SlackNotifier | None = None,
) -> bool:
    """Send a Slack DM about repeated escalations.

    Args:
        escalation: The RepeatedEscalation to report
        notifier: Slack notifier (if None, logs only)

    Returns:
        True if notification sent successfully
    """
    # Build message
    lines = [
        f"🚨 *Repeated Error Escalation Detected*",
        f"",
        f"*Error Class:* `{escalation.error_class}`",
        f"*Escalations:* {escalation.escalation_count}x in the last 7 days",
        f"",
        f"*Recent PRs:*",
    ]

    # Add up to 5 PRs
    for pr in escalation.prs[:5]:
        lines.append(f"  • {pr}")

    if len(escalation.prs) > 5:
        lines.append(f"  • ... and {len(escalation.prs) - 5} more")

    lines.append(f"")
    lines.append(f"Consider creating a bead to track this systemic issue.")

    message = "\n".join(lines)

    logger.info(f"Auto-triage: {escalation.escalation_count}x escalation of {escalation.error_class}")

    if notifier is None:
        logger.debug(f"Would send DM: {message[:200]}...")
        return False

    try:
        # Use JEFFREY_DM_CHANNEL
        success = notifier.send_dm(message, channel=os.environ.get("SMARTCLAW_DM_CHANNEL", ""))
        if success:
            logger.info(f"Sent auto-triage DM for {escalation.error_class}")
        return success
    except Exception as e:
        logger.warning(f"Failed to send auto-triage DM: {e}")
        return False


def check_and_notify_repeated(
    error_class: str,
    pr_url: str | None = None,
    notifier: SlackNotifier | None = None,
    outcomes_path: str | None = None,
) -> None:
    """Check for repeated escalations and notify if threshold exceeded.

    This should be called after NotifyJeffreyAction is executed.

    Args:
        error_class: The error class that just escalated
        pr_url: Optional PR URL that just escalated
        notifier: Slack notifier (if None, logs only)
        outcomes_path: Optional path to outcomes.jsonl
    """
    # Scan for repeated escalations (including this one)
    repeated = scan_repeated_escalations(
        window_days=7,
        threshold=2,
        outcomes_path=outcomes_path,
    )

    # Find matching escalation
    for escalation in repeated:
        if escalation.error_class == error_class:
            notify_repeated_escalation(escalation, notifier)
            break
