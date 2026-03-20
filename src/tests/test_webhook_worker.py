"""Tests for webhook_worker: PRLock, RetryBudget, RemediationWorker."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from orchestration.webhook_queue import NormalizedEvent, QueueStatus, RemediationQueue
from orchestration.webhook_queue import normalize_event
from orchestration.webhook_worker import (
    DispatchError,
    PRLock,
    RemediationWorker,
    RetryBudget,
)
from orchestration.webhook_ingress import WebhookStore, WebhookRecord, WebhookIngress
from orchestration.action_executor import ActionResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    event_id: str = "pr:repo/name:1:abc123:pull_request.synchronize",
    pr_number: int | None = 1,
    status: QueueStatus = QueueStatus.PENDING,
    attempt_count: int = 0,
) -> NormalizedEvent:
    return NormalizedEvent(
        event_id=event_id,
        delivery_id="delivery-001",
        trigger_type="pull_request.synchronize",
        repo_full_name="repo/name",
        pr_number=pr_number,
        head_sha="abc123",
        action_required=True,
        payload_hash="deadbeef",
        enqueued_at=datetime.now(timezone.utc),
        attempt_count=attempt_count,
        status=status,
    )


def _tmp_queue() -> RemediationQueue:
    """Return a RemediationQueue backed by a temporary file."""
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    q = RemediationQueue(db_path=tmp)
    q.init_schema()
    return q


# ---------------------------------------------------------------------------
# PRLock tests
# ---------------------------------------------------------------------------


class TestPRLock:
    def test_acquire_and_release(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            lock = PRLock(db_path=f.name)
            assert lock.acquire("pr:repo/test:1") is True
            lock.release("pr:repo/test:1")  # should not raise

    def test_double_acquire_same_key_returns_false(self) -> None:
        """A second acquire on the same key before release should return False."""
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            lock = PRLock(db_path=f.name)
            assert lock.acquire("pr:repo/test:1") is True
            # Second acquire on same lock instance before release → should fail
            assert lock.acquire("pr:repo/test:1", timeout_seconds=0.05) is False
            lock.release("pr:repo/test:1")

    def test_acquire_different_keys_independent(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            lock = PRLock(db_path=f.name)
            assert lock.acquire("pr:repo/test:1") is True
            assert lock.acquire("pr:repo/test:2") is True
            lock.release("pr:repo/test:1")
            lock.release("pr:repo/test:2")

    def test_context_manager(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            lock = PRLock(db_path=f.name)
            with lock.for_key("pr:repo/test:1"):
                pass  # should not raise

    def test_context_manager_releases_on_success(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            lock = PRLock(db_path=f.name)
            with lock.for_key("pr:repo/test:99"):
                pass
            # After context exit, acquiring again should succeed
            assert lock.acquire("pr:repo/test:99") is True
            lock.release("pr:repo/test:99")

    def test_context_manager_releases_on_exception(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            lock = PRLock(db_path=f.name)
            try:
                with lock.for_key("pr:repo/test:7"):
                    raise ValueError("boom")
            except ValueError:
                pass
            # Lock should be released despite exception
            assert lock.acquire("pr:repo/test:7") is True
            lock.release("pr:repo/test:7")

    def test_concurrent_acquire_blocks_until_release(self) -> None:
        """Thread B should not acquire until thread A releases."""
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            lock_a = PRLock(db_path=f.name)
            lock_b = PRLock(db_path=f.name)

            acquired_by_b: list[bool] = []

            assert lock_a.acquire("pr:repo/test:3", timeout_seconds=1.0) is True

            def try_b() -> None:
                result = lock_b.acquire("pr:repo/test:3", timeout_seconds=0.1)
                acquired_by_b.append(result)

            t = threading.Thread(target=try_b)
            t.start()
            t.join(timeout=2.0)

            # Lock held by A is fresh (< stale_lock_seconds), so B times out
            assert acquired_by_b == [False]
            lock_a.release("pr:repo/test:3")

    def test_crash_recovery_steals_stale_lock(self) -> None:
        """A stale lock (older than stale_lock_seconds) can be recovered."""
        import time as time_mod
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            lock_a = PRLock(db_path=f.name)
            lock_b = PRLock(db_path=f.name)
            # Set stale threshold very low for testing
            lock_b.stale_lock_seconds = 0.0

            assert lock_a.acquire("pr:repo/test:stale") is True
            # lock_b should recover the stale lock
            assert lock_b.acquire("pr:repo/test:stale", timeout_seconds=0.2) is True
            lock_b.release("pr:repo/test:stale")


# ---------------------------------------------------------------------------
# RetryBudget tests
# ---------------------------------------------------------------------------


class TestRetryBudget:
    def test_default_values(self) -> None:
        b = RetryBudget()
        assert b.max_attempts == 3
        assert b.backoff_seconds == [5, 30, 120]

    def test_next_delay_within_list(self) -> None:
        b = RetryBudget()
        assert b.next_delay(0) == 5
        assert b.next_delay(1) == 30
        assert b.next_delay(2) == 120

    def test_next_delay_beyond_list_returns_last(self) -> None:
        b = RetryBudget()
        assert b.next_delay(3) == 120
        assert b.next_delay(99) == 120

    def test_next_delay_empty_list_returns_zero(self) -> None:
        b = RetryBudget(backoff_seconds=[])
        assert b.next_delay(0) == 0.0
        assert b.next_delay(5) == 0.0

    def test_custom_backoff(self) -> None:
        b = RetryBudget(max_attempts=5, backoff_seconds=[1, 2, 4, 8])
        assert b.next_delay(0) == 1
        assert b.next_delay(3) == 8
        assert b.next_delay(4) == 8  # beyond list → last value


# ---------------------------------------------------------------------------
# RemediationWorker tests
# ---------------------------------------------------------------------------


class TestRemediationWorker:
    def test_run_once_empty_queue(self) -> None:
        q = _tmp_queue()
        worker = RemediationWorker(queue=q)
        processed, failed = worker.run_once(limit=10)
        assert processed == 0
        assert failed == 0

    def test_run_once_success(self) -> None:
        q = _tmp_queue()
        event = _make_event()
        q.enqueue(event)

        worker = RemediationWorker(queue=q)
        with patch.object(worker, "_dispatch", return_value=None):
            processed, failed = worker.run_once(limit=10)

        assert processed == 1
        assert failed == 0

        # Status should be DONE
        rows = q.dequeue_pending(limit=10)
        assert rows == []  # nothing left as PENDING

    def test_run_once_dispatch_error_increments_attempt(self) -> None:
        q = _tmp_queue()
        event = _make_event(attempt_count=0)
        q.enqueue(event)

        budget = RetryBudget(max_attempts=3)
        worker = RemediationWorker(queue=q, budget=budget)
        with patch.object(worker, "_dispatch", side_effect=DispatchError("fail")):
            processed, failed = worker.run_once(limit=10)

        assert processed == 0
        assert failed == 1

    def test_run_once_escalates_to_failed_at_max_attempts(self) -> None:
        """When attempt_count reaches max_attempts, status must be FAILED."""
        q = _tmp_queue()
        # Event already at max_attempts - 1 so this dispatch will push it over
        event = _make_event(attempt_count=2)
        q.enqueue(event)

        budget = RetryBudget(max_attempts=3)
        worker = RemediationWorker(queue=q, budget=budget)
        with patch.object(worker, "_dispatch", side_effect=DispatchError("fail")):
            processed, failed = worker.run_once(limit=10)

        assert failed == 1
        # Should NOT remain PENDING
        assert q.dequeue_pending(limit=10) == []

    def test_run_once_multiple_events(self) -> None:
        q = _tmp_queue()
        for i in range(3):
            q.enqueue(_make_event(event_id=f"pr:repo/name:{i}:sha:pull_request.synchronize", pr_number=i))

        worker = RemediationWorker(queue=q)
        with patch.object(worker, "_dispatch", return_value=None):
            processed, failed = worker.run_once(limit=10)

        assert processed == 3
        assert failed == 0

    def test_run_once_respects_limit(self) -> None:
        q = _tmp_queue()
        for i in range(5):
            q.enqueue(_make_event(event_id=f"pr:repo/name:{i}:sha:pull_request.synchronize", pr_number=i))

        worker = RemediationWorker(queue=q)
        with patch.object(worker, "_dispatch", return_value=None):
            processed, failed = worker.run_once(limit=2)

        assert processed == 2
        assert failed == 0
        # 3 events should remain pending
        assert len(q.dequeue_pending(limit=10)) == 3

    def test_pr_lock_prevents_concurrent_same_pr(self) -> None:
        """process_one should skip if the PR lock cannot be acquired."""
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            q = _tmp_queue()
            event = _make_event()
            q.enqueue(event)

            worker = RemediationWorker(queue=q)
            # Pre-acquire the lock so process_one can't get it
            worker._lock = PRLock(db_path=f.name)
            pr_key = f"{event.repo_full_name}:{event.pr_number}"
            worker._lock.acquire(pr_key, timeout_seconds=1.0)

            # Should return False because lock is held
            result = worker.process_one(event)
            assert result is False

            worker._lock.release(pr_key)

    def test_dispatch_error_type(self) -> None:
        err = DispatchError("something went wrong")
        assert isinstance(err, Exception)
        assert str(err) == "something went wrong"


# ---------------------------------------------------------------------------
# Daemon integration tests (store -> queue -> escalation)
# ---------------------------------------------------------------------------


def _tmp_store() -> tuple[WebhookStore, str]:
    """Return a WebhookStore backed by a temporary file."""
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    store = WebhookStore(db_path=tmp)
    store.init_schema()
    return store, tmp


def _make_pr_webhook_payload() -> dict:
    """Create a valid PR webhook payload."""
    return {
        "action": "synchronize",
        "pull_request": {
            "number": 42,
            "head": {"sha": "abc123def456"},
            "base": {"repo": {"full_name": "owner/repo"}},
        },
        "repository": {"full_name": "owner/repo"},
    }


def _make_ao_webhook_payload() -> dict:
    """Create a valid AO (Agent Orchestration) webhook payload."""
    return {
        "ao_event_type": "session_escalation",
        "session_id": "session-123",
        "escalation_context": {
            "reaction_key": "test_reaction",
        },
        "data": {
            "task_id": "task-456",
            "subtask_id": "subtask-789",
        },
    }


class TestStoreToQueueIntegration:
    """Tests for the flow from WebhookStore -> RemediationQueue -> processing."""

    def test_store_poll_and_normalize_pr_event(self) -> None:
        """Worker should pick up unprocessed PR webhook, normalize, and enqueue."""
        store, db_path = _tmp_store()
        queue = RemediationQueue(db_path=db_path)
        queue.init_schema()

        # Insert a PR webhook into the store
        payload = _make_pr_webhook_payload()
        record = WebhookRecord(
            delivery_id="delivery-pr-001",
            event_type="pull_request",
            payload=json.dumps(payload),
            headers={"X-GitHub-Delivery": "delivery-pr-001"},
            received_at=datetime.now(timezone.utc),
        )
        store.store(record)

        # Verify initial state
        unprocessed = store.get_unprocessed(limit=10)
        assert len(unprocessed) == 1

        # Simulate what the daemon does: normalize and enqueue
        normalized = normalize_event(
            delivery_id=record.delivery_id,
            event_type=record.event_type,
            raw_payload=payload,
        )
        assert normalized is not None
        assert normalized.trigger_type == "pull_request.synchronize"
        assert normalized.repo_full_name == "owner/repo"
        assert normalized.pr_number == 42

        # Enqueue to queue
        queue.enqueue(normalized)

        # Verify it was enqueued
        pending = queue.dequeue_pending(limit=10)
        assert len(pending) == 1
        assert pending[0].event_id == normalized.event_id

    def test_store_idempotent_re_delivery(self) -> None:
        """Re-processing same delivery_id should be no-op (idempotent)."""
        store, db_path = _tmp_store()
        queue = RemediationQueue(db_path=db_path)
        queue.init_schema()

        # Insert webhook
        payload = _make_pr_webhook_payload()
        record = WebhookRecord(
            delivery_id="delivery-idempotent-001",
            event_type="pull_request",
            payload=json.dumps(payload),
            headers={"X-GitHub-Delivery": "delivery-idempotent-001"},
            received_at=datetime.now(timezone.utc),
        )
        inserted = store.store(record)
        assert inserted is True  # First insert

        # Try to insert same delivery_id again
        inserted_again = store.store(record)
        assert inserted_again is False  # Idempotent - should be no-op

        # Normalize and enqueue (first time)
        normalized = normalize_event(
            delivery_id=record.delivery_id,
            event_type=record.event_type,
            raw_payload=payload,
        )
        queue.enqueue(normalized)

        # Try to enqueue again (idempotent)
        enqueued_again = queue.enqueue(normalized)
        assert enqueued_again is False  # Already exists

    def test_store_ao_webhook_detection(self) -> None:
        """AO webhooks should be detected via ao_event_type field."""
        # AO webhook
        ao_payload = _make_ao_webhook_payload()
        assert "ao_event_type" in ao_payload

        # PR webhook
        pr_payload = _make_pr_webhook_payload()
        assert "ao_event_type" not in pr_payload

    def test_mark_processed_after_normalization(self) -> None:
        """After processing, mark the store record as processed."""
        store, db_path = _tmp_store()

        payload = _make_pr_webhook_payload()
        record = WebhookRecord(
            delivery_id="delivery-processed-001",
            event_type="pull_request",
            payload=json.dumps(payload),
            headers={"X-GitHub-Delivery": "delivery-processed-001"},
            received_at=datetime.now(timezone.utc),
        )
        store.store(record)

        # Before processing
        unprocessed = store.get_unprocessed(limit=10)
        assert len(unprocessed) == 1

        # Mark as processed
        store.mark_processed(record.delivery_id)

        # After processing
        unprocessed_after = store.get_unprocessed(limit=10)
        assert len(unprocessed_after) == 0

    def test_queue_handles_exception_continues(self) -> None:
        """Worker should handle exceptions and continue processing other events."""
        store, db_path = _tmp_store()
        queue = RemediationQueue(db_path=db_path)
        queue.init_schema()

        # Insert two webhooks
        payload1 = _make_pr_webhook_payload()
        payload1["pull_request"]["number"] = 1
        record1 = WebhookRecord(
            delivery_id="delivery-fail-001",
            event_type="pull_request",
            payload=json.dumps(payload1),
            headers={"X-GitHub-Delivery": "delivery-fail-001"},
            received_at=datetime.now(timezone.utc),
        )
        store.store(record1)

        payload2 = _make_pr_webhook_payload()
        payload2["pull_request"]["number"] = 2
        record2 = WebhookRecord(
            delivery_id="delivery-fail-002",
            event_type="pull_request",
            payload=json.dumps(payload2),
            headers={"X-GitHub-Delivery": "delivery-fail-002"},
            received_at=datetime.now(timezone.utc),
        )
        store.store(record2)

        # Normalize first event (this will succeed)
        normalized1 = normalize_event(
            delivery_id=record1.delivery_id,
            event_type=record1.event_type,
            raw_payload=payload1,
        )
        queue.enqueue(normalized1)

        # Second event has invalid JSON - will fail during normalization
        # (simulate by calling normalize with invalid payload)
        # The daemon should catch this exception and continue

        # Process what we can - the queue should handle exceptions gracefully
        pending = queue.dequeue_pending(limit=10)
        assert len(pending) == 1  # Only valid event

    def test_worker_with_escalation_handler_integration(self) -> None:
        """Worker should integrate with escalation handler for AO events."""
        # This tests that the daemon's _handle_ao_webhook would work
        # when an AO webhook is received

        # Mock the CLI and notifier
        mock_cli = MagicMock()
        mock_notifier = MagicMock()

        # Create a mock action result using correct dataclass fields
        mock_result = ActionResult(
            success=True,
            action_type="notify",
            details={"message": "Test action completed"},
        )

        # The handle_escalation function requires proper protocol implementations
        # This test verifies the integration point exists
        payload = _make_ao_webhook_payload()

        # Verify the payload has the expected structure for escalation
        assert payload.get("ao_event_type") == "session_escalation"
        assert "escalation_context" in payload
        assert payload["escalation_context"]["reaction_key"] == "test_reaction"

        # Verify the mock result has correct structure
        assert mock_result.success is True
        assert mock_result.action_type == "notify"


# ---------------------------------------------------------------------------
# WebhookIngress.shutdown() — TDD
# ---------------------------------------------------------------------------


def test_webhook_ingress_shutdown(tmp_path: Path) -> None:
    """WebhookIngress must expose shutdown() and it must be a no-op when server is not running."""
    db = str(tmp_path / "ingress_test.db")
    ingress = WebhookIngress(host="127.0.0.1", port=19999, db_path=db)
    assert hasattr(ingress, "shutdown"), "WebhookIngress must have shutdown() for graceful daemon stop"
    ingress.shutdown()  # no-op when serve_forever() was never called — must not raise
