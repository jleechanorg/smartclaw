"""Webhook pipeline: combined ingress, queue, worker, and daemon.

This module combines:
- webhook_ingress: HTTP adapter, HMAC validation, SQLite queue
- webhook_queue: event normalization, dedupe keys, SQLite-backed queue
- webhook_worker: PR lock, retry budget, event dispatch
- webhook_daemon: supervised ingress+worker process
- webhook_metrics: inline counters (no external dependencies)

Responsibilities:
- Validate X-Hub-Signature-256 on incoming GitHub webhook deliveries
- Store raw payload + headers + receive timestamp to SQLite
- Deduplicate by X-GitHub-Delivery header
- Normalize events and enqueue to remediation queue
- Process events via RemediationWorker with PR lock + bounded retries
- Combined daemon with logging and signal handling
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Generator


# =============================================================================
# PART 1: Ingress - HTTP adapter, HMAC validation, SQLite store
# =============================================================================

class GitHubEventType(StrEnum):
    """Known GitHub webhook event types handled by the ingress layer."""
    PULL_REQUEST = "pull_request"
    PULL_REQUEST_REVIEW = "pull_request_review"
    PULL_REQUEST_REVIEW_COMMENT = "pull_request_review_comment"
    ISSUE_COMMENT = "issue_comment"
    CHECK_SUITE = "check_suite"
    PUSH = "push"
    PING = "ping"


_DEFAULT_DB_PATH = os.path.expanduser("~/.openclaw/webhook_queue.db")

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS webhook_deliveries (
    delivery_id  TEXT PRIMARY KEY,
    event_type   TEXT NOT NULL,
    payload      TEXT NOT NULL,
    headers      TEXT NOT NULL,
    received_at  TEXT NOT NULL,
    processed    INTEGER NOT NULL DEFAULT 0
);
"""

_INSERT_SQL = """
INSERT OR IGNORE INTO webhook_deliveries
    (delivery_id, event_type, payload, headers, received_at, processed)
VALUES (?, ?, ?, ?, ?, 0)
"""

_SELECT_UNPROCESSED_SQL = """
SELECT delivery_id, event_type, payload, headers, received_at, processed
FROM webhook_deliveries
WHERE processed = 0
ORDER BY received_at ASC
LIMIT ?
"""

_MARK_PROCESSED_SQL = """
UPDATE webhook_deliveries SET processed = 1 WHERE delivery_id = ?
"""


@dataclass
class WebhookRecord:
    """Represents a single GitHub webhook delivery in the queue."""
    delivery_id: str
    event_type: str
    payload: str
    headers: dict[str, str]
    received_at: datetime
    processed: bool = False


def validate_signature(
    payload_bytes: bytes,
    signature_header: str,
    secret: str,
) -> bool:
    """Return True iff X-Hub-Signature-256 header matches HMAC-SHA256 of payload."""
    if not signature_header.startswith("sha256="):
        return False
    expected_hex = signature_header[len("sha256="):]
    if not expected_hex:
        return False
    try:
        expected_bytes = bytes.fromhex(expected_hex)
    except ValueError:
        return False
    actual_bytes = hmac.new(secret.encode(), payload_bytes, hashlib.sha256).digest()
    return hmac.compare_digest(actual_bytes, expected_bytes)


class WebhookStore:
    """Manages a SQLite queue of received webhook deliveries."""

    def __init__(self, db_path: str = _DEFAULT_DB_PATH) -> None:
        self._db_path = db_path

    def init_schema(self) -> None:
        """Create the deliveries table if it does not already exist."""
        with closing(self._connect()) as conn:
            conn.execute(_CREATE_TABLE_SQL)
            conn.commit()

    def store(self, record: WebhookRecord) -> bool:
        """Persist a WebhookRecord. Returns True if inserted, False if already present."""
        received_iso = record.received_at.astimezone(timezone.utc).isoformat()
        headers_json = json.dumps(record.headers)
        with closing(self._connect()) as conn:
            cur = conn.execute(
                _INSERT_SQL,
                (
                    record.delivery_id,
                    record.event_type,
                    record.payload,
                    headers_json,
                    received_iso,
                ),
            )
            conn.commit()
            return cur.rowcount > 0

    def mark_processed(self, delivery_id: str) -> None:
        """Mark a delivery as processed."""
        with closing(self._connect()) as conn:
            conn.execute(_MARK_PROCESSED_SQL, (delivery_id,))
            conn.commit()

    def get_unprocessed(self, limit: int = 100) -> list[WebhookRecord]:
        """Return up to limit unprocessed records, oldest first."""
        with closing(self._connect()) as conn:
            rows = conn.execute(_SELECT_UNPROCESSED_SQL, (limit,)).fetchall()
        return [self._row_to_record(row) for row in rows]

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)

    @staticmethod
    def _row_to_record(row: tuple[Any, ...]) -> WebhookRecord:
        delivery_id, event_type, payload, headers_json, received_iso, processed_int = row
        headers: dict[str, str] = json.loads(headers_json)
        received_at = datetime.fromisoformat(received_iso)
        return WebhookRecord(
            delivery_id=delivery_id,
            event_type=event_type,
            payload=payload,
            headers=headers,
            received_at=received_at,
            processed=bool(processed_int),
        )


