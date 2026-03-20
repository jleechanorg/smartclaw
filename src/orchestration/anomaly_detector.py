"""Anomaly detector: monitor escalation patterns and alert on anomalies.

This module monitors the action_log.jsonl for escalation patterns:
- Counts escalations per error_class in the rolling 7-day window
- If any error_class has >= 2 escalations in the rolling week, sends Slack DM
- Provides a summary with error class, count, first/last seen, and PR context

Runs as a weekly cron job (Monday 09:00).
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Default paths
DEFAULT_STATE_DIR = "~/.openclaw/state"
DEFAULT_ACTION_LOG = "action_log.jsonl"

# Threshold for anomaly detection
ESCALATION_THRESHOLD = 2


@dataclass
class EscalationRecord:
    """Single escalation event from action_log."""
    timestamp: str
    session_id: str
    action_type: str
    success: bool
    error_class: str | None = None
    reason: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class EscalationSummary:
    """Summary of escalations for a specific error_class."""
    error_class: str
    count: int
    first_seen: str
    last_seen: str
    sessions: list[str]
    recent_reasons: list[str]


def read_action_log(log_path: Path | str) -> list[dict[str, Any]]:
    """Read action log JSONL file and return list of entries."""
    path = Path(log_path).expanduser()
    if not path.exists():
        return []

    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def filter_escalations(
    entries: list[dict[str, Any]],
    days: int = 7,
) -> list[EscalationRecord]:
    """Filter entries to escalations within the specified day window."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    escalations = []

    for entry in entries:
        try:
            ts_str = entry.get("timestamp", "")
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue

        if ts < cutoff:
            continue

        # Include escalation-related action types
        # Note: action_executor logs these as KillAndRespawnAction and NotifyJeffreyAction
        action_type = entry.get("action_type", "")
        if "Escalation" in action_type or action_type in (
            "ParallelRetryAction",
            "KillAndRespawnAction",
            "NotifyJeffreyAction",
        ):
            # Try to extract error_class from details
            details = entry.get("details", {})
            error_class = details.get("error_class") or details.get("error_fingerprint") or ""

            escalations.append(EscalationRecord(
                timestamp=ts_str,
                session_id=entry.get("session_id", "unknown"),
                action_type=action_type,
                success=entry.get("success", False),
                error_class=error_class,
                reason=entry.get("reason"),
                details=details,
            ))

    return escalations


def count_by_error_class(
    escalations: list[EscalationRecord],
) -> dict[str, EscalationSummary]:
    """Count escalations grouped by error_class."""
    by_class: dict[str, list[EscalationRecord]] = defaultdict(list)

    for esc in escalations:
        # Use "unknown" if no error_class
        key = esc.error_class or "unknown"
        by_class[key].append(esc)

    summaries = {}
    for error_class, records in by_class.items():
        timestamps = sorted([r.timestamp for r in records])
        sessions = sorted(set(r.session_id for r in records))
        reasons = sorted(set(r.reason for r in records[-5:] if r.reason))  # Last 5 unique reasons

        summaries[error_class] = EscalationSummary(
            error_class=error_class,
            count=len(records),
            first_seen=timestamps[0],
            last_seen=timestamps[-1],
            sessions=sessions,
            recent_reasons=reasons,
        )

    return summaries


def detect_anomalies(
    summaries: dict[str, EscalationSummary],
    threshold: int = ESCALATION_THRESHOLD,
) -> list[EscalationSummary]:
    """Return error_classes with escalations >= threshold."""
    return [s for s in summaries.values() if s.count >= threshold]


def format_anomaly_message(anomalies: list[EscalationSummary]) -> str:
    """Format anomalies for Slack DM."""
    lines = [
        ":rotating_light: *Escalation Anomaly Detected*\n",
        f"Found {len(anomalies)} error class(es) with >= {ESCALATION_THRESHOLD} escalations in last 7 days:\n",
    ]

    for a in anomalies:
        lines.append(f"• *{a.error_class}* — {a.count} escalations")
        lines.append(f"  First: {a.first_seen[:19]}")
        lines.append(f"  Last: {a.last_seen[:19]}")
        lines.append(f"  Sessions: {', '.join(a.sessions[:3])}")
        if a.recent_reasons:
            lines.append(f"  Recent reasons: {a.recent_reasons[0][:80]}")
        lines.append("")

    return "\n".join(lines)


def send_anomaly_notification(
    anomalies: list[EscalationSummary],
    dry_run: bool = False,
) -> bool:
    """Send Slack DM with anomaly summary. Returns True if sent successfully."""
    if not anomalies:
        return True

    message = format_anomaly_message(anomalies)

    if dry_run:
        print("Dry run - would send:")
        print(message)
        return True

    # Import and use openclaw_notifier - send custom message directly
    from orchestration.openclaw_notifier import notify_slack_outbox_alert

    # Build payload with message field that notify_slack_outbox_alert will use
    payload = {
        "message": message,  # Human-readable anomaly summary
        "pending_count": 0,  # Required field but not used when message is present
    }

    # Use notify_slack_outbox_alert which posts to DM channel
    return notify_slack_outbox_alert(payload)


def run_anomaly_detection(
    action_log_path: str | None = None,
    dry_run: bool = False,
) -> list[EscalationSummary]:
    """Main entry point for anomaly detection.

    Args:
        action_log_path: Optional path to action_log.jsonl
        dry_run: If True, print message instead of sending Slack DM

    Returns:
        List of detected anomalies (error_classes with >= threshold escalations)
    """
    if action_log_path is None:
        action_log_path = str(
            Path(DEFAULT_STATE_DIR).expanduser() / DEFAULT_ACTION_LOG
        )

    # Read and process action log
    entries = read_action_log(action_log_path)
    escalations = filter_escalations(entries, days=7)

    # Group by error_class and detect anomalies
    summaries = count_by_error_class(escalations)
    anomalies = detect_anomalies(summaries)

    # Send notification if anomalies found
    send_anomaly_notification(anomalies, dry_run=dry_run)

    return anomalies


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Escalation anomaly detector")
    parser.add_argument(
        "--log-path",
        default=None,
        help="Path to action_log.jsonl",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print message instead of sending Slack DM",
    )
    args = parser.parse_args()

    anomalies = run_anomaly_detection(
        action_log_path=args.log_path,
        dry_run=args.dry_run,
    )

    if anomalies:
        print(f"Detected {len(anomalies)} anomaly(ies)")
    else:
        print("No anomalies detected")
