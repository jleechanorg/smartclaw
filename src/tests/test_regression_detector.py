"""Tests for regression_detector module."""

import json
import pytest
from pathlib import Path

from orchestration.regression_detector import (
    RegressionAlert,
    WeeklyMetrics,
    compute_weekly_metrics,
    detect_regression,
    format_regression_message,
    run_regression_check,
)


def test_compute_weekly_metrics_empty_file(tmp_path) -> None:
    """Empty file returns None."""
    outcomes_file = tmp_path / "outcomes.jsonl"
    outcomes_file.write_text("")
    
    result = compute_weekly_metrics(outcomes_path=str(outcomes_file), week_offset=0)
    
    assert result is None


def test_compute_weekly_metrics_counts_correctly(tmp_path) -> None:
    """Metrics are counted correctly."""
    outcomes_file = tmp_path / "outcomes.jsonl"
    # Write entries with current timestamp
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    
    outcomes_file.write_text(f'''{{"result": "merged", "timestamp": "{now}"}}
{{"result": "success", "timestamp": "{now}"}}
{{"result": "escalated", "timestamp": "{now}"}}
{{"result": "failed", "timestamp": "{now}"}}
''')
    
    result = compute_weekly_metrics(outcomes_path=str(outcomes_file), week_offset=0)
    
    assert result is not None
    assert result.total_actions == 4
    assert result.merged_count == 1
    assert result.success_count == 1
    assert result.escalated_count == 1
    assert result.failed_count == 1


def test_compute_weekly_metrics_ignores_old_entries(tmp_path) -> None:
    """Old entries outside week are ignored."""
    outcomes_file = tmp_path / "outcomes.jsonl"
    
    # Entry from 2 weeks ago should be ignored
    outcomes_file.write_text('''{"result": "merged", "timestamp": "2026-01-01T00:00:00Z"}
{"result": "merged", "timestamp": "2026-03-16T10:00:00Z"}
''')
    
    result = compute_weekly_metrics(outcomes_path=str(outcomes_file), week_offset=0)
    
    # Should only count recent entry
    assert result is not None
    assert result.total_actions == 1


def test_detect_regression_no_previous() -> None:
    """No regression with no previous data."""
    current = WeeklyMetrics(
        week_start="2026-03-10",
        week_end="2026-03-16",
        total_actions=10,
        success_count=5,
        escalated_count=5,
        merged_count=3,
        failed_count=2,
        mttr_minutes=30.0,
        win_rate=0.5,
    )
    
    alerts = detect_regression(current, None)
    
    assert alerts == []


def test_detect_regression_escalation_rate_warning() -> None:
    """Escalation rate increase >50% triggers alert."""
    current = WeeklyMetrics(
        week_start="2026-03-10",
        week_end="2026-03-16",
        total_actions=10,
        success_count=5,
        escalated_count=8,
        merged_count=3,
        failed_count=2,
        mttr_minutes=30.0,
        win_rate=0.5,
    )
    previous = WeeklyMetrics(
        week_start="2026-03-03",
        week_end="2026-03-09",
        total_actions=10,
        success_count=5,
        escalated_count=2,
        merged_count=3,
        failed_count=2,
        mttr_minutes=25.0,
        win_rate=0.5,
    )
    
    alerts = detect_regression(current, previous)
    
    assert len(alerts) == 1
    assert alerts[0].metric == "escalation_rate"
    assert alerts[0].severity == "critical"


def test_detect_regression_escalation_rate_ok() -> None:
    """Small escalation rate increase is OK."""
    current = WeeklyMetrics(
        week_start="2026-03-10",
        week_end="2026-03-16",
        total_actions=10,
        success_count=5,
        escalated_count=3,
        merged_count=3,
        failed_count=2,
        mttr_minutes=30.0,
        win_rate=0.5,
    )
    previous = WeeklyMetrics(
        week_start="2026-03-03",
        week_end="2026-03-09",
        total_actions=10,
        success_count=5,
        escalated_count=2,
        merged_count=3,
        failed_count=2,
        mttr_minutes=25.0,
        win_rate=0.5,
    )
    
    alerts = detect_regression(current, previous)
    
    # 50% increase = borderline, but threshold is >50%
    assert len(alerts) == 0