class _WebhookHandler(BaseHTTPRequestHandler):
    """BaseHTTPRequestHandler that validates and queues GitHub webhook deliveries."""

    store: WebhookStore
    webhook_secret: str | None

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status": "ok"}')
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        if self.path not in ("/", "/webhook"):
            self.send_response(404)
            self.end_headers()
            return
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            raw_body = self.rfile.read(content_length) if content_length else b""
        except (ValueError, OSError):
            self._respond(400)
            return

        secret = self.webhook_secret
        if secret:
            sig_header = self.headers.get("X-Hub-Signature-256", "")
            if not validate_signature(raw_body, sig_header, secret):
                self._respond(401)
                return

        delivery_id = self.headers.get("X-GitHub-Delivery", "")
        event_type = self.headers.get("X-GitHub-Event", "unknown")

        if not delivery_id:
            self._respond(400)
            return

        try:
            payload_str = raw_body.decode("utf-8")
            json.loads(payload_str)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._respond(400)
            return

        headers_snapshot = {
            "X-GitHub-Delivery": delivery_id,
            "X-GitHub-Event": event_type,
            "X-Hub-Signature-256": self.headers.get("X-Hub-Signature-256", ""),
            "Content-Type": self.headers.get("Content-Type", ""),
        }

        record = WebhookRecord(
            delivery_id=delivery_id,
            event_type=event_type,
            payload=payload_str,
            headers=headers_snapshot,
            received_at=datetime.now(tz=timezone.utc),
        )
        is_new = self.store.store(record)
        if not is_new:
            inc_metric("webhooks_deduped")
        self._respond(204)

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: ANN001
        pass

    def _respond(self, status: int) -> None:
        self.send_response(status)
        self.end_headers()


