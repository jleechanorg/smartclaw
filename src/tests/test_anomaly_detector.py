"""Tests for anomaly_detector module."""

from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from orchestration.anomaly_detector import (
    EscalationRecord,
    EscalationSummary,
    read_action_log,
    filter_escalations,
    count_by_error_class,
    detect_anomalies,
    format_anomaly_message,
    run_anomaly_detection,
    ESCALATION_THRESHOLD,
)


# Freeze datetime to 2026-03-16 for deterministic tests
FIXED_TIME = datetime(2026, 3, 16, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def mock_datetime():
    """Freeze datetime.now() to a fixed date for all tests."""
    with patch("orchestration.anomaly_detector.datetime") as mock_dt:
        mock_dt.now.return_value = FIXED_TIME
        mock_dt.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)
        yield mock_dt


# Test fixtures
SAMPLE_ENTRIES = [
    {
        "timestamp": "2026-03-10T10:00:00.000000+00:00",
        "action_type": "ParallelRetryAction",
        "session_id": "session-1",
        "success": True,
        "reason": "CI failed on import",
        "details": {"error_class": "ci-failed:import-error"},
    },
    {
        "timestamp": "2026-03-12T14:00:00.000000+00:00",
        "action_type": "ParallelRetryAction",
        "session_id": "session-2",
        "success": False,
        "reason": "Build failed",
        "details": {"error_class": "ci-failed:import-error"},
    },
    {
        "timestamp": "2026-03-15T08:00:00.000000+00:00",
        "action_type": "ParallelRetryAction",
        "session_id": "session-3",
        "success": True,
        "reason": "Test timeout",
        "details": {"error_class": "ci-failed:import-error"},
    },
    {
        "timestamp": "2026-03-15T09:00:00.000000+00:00",
        "action_type": "KillRespawnAction",
        "session_id": "session-4",
        "success": True,
        "reason": "Process stuck",
        "details": {"error_class": "ci-failed:timeout"},
    },
    {
        "timestamp": "2026-03-15T10:00:00.000000+00:00",
        "action_type": "WaitForCIAction",
        "session_id": "session-5",
        "success": True,
        "reason": "CI pending",
        "details": {},
    },
    {
        "timestamp": "2026-03-16T01:00:00.000000+00:00",
        "action_type": "ParallelRetryAction",
        "session_id": "session-6",
        "success": True,
        "reason": "Type error",
        "details": {"error_class": "ci-failed:type-error"},
    },
]


class TestReadActionLog:
    """Tests for read_action_log function."""

    def test_read_empty_file(self, tmp_path):
        log_file = tmp_path / "empty.jsonl"
        log_file.write_text("")
        assert read_action_log(log_file) == []

    def test_read_malformed_lines_skipped(self, tmp_path):
        log_file = tmp_path / "malformed.jsonl"
        log_file.write_text('{"valid": true}\ninvalid json\n{"also": "valid"}')
        result = read_action_log(log_file)
        assert len(result) == 2

    def test_read_file_not_found(self):
        assert read_action_log("/nonexistent/path.jsonl") == []


class TestFilterEscalations:
    """Tests for filter_escalations function."""

    def test_filters_by_action_type(self):
        result = filter_escalations(SAMPLE_ENTRIES, days=7)
        action_types = [r.action_type for r in result]
        # WaitForCIAction should be excluded
        assert "WaitForCIAction" not in action_types

    def test_filters_by_time_window(self):
        # Only entries from last 7 days
        result = filter_escalations(SAMPLE_ENTRIES, days=7)
        assert len(result) >= 3  # Has ParallelRetryAction and KillRespawnAction

    def test_extracts_error_class(self):
        result = filter_escalations(SAMPLE_ENTRIES, days=7)
        with_error_class = [r for r in result if r.error_class]
        assert len(with_error_class) >= 3


