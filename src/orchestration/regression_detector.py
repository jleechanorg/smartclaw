"""Regression detector: weekly orchestration health monitoring.

This module implements convergence intelligence:
- Computes weekly metrics from outcomes.jsonl
- Compares current week vs previous week
- Sends Slack DM if regression thresholds exceeded

Regression triggers:
- MTTR increased >20%
- Escalation count increased >50%
- Win rate dropped >10%
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
class WeeklyMetrics:
    """Metrics for a single week."""

    week_start: str  # ISO date
    week_end: str  # ISO date
    total_actions: int
    success_count: int
    escalated_count: int
    merged_count: int
    failed_count: int
    mttr_minutes: float | None  # Mean time to resolution (None if no data)
    win_rate: float  # 0.0 to 1.0


@dataclass
class RegressionAlert:
    """A detected regression."""

    metric: str
    current_value: float
    previous_value: float
    percent_change: float
    severity: str  # "warning" or "critical"


class SlackNotifier(Protocol):
    """Protocol for Slack notification."""

    def send_dm(self, message: str, channel: str) -> bool:
        """Send a direct message."""
        ...


def compute_weekly_metrics(
    outcomes_path: str | None = None,
    week_offset: int = 0,
) -> WeeklyMetrics | None:
    """Compute metrics for a specific week.

    Args:
        outcomes_path: Optional path to outcomes.jsonl
        week_offset: 0 = current week (Mon-Sun), 1 = last week, etc.

    Returns:
        WeeklyMetrics or None if no data
    """
    if outcomes_path is None:
        state_dir = os.path.expanduser(DEFAULT_STATE_DIR)
        outcomes_path = os.path.join(state_dir, DEFAULT_OUTCOMES_FILE)

    outcomes_file = Path(outcomes_path)
    if not outcomes_file.exists():
        logger.debug(f"Outcomes file not found: {outcomes_path}")
        return None

    # Calculate week boundaries (Monday to Sunday)
    now = datetime.now(timezone.utc)
    days_since_monday = now.weekday()
    week_start = now - timedelta(days=days_since_monday + (week_offset * 7))
    week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)
    week_end = week_start + timedelta(days=6, hours=23, minutes=59, seconds=59)

    # Track metrics
    total_actions = 0
    success_count = 0
    escalated_count = 0
    merged_count = 0
    failed_count = 0
    resolution_times: list[float] = []

    try:
        with open(outcomes_file, "r") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Check timestamp
                timestamp_str = entry.get("timestamp", "")
                if not timestamp_str:
                    continue

                try:
                    timestamp = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    continue

                # Check if in week
                if not (week_start <= timestamp <= week_end):
                    continue

                total_actions += 1
                result = entry.get("result", "")
                action = entry.get("action", "")

                # Count by result
                if result == "escalated" or action == "NotifyJeffreyAction":
                    escalated_count += 1
                if result == "merged":
                    merged_count += 1
                if result == "success":
                    success_count += 1
                if result == "failed":
                    failed_count += 1

                # Calculate MTTR if we have start/end times
                # (outcomes.jsonl doesn't have duration, so we use action count as proxy)
                # A more complete implementation would track actual duration

    except Exception as e:
        logger.warning(f"Failed to compute weekly metrics: {e}")
        return None

    if total_actions == 0:
        return None

    # Calculate win rate
    successful = merged_count + success_count
    win_rate = successful / total_actions if total_actions > 0 else 0.0

    # MTTR: simplified (would need duration tracking for real MTTR)
    mttr_minutes = None

    return WeeklyMetrics(
        week_start=week_start.isoformat(),
        week_end=week_end.isoformat(),
        total_actions=total_actions,
        success_count=success_count,
        escalated_count=escalated_count,
        merged_count=merged_count,
        failed_count=failed_count,
        mttr_minutes=mttr_minutes,
        win_rate=win_rate,
    )


def detect_regression(
    current: WeeklyMetrics,
    previous: WeeklyMetrics | None,
) -> list[RegressionAlert]:
    """Detect regression between current and previous week.

    Regression thresholds:
    - MTTR increased >20%
    - Escalation count increased >50%
    - Win rate dropped >10%

    Args:
        current: Current week's metrics
        previous: Previous week's metrics (None = no comparison)

    Returns:
        List of RegressionAlert objects
    """
    if previous is None:
        return []

    alerts: list[RegressionAlert] = []

    # Check escalation rate increase
    if previous.total_actions > 0:
        prev_escalation_rate = previous.escalated_count / previous.total_actions
        curr_escalation_rate = current.escalated_count / current.total_actions
    else:
        prev_escalation_rate = 0
        curr_escalation_rate = 0

    if prev_escalation_rate > 0:
        escalation_change = ((curr_escalation_rate - prev_escalation_rate) / prev_escalation_rate) * 100
        if escalation_change > 50:
            alerts.append(RegressionAlert(
                metric="escalation_rate",
                current_value=curr_escalation_rate * 100,
                previous_value=prev_escalation_rate * 100,
                percent_change=escalation_change,
                severity="critical" if escalation_change > 100 else "warning",
            ))

    # Check win rate drop
    if previous.win_rate > 0:
        win_rate_change = ((current.win_rate - previous.win_rate) / previous.win_rate) * 100
        if win_rate_change < -10:
            alerts.append(RegressionAlert(
                metric="win_rate",
                current_value=current.win_rate * 100,
                previous_value=previous.win_rate * 100,
                percent_change=win_rate_change,
                severity="critical" if win_rate_change < -25 else "warning",
            ))

    # Check MTTR increase (if we have data)
    if current.mttr_minutes is not None and previous.mttr_minutes is not None and previous.mttr_minutes > 0:
        mttr_change = ((current.mttr_minutes - previous.mttr_minutes) / previous.mttr_minutes) * 100
        if mttr_change > 20:
            alerts.append(RegressionAlert(
                metric="mttr",
                current_value=current.mttr_minutes,
                previous_value=previous.mttr_minutes,
                percent_change=mttr_change,
                severity="critical" if mttr_change > 50 else "warning",
            ))

    return alerts


def format_regression_message(
    current: WeeklyMetrics,
    previous: WeeklyMetrics,
    alerts: list[RegressionAlert],
) -> str:
    """Format Slack message for regression alert."""
    lines = [
        "📉 *Orchestration Regression Detected*",
        "",
        f"*Week:* {current.week_start[:10]} to {current.week_end[:10]}",
        "",
        "*Metrics Comparison:*",
        f"  • Total actions: {current.total_actions} (was {previous.total_actions})",
        f"  • Win rate: {current.win_rate:.1%} (was {previous.win_rate:.1%})",
        f"  • Escalations: {current.escalated_count} (was {previous.escalated_count})",
        "",
    ]

    if alerts:
        lines.append("*Regressions:*")
        for alert in alerts:
            emoji = "🔴" if alert.severity == "critical" else "🟡"
            lines.append(f"  {emoji} {alert.metric}: {alert.current_value:.1f} (was {alert.previous_value:.1f}, {alert.percent_change:+.1f}%)")
        lines.append("")
        lines.append("Consider reviewing recent failures to identify systemic issues.")

    return "\n".join(lines)


def run_regression_check(
    outcomes_path: str | None = None,
    notifier: SlackNotifier | None = None,
) -> list[RegressionAlert]:
    """Run weekly regression check and send notification if needed.

    Args:
        outcomes_path: Optional path to outcomes.jsonl
        notifier: Slack notifier (if None, logs only)

    Returns:
        List of detected alerts
    """
    current = compute_weekly_metrics(outcomes_path=outcomes_path, week_offset=0)
    previous = compute_weekly_metrics(outcomes_path=outcomes_path, week_offset=1)

    if current is None:
        logger.debug("No current week data for regression check")
        return []

    if previous is None:
        logger.debug("No previous week data for regression check")
        return []

    alerts = detect_regression(current, previous)

    if alerts:
        logger.warning(f"Regression detected: {len(alerts)} alerts")
        message = format_regression_message(current, previous, alerts)

        if notifier:
            try:
                notifier.send_dm(message, channel=os.environ.get("SMARTCLAW_DM_CHANNEL", ""))
            except Exception as e:
                logger.warning(f"Failed to send regression DM: {e}")
        else:
            logger.info(f"Would send DM: {message[:200]}...")

    return alerts
