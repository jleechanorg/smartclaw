"""Unit tests for webhook_queue: dedupe_key, normalize_event, RemediationQueue."""
from __future__ import annotations

from pathlib import Path

import pytest

from orchestration.webhook_queue import (
    QueueStatus,
    RemediationQueue,
    dedupe_key,
    normalize_event,
)


# ---------------------------------------------------------------------------
# dedupe_key tests
# ---------------------------------------------------------------------------

def test_dedupe_key_pull_request() -> None:
    payload = {
        "repository": {"full_name": "jleechanorg/worldarchitect.ai"},
        "pull_request": {
            "number": 42,
            "head": {"sha": "abc123"},
        },
        "action": "synchronize",
    }
    key = dedupe_key("pull_request", payload)
    assert key == "pr:jleechanorg/worldarchitect.ai:42:abc123:pull_request.synchronize"


def test_dedupe_key_check_suite() -> None:
    payload = {
        "repository": {"full_name": "jleechanorg/worldarchitect.ai"},
        "check_suite": {
            "id": 9999,
            "head_sha": "def456",
        },
        "action": "completed",
    }
    key = dedupe_key("check_suite", payload)
    assert key == "cs:jleechanorg/worldarchitect.ai:def456:9999"


def test_dedupe_key_fallback() -> None:
    # delivery_id comes from parameter, not payload field
    key = dedupe_key("push", {}, delivery_id="xyz-789")
    assert key == "misc:xyz-789"


def test_dedupe_key_fallback_no_delivery_id() -> None:
    key = dedupe_key("push", {})
    assert key.startswith("misc:")


def test_dedupe_key_fallback_unique_per_delivery() -> None:
    """Two unrecognised events with different delivery IDs must not collide."""
    payload: dict = {}
    key1 = dedupe_key("push", payload, delivery_id="del-aaa")
    key2 = dedupe_key("push", payload, delivery_id="del-bbb")
    assert key1 == "misc:del-aaa"
    assert key2 == "misc:del-bbb"
    assert key1 != key2


def test_dedupe_key_review_fallback_uses_delivery_id() -> None:
    """pull_request_review with missing review.id falls back to misc:{delivery_id}."""
    payload = {
        "action": "submitted",
        "repository": {"full_name": "org/repo"},
        "pull_request": {"number": 1, "head": {"sha": "abc"}},
        # review key absent → review_id="" → fallback branch
    }
    key1 = dedupe_key("pull_request_review", payload, delivery_id="hdr-111")
    key2 = dedupe_key("pull_request_review", payload, delivery_id="hdr-222")
    assert key1 == "misc:hdr-111"
    assert key2 == "misc:hdr-222"
    assert key1 != key2


# ---------------------------------------------------------------------------
# normalize_event tests — action filtering
# ---------------------------------------------------------------------------

def _pr_payload(action: str, pr_number: int = 10, sha: str = "sha1") -> dict:
    return {
        "action": action,
        "repository": {"full_name": "org/repo"},
        "pull_request": {
            "number": pr_number,
            "head": {"sha": sha},
        },
    }


def _check_suite_payload(conclusion: str, sha: str = "sha1", suite_id: int = 1) -> dict:
    return {
        "action": "completed",
        "repository": {"full_name": "org/repo"},
        "check_suite": {
            "id": suite_id,
            "head_sha": sha,
            "conclusion": conclusion,
        },
    }


def test_normalize_event_pr_synchronize_is_action_required() -> None:
    event = normalize_event("d1", "pull_request", _pr_payload("synchronize"))
    assert event is not None
    assert event.action_required is True
    assert event.trigger_type == "pull_request.synchronize"
    assert event.pr_number == 10
    assert event.repo_full_name == "org/repo"
    assert event.head_sha == "sha1"
    assert event.delivery_id == "d1"
    assert event.status == QueueStatus.PENDING


def test_normalize_event_pr_opened_is_action_required() -> None:
    event = normalize_event("d2", "pull_request", _pr_payload("opened"))
    assert event is not None
    assert event.action_required is True
    assert event.trigger_type == "pull_request.opened"


def test_normalize_event_pr_opened_pr42_deadbeef() -> None:
    """Test PR #42 with SHA deadbeef1234 normalizes correctly."""
    payload = _pr_payload("opened", pr_number=42, sha="deadbeef1234")
    event = normalize_event("d-pr42", "pull_request", payload)

    assert event is not None
    assert event.trigger_type == "pull_request.opened"
    assert event.pr_number == 42
    assert event.head_sha == "deadbeef1234"
    assert event.action_required is True
    assert event.repo_full_name == "org/repo"


def test_normalize_event_check_suite_failure_is_action_required() -> None:
    event = normalize_event("d3", "check_suite", _check_suite_payload("failure"))
    assert event is not None
    assert event.action_required is True
    assert event.trigger_type == "check_suite.completed"


def test_normalize_event_pr_labeled_returns_none() -> None:
    event = normalize_event("d4", "pull_request", _pr_payload("labeled"))
    assert event is None


