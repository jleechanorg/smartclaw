from __future__ import annotations

from datetime import datetime, timezone

from orchestration.pr_lifecycle import (
    route_catch_up,
    route_event,
    summarize_status,
)


def _opened_event(*, pr_number: int = 101, head_sha: str = "sha-open-001") -> dict:
    return {
        "trigger_source": "event",
        "trigger_type": "pull_request.opened",
        "repository": "jleechanorg/worldarchitect.ai",
        "pr_number": pr_number,
        "head_sha": head_sha,
        "draft": False,
    }


def _synchronize_event(*, pr_number: int = 101, head_sha: str = "sha-sync-001") -> dict:
    return {
        "trigger_source": "event",
        "trigger_type": "pull_request.synchronize",
        "repository": "jleechanorg/worldarchitect.ai",
        "pr_number": pr_number,
        "head_sha": head_sha,
        "draft": False,
    }


def test_pr_opened_routes_to_comment_validation() -> None:
    result = route_event(_opened_event(), previous_runs=[])

    assert result["workflow_lane"] == "comment-validation"
    assert result["trigger_source"] == "event"
    assert result["run_outcome"] == "executed"
    assert result["idempotency_key"] == "101|sha-open-001|comment-validation"


def test_pr_opened_pr42_routes_correctly() -> None:
    """Test PR #42 with SHA deadbeef1234 routes to comment-validation."""
    event = _opened_event(pr_number=42, head_sha="deadbeef1234")
    result = route_event(event, previous_runs=[])

    assert result["workflow_lane"] == "comment-validation"
    assert result["pr_number"] == 42
    assert result["head_sha"] == "deadbeef1234"
    assert result["run_outcome"] == "executed"
    assert result["idempotency_key"] == "42|deadbeef1234|comment-validation"


def test_duplicate_synchronize_on_same_sha_is_suppressed() -> None:
    previous_runs = [{
        "pr_number": 101,
        "head_sha": "sha-sync-001",
        "workflow_lane": "comment-validation",
        "trigger_source": "event",
        "run_outcome": "executed",
        "idempotency_key": "101|sha-sync-001|comment-validation",
    }]

    result = route_event(_synchronize_event(), previous_runs=previous_runs)

    assert result["run_outcome"] == "duplicate_suppressed"
    assert result["skip_reason"] == "duplicate_event_same_head_sha"


def test_unmapped_event_fails_closed() -> None:
    result = route_event({
        "trigger_source": "event",
        "trigger_type": "pull_request.unknown_transition",
        "repository": "jleechanorg/worldarchitect.ai",
        "pr_number": 101,
        "head_sha": "sha-unknown-001",
    })

    assert result["run_outcome"] == "skipped_ineligible"
    assert result["skip_reason"] == "unmapped_trigger"

def test_check_run_failure_routes_to_fixpr() -> None:
    result = route_event(
        {
            "trigger_source": "event",
            "trigger_type": "check_run.completed.failure",
            "repository": "jleechanorg/worldarchitect.ai",
            "pr_number": 101,
            "head_sha": "sha-checkrun-001",
        },
        previous_runs=[],
    )

    assert result["workflow_lane"] == "fixpr"
    assert result["run_outcome"] == "executed"
    assert result["idempotency_key"] == "101|sha-checkrun-001|fixpr"


def test_catch_up_recovers_failed_run() -> None:
    result = route_catch_up(
        {
            "workflow_lane": "fixpr",
            "pr_number": 70,
            "head_sha": "deadbeef",
            "trigger_source": "catch_up",
        },
        previous_runs=[{
            "idempotency_key": "70|deadbeef|fixpr",
            "run_outcome": "failed",
        }],
    )

    assert result["run_outcome"] == "stale_recovered"
    assert result["idempotency_key"] == "70|deadbeef|fixpr"


def test_catch_up_skips_when_recent_success_exists() -> None:
    result = route_catch_up(
        {
            "workflow_lane": "fix-comment",
            "pr_number": 70,
            "head_sha": "deadbeef",
            "trigger_source": "catch_up",
        },
        previous_runs=[{
            "idempotency_key": "70|deadbeef|fix-comment",
            "run_outcome": "executed",
            "completed_at": "2026-03-08T23:00:00Z",
        }],
        now=datetime(2026, 3, 8, 23, 10, 0, tzinfo=timezone.utc),
        freshness_window_seconds=3600,
    )

    assert result["run_outcome"] == "skipped_ineligible"
    assert result["skip_reason"] == "fresh_event_run_exists"


def test_check_suite_completed_without_pr_is_skipped() -> None:
    """check_suite/check_run on default branch (no PR) should be skipped."""
    result = route_event(
        {
            "trigger_source": "event",
            "trigger_type": "check_suite.completed.success",
            "repository": "jleechanorg/jleechanclaw",
            "pr_number": None,
            "head_sha": "abc123def456",
        },
        previous_runs=[],
    )

    assert result["run_outcome"] == "skipped_ineligible"
    assert result["skip_reason"] == "no_pr_associated"
    assert result["pr_number"] is None


def test_check_run_completed_without_pr_is_skipped() -> None:
    """check_run completed on default branch (no PR) should be skipped."""
    result = route_event(
        {
            "trigger_source": "event",
            "trigger_type": "check_run.completed.failure",
            "repository": "jleechanorg/jleechanclaw",
            "pr_number": None,
            "head_sha": "deadbeef1234",
        },
        previous_runs=[],
    )

    assert result["run_outcome"] == "skipped_ineligible"
    assert result["skip_reason"] == "no_pr_associated"
    assert result["pr_number"] is None


def test_summarize_status_marks_real_time_and_duplicate_rows() -> None:
    rows = summarize_status([
        {
            "pr_number": 70,
            "workflow_lane": "fixpr",
            "trigger_source": "event",
            "run_outcome": "executed",
        },
        {
            "pr_number": 70,
            "workflow_lane": "fixpr",
            "trigger_source": "event",
            "run_outcome": "duplicate_suppressed",
            "skip_reason": "duplicate_event_same_head_sha",
        },
    ])

    assert rows == [
        {
            "pr_number": 70,
            "workflow_lane": "fixpr",
            "trigger_source": "event",
            "status": "handled_in_real_time",
            "skip_reason": None,
        },
        {
            "pr_number": 70,
            "workflow_lane": "fixpr",
            "trigger_source": "event",
            "status": "duplicate_suppressed",
            "skip_reason": "duplicate_event_same_head_sha",
        },
    ]
