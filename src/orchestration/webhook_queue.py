"""Remediation queue: event normalization, dedupe keys, and SQLite-backed queue.

This module defines the stable contract between the webhook ingress layer
(ORCH-cbo.6.1) and the remediation worker (ORCH-cbo.6.3).  It is
intentionally dependency-free (stdlib only).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

from orchestration.event_util import trigger_type_for
from orchestration.gh_integration import extract_head_sha, extract_pr_number, extract_repo


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class QueueStatus(StrEnum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    DONE = "DONE"
    FAILED = "FAILED"
    STALE = "STALE"


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Dedupe key
# ---------------------------------------------------------------------------

def dedupe_key(event_type: str, payload: dict, delivery_id: str = "") -> str:
    """Generate a stable dedupe key for an event payload.

    Formats:
    - PR events:     ``pr:{repo}:{pr_number}:{head_sha}:{trigger_type}``
    - check_suite:   ``cs:{repo}:{head_sha}:{check_suite_id}``
    - fallback:      ``misc:{delivery_id}``  (delivery_id from X-GitHub-Delivery header)
    """
    repo = extract_repo(payload)

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


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _payload_hash(raw_payload: dict) -> str:
    serialised = json.dumps(raw_payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialised.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Action-required classification
# ---------------------------------------------------------------------------

_ACTION_REQUIRED_TRIGGER_TYPES: frozenset[str] = frozenset({
    "pull_request.synchronize",
    "pull_request.opened",
    "pull_request.review_requested",
})

_IGNORED_TRIGGER_TYPES: frozenset[str] = frozenset({
    "pull_request.closed",
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


# ---------------------------------------------------------------------------
# normalize_event
# ---------------------------------------------------------------------------

def normalize_event(
    delivery_id: str,
    event_type: str,
    raw_payload: dict,
) -> NormalizedEvent | None:
    """Normalise a raw GitHub webhook into a queue-ready event.

    Returns None for events that require no remediation action (e.g.
    ``pull_request.labeled``).
    """
    trigger_type = trigger_type_for(event_type, raw_payload)
    if trigger_type is None:
        return None

    # Drop events we explicitly ignore
    if trigger_type in _IGNORED_TRIGGER_TYPES:
        return None

    repo = extract_repo(raw_payload)
    head_sha = extract_head_sha(raw_payload)
    pr_number = extract_pr_number(raw_payload)
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


# ---------------------------------------------------------------------------
# RemediationQueue
# ---------------------------------------------------------------------------

_DEFAULT_DB_PATH = Path.home() / ".openclaw" / "webhook_queue.db"

_CREATE_TABLE_SQL = """
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

_INSERT_SQL = """
INSERT OR IGNORE INTO remediation_queue
    (event_id, delivery_id, trigger_type, repo_full_name, pr_number,
     head_sha, action_required, payload_hash, enqueued_at, attempt_count, status)
VALUES
    (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
    """SQLite-backed queue for remediation events.

    Parameters
    ----------
    db_path:
        Path to the SQLite database file.  Defaults to
        ``~/.openclaw/webhook_queue.db``.
    """

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path or str(_DEFAULT_DB_PATH)
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def init_schema(self) -> None:
        """Create the queue table if it does not already exist."""
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.execute(_CREATE_TABLE_SQL)
            for sql in _MIGRATE_COLUMNS_SQL:
                try:
                    conn.execute(sql)
                except sqlite3.OperationalError:
                    pass  # column already exists
            conn.commit()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def enqueue(self, event: NormalizedEvent) -> bool:
        """Insert *event* into the queue.

        Returns
        -------
        bool
            ``True`` if the event was inserted, ``False`` if an event with
            the same ``event_id`` already exists (idempotent).
        """
        now = time.time()
        with closing(sqlite3.connect(self._db_path)) as conn:
            cursor = conn.execute(
                _INSERT_SQL,
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
            # Set updated_at on insert
            if cursor.rowcount > 0:
                conn.execute(
                    "UPDATE remediation_queue SET updated_at = ? WHERE event_id = ?",
                    (now, event.event_id),
                )
            conn.commit()
            return cursor.rowcount > 0

    def dequeue_pending(self, limit: int = 10) -> list[NormalizedEvent]:
        """Atomically select and mark up to *limit* PENDING events as IN_PROGRESS.

        Uses BEGIN IMMEDIATE to prevent TOCTOU races between concurrent workers.
        """
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
        """Update the status (and optionally increment attempt_count) for *event_id*."""
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
        """Mark PENDING/IN_PROGRESS events older than *max_age_hours* as STALE.

        Uses updated_at for age calculation. Returns the number of rows updated.
        """
        now = time.time()
        cutoff = now - (max_age_hours * 3600)
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.execute(_MARK_STALE_SQL, (now, cutoff))
            count: int = conn.execute(_COUNT_STALE_CHANGES_SQL).fetchone()[0]
            conn.commit()
        return count

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

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