class WebhookIngress:
    """Minimal HTTP server that ingests GitHub webhook deliveries into a local queue."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 9100,
        db_path: str = _DEFAULT_DB_PATH,
        webhook_secret: str | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._store = WebhookStore(db_path=db_path)
        self._store.init_schema()
        self._secret = webhook_secret or os.environ.get("GITHUB_WEBHOOK_SECRET")
        self._server: HTTPServer | None = None

    def serve_forever(self) -> None:
        """Start the HTTP server and block until interrupted."""
        store = self._store
        secret = self._secret

        class _Handler(_WebhookHandler):
            pass

        _Handler.store = store  # type: ignore[attr-defined]
        _Handler.webhook_secret = secret  # type: ignore[attr-defined]

        self._server = HTTPServer((self._host, self._port), _Handler)
        self._server.serve_forever()

    def shutdown(self) -> None:
        """Stop the HTTP server if running."""
        if self._server is not None:
            self._server.shutdown()
            self._server = None


# =============================================================================
# PART 2: Queue - event normalization, dedupe keys, SQLite queue
# =============================================================================

class QueueStatus(StrEnum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    DONE = "DONE"
    FAILED = "FAILED"
    STALE = "STALE"


@dataclass
class NormalizedEvent:
    """Stable remediation queue contract for a single GitHub event."""
    event_id: str
    delivery_id: str
    trigger_type: str
    repo_full_name: str
    pr_number: int | None
    head_sha: str
    action_required: bool
    payload_hash: str
    enqueued_at: datetime
    attempt_count: int = 0
    status: QueueStatus = field(default=QueueStatus.PENDING)


def _trigger_type_for(event_type: str, payload: dict) -> str | None:
    """Return a normalised trigger type string, or None when unrecognised."""
    action = payload.get("action")
    if event_type == "pull_request" and isinstance(action, str):
        return f"pull_request.{action}"
    if event_type == "pull_request_review" and isinstance(action, str):
        return f"pull_request_review.{action}"
    if event_type == "pull_request_review_comment" and isinstance(action, str):
        return f"pull_request_review_comment.{action}"
    if event_type == "check_suite" and action == "completed":
        return "check_suite.completed"
    return None


def dedupe_key(event_type: str, payload: dict, delivery_id: str = "") -> str:
    """Generate a stable dedupe key for an event payload."""
    repo = _extract_repo(payload)

    if event_type == "pull_request":
        pr = payload.get("pull_request") or {}
        pr_number = pr.get("number")
        head = pr.get("head") or {}
        head_sha = head.get("sha", "")
        action = payload.get("action", "")
        trigger = f"pull_request.{action}" if action else "pull_request"
        if repo and pr_number is not None and head_sha:
            return f"pr:{repo}:{pr_number}:{head_sha}:{trigger}"

    if event_type == "pull_request_review":
        pr = payload.get("pull_request") or {}
        pr_number = pr.get("number")
        head = pr.get("head") or {}
        head_sha = head.get("sha", "")
        action = payload.get("action", "")
        review = payload.get("review") or {}
        review_id = review.get("id", "")
        trigger = f"pull_request_review.{action}" if action else "pull_request_review"
        if repo and pr_number is not None and head_sha and review_id:
            return f"pr:{repo}:{pr_number}:{head_sha}:{trigger}:{review_id}"

    if event_type == "pull_request_review_comment":
        pr = payload.get("pull_request") or {}
        pr_number = pr.get("number")
        head = pr.get("head") or {}
        head_sha = head.get("sha", "")
        action = payload.get("action", "")
        comment = payload.get("comment") or {}
        comment_id = comment.get("id", "")
        trigger = f"pull_request_review_comment.{action}" if action else "pull_request_review_comment"
        if repo and pr_number is not None and head_sha and comment_id:
            return f"pr:{repo}:{pr_number}:{head_sha}:{trigger}:{comment_id}"

    if event_type == "check_suite":
        cs = payload.get("check_suite") or {}
        head_sha = cs.get("head_sha", "")
        suite_id = cs.get("id", "")
        if repo and head_sha:
            return f"cs:{repo}:{head_sha}:{suite_id}"

    return f"misc:{delivery_id}"


def _extract_repo(payload: dict) -> str:
    repo_obj = payload.get("repository")
    if isinstance(repo_obj, dict):
        return str(repo_obj.get("full_name", ""))
    return ""


def _extract_pr_number(payload: dict) -> int | None:
    pr = payload.get("pull_request")
    if isinstance(pr, dict):
        n = pr.get("number")
        if isinstance(n, int):
            return n
    cs = payload.get("check_suite")
    if isinstance(cs, dict):
        pulls = cs.get("pull_requests")
        if isinstance(pulls, list) and pulls:
            n = pulls[0].get("number")
            if isinstance(n, int):
                return n
    return None


def _extract_head_sha(event_type: str, payload: dict) -> str:
    pr = payload.get("pull_request")
    if isinstance(pr, dict):
        head = pr.get("head")
        if isinstance(head, dict):
            sha = head.get("sha")
            if isinstance(sha, str):
                return sha
    cs = payload.get("check_suite")
    if isinstance(cs, dict):
        sha = cs.get("head_sha")
        if isinstance(sha, str):
            return sha
    return ""


def _payload_hash(raw_payload: dict) -> str:
    serialised = json.dumps(raw_payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialised.encode()).hexdigest()


_ACTION_REQUIRED_TRIGGER_TYPES: frozenset[str] = frozenset({
    "pull_request.synchronize",
    "pull_request.opened",
    "pull_request.review_requested",
})

_IGNORED_TRIGGER_TYPES: frozenset[str] = frozenset({
    "pull_request.labeled",
    "pull_request.unlabeled",
    "pull_request.assigned",
    "pull_request.unassigned",
    "pull_request.milestoned",
    "pull_request.demilestoned",
})


def _is_action_required(trigger_type: str, payload: dict) -> bool:
    if trigger_type in _ACTION_REQUIRED_TRIGGER_TYPES:
        return True
    if trigger_type == "check_suite.completed":
        cs = payload.get("check_suite") or {}
        return cs.get("conclusion") == "failure"
    return False


def normalize_event(
    delivery_id: str,
    event_type: str,
    raw_payload: dict,
) -> NormalizedEvent | None:
    """Normalise a raw GitHub webhook into a queue-ready event."""
    trigger_type = _trigger_type_for(event_type, raw_payload)
    if trigger_type is None:
        return None
    if trigger_type in _IGNORED_TRIGGER_TYPES:
        return None

    repo = _extract_repo(raw_payload)
    head_sha = _extract_head_sha(event_type, raw_payload)
    pr_number = _extract_pr_number(raw_payload)
    action_required = _is_action_required(trigger_type, raw_payload)
    event_id = dedupe_key(event_type, raw_payload, delivery_id)
    p_hash = _payload_hash(raw_payload)

    return NormalizedEvent(
        event_id=event_id,
        delivery_id=delivery_id,
        trigger_type=trigger_type,
        repo_full_name=repo,
        pr_number=pr_number,
        head_sha=head_sha,
        action_required=action_required,
        payload_hash=p_hash,
        enqueued_at=datetime.now(timezone.utc),
    )


# RemediationQueue SQL

_REMEDIATION_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS remediation_queue (
    event_id      TEXT PRIMARY KEY,
    delivery_id   TEXT NOT NULL,
    trigger_type  TEXT NOT NULL,
    repo_full_name TEXT NOT NULL,
    pr_number     INTEGER,
    head_sha      TEXT NOT NULL,
    action_required INTEGER NOT NULL,
    payload_hash  TEXT NOT NULL,
    enqueued_at   TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'PENDING',
    updated_at    REAL,
    next_retry_at REAL
);
"""