def test_normalize_event_pr_closed_returns_none() -> None:
    event = normalize_event("d4a", "pull_request", _pr_payload("closed"))
    assert event is None


def test_normalize_event_check_suite_success_not_action_required() -> None:
    event = normalize_event("d5", "check_suite", _check_suite_payload("success"))
    assert event is not None
    assert event.action_required is False


def test_normalize_event_review_requested_is_action_required() -> None:
    payload = _pr_payload("review_requested")
    event = normalize_event("d6", "pull_request", payload)
    assert event is not None
    assert event.action_required is True


def test_normalize_event_payload_hash_is_stable() -> None:
    payload = _pr_payload("synchronize", sha="abc")
    e1 = normalize_event("d1", "pull_request", payload)
    e2 = normalize_event("d1", "pull_request", payload)
    assert e1 is not None and e2 is not None
    assert e1.payload_hash == e2.payload_hash


def test_normalize_event_event_id_matches_dedupe_key() -> None:
    payload = _pr_payload("synchronize", pr_number=7, sha="s99")
    event = normalize_event("d-x", "pull_request", payload)
    assert event is not None
    expected_key = dedupe_key("pull_request", payload)
    assert event.event_id == expected_key


# ---------------------------------------------------------------------------
# RemediationQueue tests — enqueue dedup
# ---------------------------------------------------------------------------

@pytest.fixture()
def queue(tmp_path: Path) -> RemediationQueue:
    db_path = tmp_path / "test_queue.db"
    q = RemediationQueue(str(db_path))
    q.init_schema()
    return q


def test_enqueue_returns_true_on_first_insert(queue: RemediationQueue) -> None:
    event = normalize_event("d1", "pull_request", _pr_payload("synchronize"))
    assert event is not None
    result = queue.enqueue(event)
    assert result is True


def test_enqueue_returns_false_on_duplicate(queue: RemediationQueue) -> None:
    event = normalize_event("d1", "pull_request", _pr_payload("synchronize"))
    assert event is not None
    queue.enqueue(event)
    result = queue.enqueue(event)
    assert result is False


def test_enqueue_allows_different_events(queue: RemediationQueue) -> None:
    e1 = normalize_event("d1", "pull_request", _pr_payload("synchronize", sha="sha-a"))
    e2 = normalize_event("d2", "pull_request", _pr_payload("synchronize", sha="sha-b"))
    assert e1 is not None and e2 is not None
    assert queue.enqueue(e1) is True
    assert queue.enqueue(e2) is True


def test_dequeue_pending_returns_events_and_marks_in_progress(queue: RemediationQueue) -> None:
    event = normalize_event("d1", "pull_request", _pr_payload("synchronize"))
    assert event is not None
    queue.enqueue(event)
    dequeued = queue.dequeue_pending(limit=10)
    assert len(dequeued) == 1
    assert dequeued[0].event_id == event.event_id
    # After dequeue, events are atomically marked IN_PROGRESS
    # so a second dequeue returns nothing
    second = queue.dequeue_pending(limit=10)
    assert second == []


def test_update_status_changes_event_status(queue: RemediationQueue) -> None:
    event = normalize_event("d1", "pull_request", _pr_payload("synchronize"))
    assert event is not None
    queue.enqueue(event)
    queue.update_status(event.event_id, QueueStatus.IN_PROGRESS)
    pending = queue.dequeue_pending(limit=10)
    assert len(pending) == 0


def test_update_status_increments_attempt_count(queue: RemediationQueue) -> None:
    event = normalize_event("d1", "pull_request", _pr_payload("synchronize"))
    assert event is not None
    queue.enqueue(event)
    queue.update_status(event.event_id, QueueStatus.IN_PROGRESS, attempt_delta=1)
    # Re-fetch by changing status back to PENDING to inspect
    queue.update_status(event.event_id, QueueStatus.PENDING)
    pending = queue.dequeue_pending(limit=10)
    assert pending[0].attempt_count == 1


def test_update_status_sets_next_retry_at(queue: RemediationQueue) -> None:
    import sqlite3
    import time
    event = normalize_event("d1", "pull_request", _pr_payload("synchronize"))
    assert event is not None
    queue.enqueue(event)
    future = time.time() + 9999
    queue.update_status(event.event_id, QueueStatus.PENDING, next_retry_at=future)
    # Event has next_retry_at in the future, so dequeue should skip it
    pending = queue.dequeue_pending(limit=10)
    assert len(pending) == 0


def test_mark_stale_returns_count(queue: RemediationQueue) -> None:
    # mark_stale with max_age_hours=0 should mark everything stale
    event = normalize_event("d1", "pull_request", _pr_payload("synchronize"))
    assert event is not None
    queue.enqueue(event)
    count = queue.mark_stale(max_age_hours=0)
    assert count == 1


def test_mkdir_creates_parent_dirs(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b" / "c" / "queue.db"
    q = RemediationQueue(str(nested))
    q.init_schema()
    assert nested.parent.exists()