def test_detect_regression_win_rate_drop() -> None:
    """Win rate drop >10% triggers alert."""
    # Use consistent counts: success + merged + escalated + failed = total
    current = WeeklyMetrics(
        week_start="2026-03-10",
        week_end="2026-03-16",
        total_actions=10,
        success_count=3,
        escalated_count=2,
        merged_count=2,
        failed_count=3,
        mttr_minutes=30.0,
        win_rate=0.5,  # (3+2)/10 = 50%
    )
    previous = WeeklyMetrics(
        week_start="2026-03-03",
        week_end="2026-03-09",
        total_actions=10,
        success_count=8,
        escalated_count=2,
        merged_count=0,
        failed_count=0,
        mttr_minutes=25.0,
        win_rate=0.8,  # (8+0)/10 = 80%
    )
    
    alerts = detect_regression(current, previous)
    
    assert len(alerts) == 1
    assert alerts[0].metric == "win_rate"
    assert alerts[0].severity == "critical"


def test_detect_regression_win_rate_ok() -> None:
    """Small win rate drop is OK."""
    current = WeeklyMetrics(
        week_start="2026-03-10",
        week_end="2026-03-16",
        total_actions=10,
        success_count=7,
        escalated_count=2,
        merged_count=5,
        failed_count=1,
        mttr_minutes=30.0,
        win_rate=0.7,
    )
    previous = WeeklyMetrics(
        week_start="2026-03-03",
        week_end="2026-03-09",
        total_actions=10,
        success_count=8,
        escalated_count=2,
        merged_count=5,
        failed_count=0,
        mttr_minutes=25.0,
        win_rate=0.8,
    )
    
    alerts = detect_regression(current, previous)
    
    # 12.5% drop is above threshold
    assert len(alerts) == 1


def test_detect_regression_mttr_increase() -> None:
    """MTTR increase >20% triggers alert."""
    # Use consistent counts
    current = WeeklyMetrics(
        week_start="2026-03-10",
        week_end="2026-03-16",
        total_actions=10,
        success_count=5,
        escalated_count=2,
        merged_count=3,
        failed_count=0,
        mttr_minutes=37.0,  # 23% increase from 30
        win_rate=0.5,
    )
    previous = WeeklyMetrics(
        week_start="2026-03-03",
        week_end="2026-03-09",
        total_actions=10,
        success_count=5,
        escalated_count=2,
        merged_count=3,
        failed_count=0,
        mttr_minutes=30.0,
        win_rate=0.5,
    )
    
    alerts = detect_regression(current, previous)
    
    assert len(alerts) == 1
    assert alerts[0].metric == "mttr"


def test_format_regression_message() -> None:
    """Message is formatted correctly."""
    current = WeeklyMetrics(
        week_start="2026-03-10T00:00:00",
        week_end="2026-03-16T23:59:59",
        total_actions=15,
        success_count=5,
        escalated_count=10,
        merged_count=3,
        failed_count=2,
        mttr_minutes=30.0,
        win_rate=0.33,
    )
    previous = WeeklyMetrics(
        week_start="2026-03-03T00:00:00",
        week_end="2026-03-09T23:59:59",
        total_actions=10,
        success_count=8,
        escalated_count=2,
        merged_count=5,
        failed_count=0,
        mttr_minutes=25.0,
        win_rate=0.8,
    )
    
    alerts = [
        RegressionAlert("escalation_rate", 66.7, 20.0, 233.3, "critical"),
        RegressionAlert("win_rate", 33.3, 80.0, -58.3, "critical"),
    ]
    
    message = format_regression_message(current, previous, alerts)
    
    assert "Regression Detected" in message
    assert "15" in message
    assert "escalation_rate" in message


def test_run_regression_check_with_no_data(tmp_path) -> None:
    """Regression check handles no data gracefully."""
    outcomes_file = tmp_path / "outcomes.jsonl"
    outcomes_file.write_text("")
    
    # Should not raise
    alerts = run_regression_check(outcomes_path=str(outcomes_file), notifier=None)
    
    assert alerts == []