_MIGRATE_COLUMNS_SQL = [
    "ALTER TABLE remediation_queue ADD COLUMN updated_at REAL",
    "ALTER TABLE remediation_queue ADD COLUMN next_retry_at REAL",
]

_INSERT_EVENT_SQL = """
INSERT OR IGNORE INTO remediation_queue
    (event_id, delivery_id, trigger_type, repo_full_name, pr_number,
     head_sha, action_required, payload_hash, enqueued_at, attempt_count, status)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_SELECT_PENDING_SQL = """
SELECT event_id, delivery_id, trigger_type, repo_full_name, pr_number,
       head_sha, action_required, payload_hash, enqueued_at, attempt_count, status
FROM remediation_queue
WHERE status = 'PENDING'
  AND (next_retry_at IS NULL OR next_retry_at <= ?)
ORDER BY enqueued_at ASC
LIMIT ?
"""

_UPDATE_TO_IN_PROGRESS_SQL = """
UPDATE remediation_queue
SET status = 'IN_PROGRESS', updated_at = ?
WHERE event_id IN ({placeholders})
  AND status = 'PENDING'
"""

_UPDATE_STATUS_SQL = """
UPDATE remediation_queue
SET status = ?,
    attempt_count = attempt_count + ?,
    updated_at = ?
WHERE event_id = ?
"""

_UPDATE_STATUS_WITH_RETRY_SQL = """
UPDATE remediation_queue
SET status = ?,
    attempt_count = attempt_count + ?,
    updated_at = ?,
    next_retry_at = ?
WHERE event_id = ?
"""

_MARK_STALE_SQL = """
UPDATE remediation_queue
SET status = 'STALE', updated_at = ?
WHERE status IN ('PENDING', 'IN_PROGRESS')
  AND COALESCE(updated_at, CAST(strftime('%s', enqueued_at) AS REAL)) <= ?