class TestCountByErrorClass:
    """Tests for count_by_error_class function."""

    def test_groups_by_error_class(self):
        escalations = [
            EscalationRecord(
                timestamp="2026-03-10T10:00:00+00:00",
                session_id="s1",
                action_type="ParallelRetryAction",
                success=True,
                error_class="ci-failed:import-error",
            ),
            EscalationRecord(
                timestamp="2026-03-12T10:00:00+00:00",
                session_id="s2",
                action_type="ParallelRetryAction",
                success=False,
                error_class="ci-failed:import-error",
            ),
            EscalationRecord(
                timestamp="2026-03-15T10:00:00+00:00",
                session_id="s3",
                action_type="ParallelRetryAction",
                success=True,
                error_class="ci-failed:timeout",
            ),
        ]

        result = count_by_error_class(escalations)

        assert "ci-failed:import-error" in result
        assert result["ci-failed:import-error"].count == 2
        assert result["ci-failed:timeout"].count == 1

    def test_handles_unknown_error_class(self):
        escalations = [
            EscalationRecord(
                timestamp="2026-03-10T10:00:00+00:00",
                session_id="s1",
                action_type="ParallelRetryAction",
                success=True,
                error_class=None,
            ),
        ]

        result = count_by_error_class(escalations)
        assert "unknown" in result


class TestDetectAnomalies:
    """Tests for detect_anomalies function."""

    def test_detects_threshold_exceeded(self):
        summaries = {
            "ci-failed:import-error": EscalationSummary(
                error_class="ci-failed:import-error",
                count=3,
                first_seen="2026-03-10T10:00:00+00:00",
                last_seen="2026-03-15T10:00:00+00:00",
                sessions=["s1", "s2", "s3"],
                recent_reasons=["import error"],
            ),
            "ci-failed:timeout": EscalationSummary(
                error_class="ci-failed:timeout",
                count=1,
                first_seen="2026-03-15T10:00:00+00:00",
                last_seen="2026-03-15T10:00:00+00:00",
                sessions=["s4"],
                recent_reasons=[],
            ),
        }

        result = detect_anomalies(summaries)
        assert len(result) == 1
        assert result[0].error_class == "ci-failed:import-error"

    def test_no_anomalies_below_threshold(self):
        summaries = {
            "ci-failed:timeout": EscalationSummary(
                error_class="ci-failed:timeout",
                count=1,
                first_seen="2026-03-15T10:00:00+00:00",
                last_seen="2026-03-15T10:00:00+00:00",
                sessions=["s1"],
                recent_reasons=[],
            ),
        }

        result = detect_anomalies(summaries)
        assert len(result) == 0


class TestFormatAnomalyMessage:
    """Tests for format_anomaly_message function."""

    def test_formats_correctly(self):
        anomalies = [
            EscalationSummary(
                error_class="ci-failed:import-error",
                count=3,
                first_seen="2026-03-10T10:00:00+00:00",
                last_seen="2026-03-15T10:00:00+00:00",
                sessions=["s1", "s2", "s3"],
                recent_reasons=["import error", "module not found"],
            ),
        ]

        message = format_anomaly_message(anomalies)

        assert "ci-failed:import-error" in message
        assert "3 escalations" in message
        assert "First:" in message
        assert "Last:" in message


class TestRunAnomalyDetection:
    """Tests for run_anomaly_detection function."""

    @patch("orchestration.anomaly_detector.read_action_log")
    @patch("orchestration.anomaly_detector.send_anomaly_notification")
    def test_returns_anomalies(self, mock_notify, mock_read):
        mock_read.return_value = SAMPLE_ENTRIES

        result = run_anomaly_detection(dry_run=True)

        # Should detect ci-failed:import-error with 3 occurrences
        assert len(result) >= 1
        mock_notify.assert_called_once()

    @patch("orchestration.anomaly_detector.read_action_log")
    @patch("orchestration.anomaly_detector.send_anomaly_notification")
    def test_no_anomalies_empty_log(self, mock_notify, mock_read):
        mock_read.return_value = []

        result = run_anomaly_detection(dry_run=True)

        assert len(result) == 0


class TestIntegration:
    """Integration tests with sample data."""

    def test_full_pipeline_with_threshold(self):
        """Test the full detection pipeline with exactly threshold count."""
        entries = [
            {
                "timestamp": "2026-03-15T10:00:00.000000+00:00",
                "action_type": "ParallelRetryAction",
                "session_id": f"session-{i}",
                "success": True,
                "reason": "error",
                "details": {"error_class": "ci-failed:same-error"},
            }
            for i in range(ESCALATION_THRESHOLD)
        ]

        escalations = filter_escalations(entries, days=7)
        summaries = count_by_error_class(escalations)
        anomalies = detect_anomalies(summaries)

        # Exactly threshold should trigger
        assert len(anomalies) == 1
        assert anomalies[0].count == ESCALATION_THRESHOLD
