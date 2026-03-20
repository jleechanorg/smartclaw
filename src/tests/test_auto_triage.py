"""Tests for auto_triage module."""

import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock

from orchestration.auto_triage import (
    RepeatedEscalation,
    check_and_notify_repeated,
    notify_repeated_escalation,
    scan_repeated_escalations,
)


def test_scan_repeated_escalations_empty_file(tmp_path) -> None:
    """Empty outcomes file returns empty list."""
    outcomes_file = tmp_path / "outcomes.jsonl"
    outcomes_file.write_text("")
    
    result = scan_repeated_escalations(
        window_days=7,
        threshold=2,
        outcomes_path=str(outcomes_file),
    )
    
    assert result == []


def test_scan_repeated_escalations_no_escalations(tmp_path) -> None:
    """Non-escalation outcomes are ignored."""
    outcomes_file = tmp_path / "outcomes.jsonl"
    outcomes_file.write_text('''{"error_class": "test-error", "result": "merged", "timestamp": "2026-03-16T10:00:00Z"}
{"error_class": "test-error", "result": "success", "timestamp": "2026-03-16T11:00:00Z"}
''')
    
    result = scan_repeated_escalations(
        window_days=7,
        threshold=2,
        outcomes_path=str(outcomes_file),
    )
    
    assert result == []


def test_scan_repeated_escalations_finds_escalations(tmp_path) -> None:
    """Escalation results are detected."""
    outcomes_file = tmp_path / "outcomes.jsonl"
    outcomes_file.write_text('''{"error_class": "test-error", "result": "escalated", "timestamp": "2026-03-16T10:00:00Z", "pr_url": "https://github.com/jleechanorg/test/pull/1"}
{"error_class": "test-error", "result": "escalated", "timestamp": "2026-03-16T11:00:00Z", "pr_url": "https://github.com/jleechanorg/test/pull/2"}
''')
    
    result = scan_repeated_escalations(
        window_days=7,
        threshold=2,
        outcomes_path=str(outcomes_file),
    )
    
    assert len(result) == 1
    assert result[0].error_class == "test-error"
    assert result[0].escalation_count == 2


def test_scan_repeated_escalations_below_threshold(tmp_path) -> None:
    """Below threshold is not reported."""
    outcomes_file = tmp_path / "outcomes.jsonl"
    outcomes_file.write_text('''{"error_class": "test-error", "result": "escalated", "timestamp": "2026-03-16T10:00:00Z"}
''')
    
    result = scan_repeated_escalations(
        window_days=7,
        threshold=2,
        outcomes_path=str(outcomes_file),
    )
    
    assert result == []


def test_scan_repeated_escalations_ignores_old_escalations(tmp_path) -> None:
    """Old escalations outside window are ignored."""
    outcomes_file = tmp_path / "outcomes.jsonl"
    outcomes_file.write_text('''{"error_class": "test-error", "result": "escalated", "timestamp": "2026-01-01T10:00:00Z"}
{"error_class": "test-error", "result": "escalated", "timestamp": "2026-01-02T10:00:00Z"}
''')
    
    result = scan_repeated_escalations(
        window_days=7,
        threshold=2,
        outcomes_path=str(outcomes_file),
    )
    
    assert result == []


def test_scan_repeated_escalations_action_escalation(tmp_path) -> None:
    """NotifyJeffreyAction also triggers escalation detection."""
    outcomes_file = tmp_path / "outcomes.jsonl"
    outcomes_file.write_text('''{"error_class": "ci-fail", "action": "NotifyJeffreyAction", "timestamp": "2026-03-16T10:00:00Z", "pr_url": "https://github.com/jleechanorg/test/pull/1"}
{"error_class": "ci-fail", "action": "NotifyJeffreyAction", "timestamp": "2026-03-16T11:00:00Z", "pr_url": "https://github.com/jleechanorg/test/pull/2"}
''')
    
    result = scan_repeated_escalations(
        window_days=7,
        threshold=2,
        outcomes_path=str(outcomes_file),
    )
    
    assert len(result) == 1
    assert result[0].error_class == "ci-fail"


def test_notify_repeated_escalation_with_notifier(tmp_path) -> None:
    """Notification sends DM via notifier."""
    mock_notifier = MagicMock()
    mock_notifier.send_dm.return_value = True
    
    escalation = RepeatedEscalation(
        error_class="test-error",
        escalation_count=3,
        prs=["https://github.com/jleechanorg/test/pull/1", "https://github.com/jleechanorg/test/pull/2"],
        first_escalation="2026-03-16T10:00:00Z",
        last_escalation="2026-03-16T12:00:00Z",
    )
    
    result = notify_repeated_escalation(escalation, notifier=mock_notifier)
    
    assert result is True
    mock_notifier.send_dm.assert_called_once()


def test_notify_repeated_escalation_no_notifier(tmp_path) -> None:
    """Without notifier, logs but doesn't send."""
    escalation = RepeatedEscalation(
        error_class="test-error",
        escalation_count=3,
        prs=["https://github.com/jleechanorg/test/pull/1"],
        first_escalation="2026-03-16T10:00:00Z",
        last_escalation="2026-03-16T12:00:00Z",
    )
    
    result = notify_repeated_escalation(escalation, notifier=None)
    
    assert result is False


def test_check_and_notify_repeated_calls_notifier(tmp_path) -> None:
    """check_and_notify_repeated triggers notification."""
    outcomes_file = tmp_path / "outcomes.jsonl"
    outcomes_file.write_text('''{"error_class": "ci-fail", "result": "escalated", "timestamp": "2026-03-16T10:00:00Z", "pr_url": "https://github.com/jleechanorg/test/pull/1"}
{"error_class": "ci-fail", "result": "escalated", "timestamp": "2026-03-16T11:00:00Z", "pr_url": "https://github.com/jleechanorg/test/pull/2"}
''')
    
    mock_notifier = MagicMock()
    mock_notifier.send_dm.return_value = True
    
    check_and_notify_repeated(
        error_class="ci-fail",
        pr_url="https://github.com/jleechanorg/test/pull/2",
        notifier=mock_notifier,
        outcomes_path=str(outcomes_file),
    )
    
    mock_notifier.send_dm.assert_called_once()