"""

_COUNT_STALE_CHANGES_SQL = "SELECT changes()"


class RemediationQueue:
    """SQLite-backed queue for remediation events."""

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path or str(_DEFAULT_DB_PATH)
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)

    def init_schema(self) -> None:
        """Create the queue table if it does not already exist."""
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.execute(_REMEDIATION_CREATE_TABLE_SQL)
            for sql in _MIGRATE_COLUMNS_SQL:
                try:
                    conn.execute(sql)
                except sqlite3.OperationalError:
                    pass
            conn.commit()

    def enqueue(self, event: NormalizedEvent) -> bool:
        """Insert event into the queue. Returns True if inserted, False if already present."""
        now = time.time()
        with closing(sqlite3.connect(self._db_path)) as conn:
            cursor = conn.execute(
                _INSERT_EVENT_SQL,
                (
                    event.event_id,
                    event.delivery_id,
                    event.trigger_type,
                    event.repo_full_name,
                    event.pr_number,
                    event.head_sha,
                    int(event.action_required),
                    event.payload_hash,
                    event.enqueued_at.isoformat(),
                    event.attempt_count,
                    str(event.status),
                ),
            )
            if cursor.rowcount > 0:
                conn.execute(
                    "UPDATE remediation_queue SET updated_at = ? WHERE event_id = ?",
                    (now, event.event_id),
                )
            conn.commit()
            return cursor.rowcount > 0

    def dequeue_pending(self, limit: int = 10) -> list[NormalizedEvent]:
        """Atomically select and mark up to limit PENDING events as IN_PROGRESS."""
        now = time.time()
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(_SELECT_PENDING_SQL, (now, limit)).fetchall()
            if rows:
                event_ids = [r[0] for r in rows]
                placeholders = ",".join("?" for _ in event_ids)
                sql = _UPDATE_TO_IN_PROGRESS_SQL.format(placeholders=placeholders)
                conn.execute(sql, [now, *event_ids])
            conn.commit()
        return [self._row_to_event(row) for row in rows]

    def update_status(
        self,
        event_id: str,
        status: QueueStatus,
        attempt_delta: int = 0,
        next_retry_at: float | None = None,
    ) -> None:
        """Update the status for event_id."""
        now = time.time()
        with closing(sqlite3.connect(self._db_path)) as conn:
            if next_retry_at is not None:
                conn.execute(
                    _UPDATE_STATUS_WITH_RETRY_SQL,
                    (str(status), attempt_delta, now, next_retry_at, event_id),
                )
            else:
                conn.execute(_UPDATE_STATUS_SQL, (str(status), attempt_delta, now, event_id))
            conn.commit()

    def mark_stale(self, max_age_hours: int = 24) -> int:
        """Mark PENDING/IN_PROGRESS events older than max_age_hours as STALE."""
        now = time.time()
        cutoff = now - (max_age_hours * 3600)
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.execute(_MARK_STALE_SQL, (now, cutoff))
            count: int = conn.execute(_COUNT_STALE_CHANGES_SQL).fetchone()[0]
            conn.commit()
        return count

    @staticmethod
    def _row_to_event(row: tuple) -> NormalizedEvent:  # type: ignore[type-arg]
        (
            event_id, delivery_id, trigger_type, repo_full_name, pr_number,
            head_sha, action_required, payload_hash, enqueued_at_str,
            attempt_count, status_str,
        ) = row
        enqueued_at = datetime.fromisoformat(enqueued_at_str)
        return NormalizedEvent(
            event_id=event_id,
            delivery_id=delivery_id,
            trigger_type=trigger_type,
            repo_full_name=repo_full_name,
            pr_number=pr_number,
            head_sha=head_sha,
            action_required=bool(action_required),
            payload_hash=payload_hash,
            enqueued_at=enqueued_at,
            attempt_count=attempt_count,
            status=QueueStatus(status_str),
        )


# =============================================================================
# PART 3: Worker - PR lock, retry budget, event dispatch
# =============================================================================

class DispatchError(Exception):
    """Raised when dispatch of a remediation event fails."""


_CREATE_LOCK_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS pr_locks (
    pr_key      TEXT PRIMARY KEY,
    locked_at   REAL NOT NULL
);
"""

_ACQUIRE_SQL = "INSERT OR IGNORE INTO pr_locks (pr_key, locked_at) VALUES (?, ?)"
_RELEASE_SQL = "DELETE FROM pr_locks WHERE pr_key = ?"
_CHECK_SQL = "SELECT locked_at FROM pr_locks WHERE pr_key = ?"


