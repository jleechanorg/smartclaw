"""Tests for webhook_reconciler: run_once stuck-reset and stale-reset logic."""
from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import tempfile

from orchestration.webhook_queue import (
    NormalizedEvent,
    QueueStatus,
    RemediationQueue,
)
from orchestration.webhook_reconciler import (
    ReconcilerConfig,
    WebhookReconciler,
    check_pr_open,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_queue() -> RemediationQueue:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    q = RemediationQueue(db_path=tmp.name)
    q.init_schema()
    return q


def _event(
    event_id: str,
    status: QueueStatus = QueueStatus.PENDING,
    attempt_count: int = 0,
    age_minutes: int = 0,
    pr_number: int | None = 1,
    repo: str = "owner/repo",
) -> NormalizedEvent:
    enqueued_at = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
    return NormalizedEvent(
        event_id=event_id,
        delivery_id=f"del-{event_id}",
        trigger_type="pull_request.opened",
        repo_full_name=repo,
        pr_number=pr_number,
        head_sha="abc123",
        action_required=True,
        payload_hash="hash",
        enqueued_at=enqueued_at,
        attempt_count=attempt_count,
        status=status,
    )


def _enqueue_with_status(
    queue: RemediationQueue,
    event: NormalizedEvent,
    status: QueueStatus,
    age_minutes: int = 0,
) -> None:
    """Insert event and immediately set its status.

    Also backdates updated_at by age_minutes so time-based queries work correctly.
    """
    queue.enqueue(event)
    if status != QueueStatus.PENDING:
        queue.update_status(event.event_id, status)
    if age_minutes > 0:
        old_ts = time.time() - (age_minutes * 60)
        with closing(sqlite3.connect(queue._db_path)) as conn:
            conn.execute(
                "UPDATE remediation_queue SET updated_at = ? WHERE event_id = ?",
                (old_ts, event.event_id),
            )
            conn.commit()


# ---------------------------------------------------------------------------
# stuck IN_PROGRESS reset
# ---------------------------------------------------------------------------


def test_stuck_in_progress_reset_to_pending() -> None:
    """Events IN_PROGRESS for longer than stuck_in_progress_minutes are reset to PENDING."""
    q = _make_queue()
    cfg = ReconcilerConfig(stuck_in_progress_minutes=30)
    rec = WebhookReconciler(queue=q, config=cfg)

    # Stuck event: IN_PROGRESS for 60 minutes (backdate updated_at)
    stuck = _event("stuck-1", age_minutes=60)
    _enqueue_with_status(q, stuck, QueueStatus.IN_PROGRESS, age_minutes=60)

    # Recent event: IN_PROGRESS for only 5 minutes (should NOT be reset)
    recent = _event("recent-1", age_minutes=5)
    _enqueue_with_status(q, recent, QueueStatus.IN_PROGRESS, age_minutes=5)

    with patch(
        "orchestration.webhook_reconciler.check_pr_open", return_value=True
    ):
        stats = rec.run_once()

    assert stats.stuck_reset == 1

    # Verify DB state
    rows = _fetch_statuses(q)
    assert rows["stuck-1"] == "PENDING"
    assert rows["recent-1"] == "IN_PROGRESS"


def test_no_stuck_reset_when_within_threshold() -> None:
    """Events IN_PROGRESS within threshold are left alone."""
    q = _make_queue()
    cfg = ReconcilerConfig(stuck_in_progress_minutes=30)
    rec = WebhookReconciler(queue=q, config=cfg)

    ev = _event("ok-1", age_minutes=10)
    _enqueue_with_status(q, ev, QueueStatus.IN_PROGRESS, age_minutes=10)

    with patch("orchestration.webhook_reconciler.check_pr_open", return_value=True):
        stats = rec.run_once()

    assert stats.stuck_reset == 0


# ---------------------------------------------------------------------------
# stale PENDING reset / mark STALE
# ---------------------------------------------------------------------------


def test_stale_pending_pr_open_reset() -> None:
    """Stale PENDING events whose PR is still open get their attempt_count reset."""
    q = _make_queue()
    cfg = ReconcilerConfig(stale_pending_hours=2)
    rec = WebhookReconciler(queue=q, config=cfg)

    stale = _event("stale-open", age_minutes=180, attempt_count=3)
    q.enqueue(stale)
    old_ts = time.time() - (180 * 60)
    with closing(sqlite3.connect(q._db_path)) as conn:
        conn.execute(
            "UPDATE remediation_queue SET updated_at = ? WHERE event_id = ?",
            (old_ts, "stale-open"),
        )
        conn.commit()

    with patch(
        "orchestration.webhook_reconciler.check_pr_open", return_value=True
    ):
        stats = rec.run_once()

    assert stats.stale_reset == 1
    rows = _fetch_statuses(q)
    assert rows["stale-open"] == "PENDING"


def test_stale_pending_pr_closed_marks_stale() -> None:
    """Stale PENDING events whose PR is closed are marked STALE."""
    q = _make_queue()
    cfg = ReconcilerConfig(stale_pending_hours=2)
    rec = WebhookReconciler(queue=q, config=cfg)

    stale = _event("stale-closed", age_minutes=180)
    q.enqueue(stale)
    old_ts = time.time() - (180 * 60)
    with closing(sqlite3.connect(q._db_path)) as conn:
        conn.execute(
            "UPDATE remediation_queue SET updated_at = ? WHERE event_id = ?",
            (old_ts, "stale-closed"),
        )
        conn.commit()

    with patch(
        "orchestration.webhook_reconciler.check_pr_open", return_value=False
    ):
        rec.run_once()

    rows = _fetch_statuses(q)
    assert rows["stale-closed"] == "STALE"


def test_fresh_pending_not_touched() -> None:
    """Fresh PENDING events (within stale_pending_hours) are not touched."""
    q = _make_queue()
    cfg = ReconcilerConfig(stale_pending_hours=2)
    rec = WebhookReconciler(queue=q, config=cfg)

    fresh = _event("fresh-1", age_minutes=30)
    q.enqueue(fresh)
    # updated_at is set to now on enqueue, so it's fresh

    with patch("orchestration.webhook_reconciler.check_pr_open", return_value=True):
        stats = rec.run_once()

    assert stats.stale_reset == 0
    rows = _fetch_statuses(q)
    assert rows["fresh-1"] == "PENDING"


# ---------------------------------------------------------------------------
# FAILED re-queue
# ---------------------------------------------------------------------------


def test_failed_requeued_within_limit() -> None:
    """FAILED events within max_failed_requeue * max_attempts are reset to PENDING."""
    q = _make_queue()
    # max_failed_requeue=1, so events with attempt_count < max_attempts get requeued
    cfg = ReconcilerConfig(max_failed_requeue=1)
    rec = WebhookReconciler(queue=q, config=cfg)

    # Low attempt count — should be requeued
    ev = _event("fail-low", attempt_count=1)
    _enqueue_with_status(q, ev, QueueStatus.FAILED)

    with patch("orchestration.webhook_reconciler.check_pr_open", return_value=True):
        stats = rec.run_once()

    assert stats.failed_requeued >= 1
    rows = _fetch_statuses(q)
    assert rows["fail-low"] == "PENDING"


def test_failed_not_requeued_over_limit() -> None:
    """FAILED events that exceed the requeue limit remain FAILED."""
    q = _make_queue()
    cfg = ReconcilerConfig(max_failed_requeue=1)
    rec = WebhookReconciler(queue=q, config=cfg)

    # Very high attempt count — should NOT be requeued
    ev = _event("fail-high", attempt_count=99)
    _enqueue_with_status(q, ev, QueueStatus.FAILED)

    with patch("orchestration.webhook_reconciler.check_pr_open", return_value=True):
        rec.run_once()

    rows = _fetch_statuses(q)
    assert rows["fail-high"] == "FAILED"


# ---------------------------------------------------------------------------
# ReconcilerStats accumulation
# ---------------------------------------------------------------------------


def test_stats_all_zeros_empty_queue() -> None:
    """Stats are all zero when queue is empty."""
    q = _make_queue()
    rec = WebhookReconciler(queue=q)

    with patch("orchestration.webhook_reconciler.check_pr_open", return_value=True):
        stats = rec.run_once()

    assert stats.stuck_reset == 0
    assert stats.stale_reset == 0
    assert stats.failed_requeued == 0


# ---------------------------------------------------------------------------
# check_pr_open helper
# ---------------------------------------------------------------------------


def test_check_pr_open_returns_true_on_subprocess_error() -> None:
    """check_pr_open is fail-open: returns True when gh CLI fails."""
    with patch("subprocess.run", side_effect=FileNotFoundError("gh not found")):
        result = check_pr_open("owner/repo", 42)
    assert result is True


def test_check_pr_open_returns_true_for_open_pr() -> None:
    """check_pr_open returns True when gh API says state=open."""
    import subprocess

    mock_result = subprocess.CompletedProcess(
        args=[], returncode=0, stdout='{"state": "open"}', stderr=""
    )
    with patch("subprocess.run", return_value=mock_result):
        result = check_pr_open("owner/repo", 1)
    assert result is True


def test_check_pr_open_returns_false_for_closed_pr() -> None:
    """check_pr_open returns False when gh API says state=closed."""
    import subprocess

    mock_result = subprocess.CompletedProcess(
        args=[], returncode=0, stdout='{"state": "closed"}', stderr=""
    )
    with patch("subprocess.run", return_value=mock_result):
        result = check_pr_open("owner/repo", 2)
    assert result is False


# ---------------------------------------------------------------------------
# DB inspection helper
# ---------------------------------------------------------------------------


def _fetch_statuses(queue: RemediationQueue) -> dict[str, str]:
    import sqlite3

    con = sqlite3.connect(queue._db_path)
    rows = con.execute(
        "SELECT event_id, status FROM remediation_queue"
    ).fetchall()
    con.close()
    return {r[0]: r[1] for r in rows}