class PRLock:
    """Per-PR advisory mutex backed by a SQLite table."""

    stale_lock_seconds: float = 300.0

    def __init__(self, db_path: str | None = None) -> None:
        resolved = Path(db_path) if db_path is not None else Path(_DEFAULT_DB_PATH)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = str(resolved)
        self._local = threading.local()
        self._init_schema()

    def _init_schema(self) -> None:
        with closing(self._connect()) as conn:
            conn.execute(_CREATE_LOCK_TABLE_SQL)
            conn.commit()

    def acquire(self, pr_key: str, timeout_seconds: float = 30.0) -> bool:
        """Try to acquire the lock for pr_key."""
        held = self._held_keys()
        if pr_key in held:
            return False

        deadline = time.monotonic() + timeout_seconds
        poll_interval = 0.05

        while True:
            now = time.time()
            with closing(self._connect()) as conn:
                cursor = conn.execute(_ACQUIRE_SQL, (pr_key, now))
                if cursor.rowcount > 0:
                    conn.commit()
                    held.add(pr_key)
                    return True
                row = conn.execute(_CHECK_SQL, (pr_key,)).fetchone()
                if row is not None:
                    locked_at = row[0]
                    if now - locked_at > self.stale_lock_seconds:
                        conn.execute(_RELEASE_SQL, (pr_key,))
                        cursor2 = conn.execute(_ACQUIRE_SQL, (pr_key, now))
                        if cursor2.rowcount > 0:
                            conn.commit()
                            held.add(pr_key)
                            return True
                conn.commit()

            if time.monotonic() >= deadline:
                return False
            time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))

    def release(self, pr_key: str) -> None:
        """Release the lock for pr_key."""
        with closing(self._connect()) as conn:
            conn.execute(_RELEASE_SQL, (pr_key,))
            conn.commit()
        self._held_keys().discard(pr_key)

    @contextmanager
    def for_key(self, pr_key: str, timeout_seconds: float = 30.0) -> Generator[bool, None, None]:
        """Context manager: acquire lock on enter, release on exit."""
        acquired = self.acquire(pr_key, timeout_seconds=timeout_seconds)
        try:
            yield acquired
        finally:
            if acquired:
                self.release(pr_key)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)

    def _held_keys(self) -> set[str]:
        if not hasattr(self._local, "held"):
            self._local.held = set()
        return self._local.held  # type: ignore[return-value]


@dataclass
class RetryBudget:
    """Configuration for retry limits and exponential backoff delays."""
    max_attempts: int = 3
    backoff_seconds: list[float] = field(default_factory=lambda: [5, 30, 120])

    def next_delay(self, attempt: int) -> float:
        """Return the backoff delay for attempt (0-indexed)."""
        if not self.backoff_seconds:
            return 0.0
        idx = min(attempt, len(self.backoff_seconds) - 1)
        return self.backoff_seconds[idx]


_WORKER_SYMPHONY_DIR = str(Path.home() / ".symphony")
_WORKER_WORKFLOW_ROOT = str(Path.home() / ".openclaw" / "workspace")


class RemediationWorker:
    """Dequeue remediation events, acquire PR lock, dispatch via Symphony."""

    def __init__(
        self,
        queue: RemediationQueue,
        budget: RetryBudget | None = None,
        db_path: str | None = None,
    ) -> None:
        self._queue = queue
        self._budget = budget or RetryBudget()
        effective_db = db_path or getattr(queue, "_db_path", None) or str(_DEFAULT_DB_PATH)
        self._lock = PRLock(db_path=effective_db)

    def process_one(self, event: NormalizedEvent) -> bool:
        """Acquire PR lock, dispatch event, release lock."""
        pr_key = f"{event.repo_full_name}:{event.pr_number}"
        acquired = self._lock.acquire(pr_key, timeout_seconds=0.5)
        if not acquired:
            return False
        try:
            self._dispatch(event)
            return True
        finally:
            self._lock.release(pr_key)

    def run_once(self, limit: int = 10) -> tuple[int, int]:
        """Process up to limit PENDING events. Returns (processed, failed)."""
        events = self._queue.dequeue_pending(limit=limit)
        processed = 0
        failed = 0

        for event in events:
            self._queue.update_status(event.event_id, QueueStatus.IN_PROGRESS)

            lock_timeout = False
            try:
                success = self.process_one(event)
                if not success:
                    lock_timeout = True
            except DispatchError:
                success = False

            if success:
                self._queue.update_status(
                    event.event_id,
                    QueueStatus.DONE,
                    attempt_delta=1,
                )
                processed += 1
            elif lock_timeout:
                retry_at = time.time() + self._budget.next_delay(0)
                self._queue.update_status(
                    event.event_id,
                    QueueStatus.PENDING,
                    attempt_delta=0,
                    next_retry_at=retry_at,
                )
            else:
                new_attempt_count = event.attempt_count + 1
                retry_at = time.time() + self._budget.next_delay(event.attempt_count)
                if new_attempt_count >= self._budget.max_attempts:
                    new_status = QueueStatus.FAILED
                    self._queue.update_status(
                        event.event_id,
                        new_status,
                        attempt_delta=1,
                    )
                else:
                    new_status = QueueStatus.PENDING
                    self._queue.update_status(
                        event.event_id,
                        new_status,
                        attempt_delta=1,
                        next_retry_at=retry_at,
                    )
                failed += 1

        return processed, failed

    def _dispatch(self, event: NormalizedEvent) -> None:
        """Build workflow + runner script for event via symphony_daemon."""
        try:
            from orchestration.symphony_daemon import build_workflow, build_runner_script

            task_line = (
                f"Remediate {event.trigger_type} on "
                f"{event.repo_full_name} PR#{event.pr_number} "
                f"@ {event.head_sha[:12]}"
            )
            workflow_content = build_workflow(
                workspace_root=_WORKER_WORKFLOW_ROOT,
                workflow_title="PR Remediation",
                workflow_intro="Automated remediation triggered by webhook event.",
                task_lines=[task_line],
                requirements=["Follow existing code patterns.", "Do not break tests."],
            )
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".yml", delete=False
            ) as workflow_tmp:
                workflow_tmp.write(workflow_content)
                workflow_path = workflow_tmp.name

            script_content = build_runner_script(
                symphony_elixir_dir=_WORKER_SYMPHONY_DIR,
                workflow_path=workflow_path,
                node_name=f"worker@{event.event_id[:16]}",
                cookie="openclaw",
                port=4000,
            )
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".sh", delete=False
            ) as tmp:
                tmp.write(script_content)
                tmp_path = tmp.name

            try:
                result = subprocess.run(
                    ["/bin/bash", tmp_path],
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
            finally:
                os.unlink(tmp_path)
                os.unlink(workflow_path)

            if result.returncode != 0:
                raise DispatchError(
                    f"runner script failed (rc={result.returncode}): {result.stderr[:500]}"
                )
        except DispatchError:
            raise
        except Exception as exc:
            raise DispatchError(f"symphony dispatch failed: {exc}") from exc


# =============================================================================
# PART 4: Inline Metrics (counters only - no external dependencies)
# =============================================================================

# Simple thread-safe counters using a lock
_metrics_lock = threading.Lock()
_metrics: dict[str, int] = {
    "webhooks_received": 0,
    "webhooks_invalid_sig": 0,
    "webhooks_deduped": 0,
    "events_enqueued": 0,
    "events_dispatched": 0,
    "events_failed": 0,
    "events_stale": 0,
    "reconciler_stuck_reset": 0,
    "reconciler_stale_reset": 0,
}


def inc_metric(name: str, amount: int = 1) -> None:
    """Increment a metric counter."""
    with _metrics_lock:
        _metrics[name] = _metrics.get(name, 0) + amount


def get_metrics() -> dict[str, int]:
    """Get a snapshot of all metrics."""
    with _metrics_lock:
        return dict(_metrics)


def reset_metrics() -> None:
    """Reset all metrics to zero (preserves keys so callers don't get KeyError)."""
    with _metrics_lock:
        for key in _metrics:
            _metrics[key] = 0


# =============================================================================
# PART 5: Daemon - supervised ingress+worker process
# =============================================================================

DEFAULT_PORT = 19888
DEFAULT_LOG_DIR = Path.home() / ".openclaw" / "logs"
WORKER_POLL_INTERVAL = 5.0


def _setup_logging(log_dir: Path) -> logging.Logger:
    """Configure logging to file and console."""
    log_dir.mkdir(parents=True, exist_ok=True)

    log_file = log_dir / "webhook.log"
    err_file = log_dir / "webhook.err.log"

    logger = logging.getLogger("webhook")
    logger.setLevel(logging.INFO)

    fh = logging.FileHandler(log_file)
    fh.setLevel(logging.DEBUG)
    fh_format = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    fh.setFormatter(fh_format)

    eh = logging.FileHandler(err_file)
    eh.setLevel(logging.ERROR)
    eh.setFormatter(fh_format)

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fh_format)

    logger.addHandler(fh)
    logger.addHandler(eh)
    logger.addHandler(ch)

    return logger


class WebhookDaemon:
    """Combined ingress + worker daemon."""

    def __init__(
        self,
        port: int = DEFAULT_PORT,
        db_path: str = str(_DEFAULT_DB_PATH),
        log_dir: Path = DEFAULT_LOG_DIR,
    ) -> None:
        self._port = port
        self._db_path = db_path
        self._log_dir = log_dir
        self._logger = _setup_logging(log_dir)

        self._ingress: WebhookIngress | None = None
        self._store: WebhookStore | None = None
        self._queue: RemediationQueue | None = None
        self._worker: RemediationWorker | None = None

        self._running = False
        self._shutdown_event = threading.Event()

    def _init_components(self) -> None:
        """Initialize all components."""
        self._logger.info(f"Initializing webhook daemon (port={self._port}, db={self._db_path})")

        self._store = WebhookStore(db_path=self._db_path)
        self._store.init_schema()
        self._ingress = WebhookIngress(
            host="127.0.0.1",
            port=self._port,
            db_path=self._db_path,
        )

        self._queue = RemediationQueue(db_path=self._db_path)
        self._queue.init_schema()

        self._worker = RemediationWorker(queue=self._queue)

        self._logger.info("Components initialized")

    def _poll_and_process(self) -> None:
        """Poll ingress store, normalize events, process via worker."""
        if not self._store or not self._queue or not self._worker:
            return

        try:
            records = self._store.get_unprocessed(limit=10)

            for record in records:
                try:
                    payload = json.loads(record.payload)

                    # Normalize and enqueue
                    normalized = normalize_event(
                        delivery_id=record.delivery_id,
                        event_type=record.event_type,
                        raw_payload=payload,
                    )
                    if normalized:
                        self._queue.enqueue(normalized)
                        inc_metric("events_enqueued")
                        self._logger.info(
                            f"Enqueued event {normalized.event_id} from {record.delivery_id}"
                        )

                    self._store.mark_processed(record.delivery_id)
                    inc_metric("webhooks_received")

                except json.JSONDecodeError as e:
                    self._logger.error(f"Failed to parse webhook {record.delivery_id}: {e}")
                    self._store.mark_processed(record.delivery_id)
                except Exception as e:
                    self._logger.error(f"Error processing webhook {record.delivery_id}: {e}")
                    self._store.mark_processed(record.delivery_id)

            if self._queue:
                processed, failed = self._worker.run_once(limit=5)
                if processed > 0 or failed > 0:
                    self._logger.info(f"Worker processed={processed}, failed={failed}")
                    inc_metric("events_dispatched", processed)
                    inc_metric("events_failed", failed)

        except Exception as e:
            self._logger.error(f"Error in poll_and_process: {e}")

    def _worker_loop(self) -> None:
        """Background thread: poll and process webhooks."""
        self._logger.info("Worker loop started")
        while self._running:
            self._poll_and_process()
            self._shutdown_event.wait(timeout=WORKER_POLL_INTERVAL)
        self._logger.info("Worker loop stopped")

    def _run_ingress(self) -> None:
        """Run the HTTP ingress server (blocking)."""
        self._logger.info(f"Starting ingress server on port {self._port}")
        try:
            self._ingress.serve_forever()
        except Exception as e:
            self._logger.error(f"Ingress server error: {e}")
            raise

    def start(self) -> None:
        """Start the daemon (ingress + worker thread)."""
        if self._running:
            self._logger.warning("Daemon already running")
            return

        self._init_components()
        self._running = True
        self._shutdown_event.clear()

        worker_thread = threading.Thread(target=self._worker_loop, name="webhook-worker")
        worker_thread.daemon = True
        worker_thread.start()

        self._logger.info("Webhook daemon started")

        try:
            self._run_ingress()
        except KeyboardInterrupt:
            self._logger.info("Received interrupt, shutting down...")
        finally:
            self.stop()

    def stop(self) -> None:
        """Stop the daemon gracefully."""
        if not self._running:
            return

        self._logger.info("Stopping webhook daemon...")
        self._running = False
        self._shutdown_event.set()

        if self._ingress is not None:
            self._ingress.shutdown()

        self._logger.info("Webhook daemon stopped")

    def restart(self) -> None:
        """Restart the daemon."""
        self._logger.info("Restarting webhook daemon...")
        self.stop()
        time.sleep(1)
        self.start()


# Signal handling
_daemon_instance: WebhookDaemon | None = None


def _signal_handler(signum: int, frame) -> None:  # noqa: ANN001
    """Handle shutdown signals."""
    sig_name = signal.Signals(signum).name
    print(f"Received {sig_name}, shutting down...")
    if _daemon_instance:
        _daemon_instance.stop()
    sys.exit(0)


def main() -> None:
    """Main entrypoint for the webhook daemon."""
    global _daemon_instance

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    port = int(os.environ.get("WEBHOOK_PORT", DEFAULT_PORT))
    db_path = os.environ.get("WEBHOOK_DB_PATH", str(_DEFAULT_DB_PATH))
    log_dir = Path(os.environ.get("WEBHOOK_LOG_DIR", str(DEFAULT_LOG_DIR)))

    daemon = WebhookDaemon(
        port=port,
        db_path=db_path,
        log_dir=log_dir,
    )
    _daemon_instance = daemon

    try:
        daemon.start()
    except Exception as e:
        print(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
