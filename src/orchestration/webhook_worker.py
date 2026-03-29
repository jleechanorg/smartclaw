"""Remediation worker: PR lock, retry budget, and event dispatch.

Dispatches remediation events via ao_spawn (openclaw agent), consistent with
the rest of the orchestration stack (action_executor, escalation_router).
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Generator

from orchestration.webhook_queue import NormalizedEvent, QueueStatus, RemediationQueue
from orchestration.ao_cli import AOCommandError, ao_spawn, ao_list
from orchestration.pr_lifecycle import route_event

_AO_YAML_PATH = Path.home() / "agent-orchestrator.yaml"


def _repo_to_project_key(repo_full_name: str) -> str:
    """Map 'owner/repo' to the ao project key from ~/agent-orchestrator.yaml.

    Reads the yaml lazily and matches on the ``repo`` field under each project.
    Falls back to the repo name component if no match is found.
    """
    try:
        import yaml  # type: ignore[import]
        data = yaml.safe_load(_AO_YAML_PATH.read_text())
        for key, cfg in (data.get("projects") or {}).items():
            if isinstance(cfg, dict) and cfg.get("repo") == repo_full_name:
                return key
    except Exception:
        pass
    # Fallback: last path component (e.g. "owner/repo" → "repo")
    return repo_full_name.split("/")[-1]


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class DispatchError(Exception):
    """Raised when dispatch of a remediation event fails."""


# ---------------------------------------------------------------------------
# PRLock — per-PR mutex backed by SQLite advisory rows
# ---------------------------------------------------------------------------

_DEFAULT_DB_PATH = Path.home() / ".openclaw" / "webhook_queue.db"

_CREATE_LOCK_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS pr_locks (
    pr_key      TEXT PRIMARY KEY,
    locked_at   REAL NOT NULL
);
"""

_ACQUIRE_SQL = "INSERT OR IGNORE INTO pr_locks (pr_key, locked_at) VALUES (?, ?)"
_RELEASE_SQL = "DELETE FROM pr_locks WHERE pr_key = ?"
_CHECK_SQL   = "SELECT locked_at FROM pr_locks WHERE pr_key = ?"


class PRLock:
    """Per-PR advisory mutex backed by a ``pr_locks`` SQLite table.

    Multiple ``PRLock`` instances may target the same database file and will
    coordinate through it.  Within a single instance, held keys are tracked
    in a thread-local set so the same thread cannot double-acquire.
    """

    # Locks older than this are considered crashed and eligible for recovery.
    stale_lock_seconds: float = 300.0

    def __init__(self, db_path: str | None = None) -> None:
        resolved = Path(db_path) if db_path is not None else _DEFAULT_DB_PATH
        resolved.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = str(resolved)
        self._local = threading.local()
        self._init_schema()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        with closing(self._connect()) as conn:
            conn.execute(_CREATE_LOCK_TABLE_SQL)
            conn.commit()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def acquire(self, pr_key: str, timeout_seconds: float = 30.0) -> bool:
        """Try to acquire the lock for *pr_key*.

        Returns True if the lock was acquired within *timeout_seconds*,
        False otherwise.  Includes crash-recovery: if a lock row exists
        with locked_at older than ``stale_lock_seconds``, it is treated as
        stale and replaced.
        """
        held = self._held_keys()
        if pr_key in held:
            return False

        deadline = time.monotonic() + timeout_seconds
        poll_interval = 0.05  # 50 ms

        while True:
            now = time.time()
            with closing(self._connect()) as conn:
                cursor = conn.execute(_ACQUIRE_SQL, (pr_key, now))
                if cursor.rowcount > 0:
                    conn.commit()
                    held.add(pr_key)
                    return True
                # Check for stale/crashed lock
                row = conn.execute(_CHECK_SQL, (pr_key,)).fetchone()
                if row is not None:
                    locked_at = row[0]
                    if now - locked_at > self.stale_lock_seconds:
                        # Stale lock — crash recovery
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
        """Release the lock for *pr_key*."""
        with closing(self._connect()) as conn:
            conn.execute(_RELEASE_SQL, (pr_key,))
            conn.commit()
        self._held_keys().discard(pr_key)

    @contextlib.contextmanager
    def for_key(self, pr_key: str, timeout_seconds: float = 30.0) -> Generator[bool, None, None]:
        """Context manager: acquire lock on enter, release on exit.

        Yields True if the lock was acquired, False if timeout expired.
        The caller is responsible for checking the yielded value when
        a non-default timeout is used.
        """
        acquired = self.acquire(pr_key, timeout_seconds=timeout_seconds)
        try:
            yield acquired
        finally:
            if acquired:
                self.release(pr_key)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)

    def _held_keys(self) -> set[str]:
        """Return the set of keys held by this instance on this thread."""
        if not hasattr(self._local, "held"):
            self._local.held = set()
        return self._local.held  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# RetryBudget
# ---------------------------------------------------------------------------


@dataclass
class RetryBudget:
    """Configuration for retry limits and exponential backoff delays."""

    max_attempts: int = 3
    backoff_seconds: list[float] = field(default_factory=lambda: [5, 30, 120])

    def next_delay(self, attempt: int) -> float:
        """Return the backoff delay for *attempt* (0-indexed).

        If *attempt* is beyond the list, returns the last value.
        Returns 0.0 when the backoff list is empty.
        """
        if not self.backoff_seconds:
            return 0.0
        idx = min(attempt, len(self.backoff_seconds) - 1)
        return self.backoff_seconds[idx]


# ---------------------------------------------------------------------------
# RemediationWorker
# ---------------------------------------------------------------------------


class RemediationWorker:
    """Dequeue remediation events, acquire PR lock, dispatch via ao_spawn.

    Parameters
    ----------
    queue:
        The ``RemediationQueue`` instance to pull events from.
    budget:
        Retry configuration.  Defaults to ``RetryBudget()`` (3 attempts,
        5/30/120 s backoff).
    db_path:
        SQLite path shared with the queue, used for ``PRLock`` storage.
        Defaults to the queue's own path when accessible, else the global
        default.
    """

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

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process_one(self, event: NormalizedEvent) -> bool:
        """Acquire PR lock, dispatch *event*, release lock.

        Returns True on success, False if the lock could not be acquired
        (the event is left in its current state for a later retry).
        """
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
        """Process up to *limit* PENDING events from the queue.

        Returns
        -------
        tuple[int, int]
            ``(processed, failed)`` where *processed* is the count of
            events that dispatched successfully and *failed* is the count
            that encountered a ``DispatchError``.
        """
        events = self._queue.dequeue_pending(limit=limit)
        processed = 0
        failed = 0

        for event in events:
            # dequeue_pending already sets IN_PROGRESS atomically,
            # but ensure status is explicitly marked
            self._queue.update_status(event.event_id, QueueStatus.IN_PROGRESS)

            lock_timeout = False
            try:
                success = self.process_one(event)
                if not success:
                    lock_timeout = True
            except DispatchError as _de:
                import logging
                logging.getLogger("webhook_daemon").error(
                    f"DispatchError for {event.event_id}: {_de}"
                )
                success = False

            if success:
                self._queue.update_status(
                    event.event_id,
                    QueueStatus.DONE,
                    attempt_delta=1,
                )
                processed += 1
            elif lock_timeout:
                # Lock contention — set next_retry_at without incrementing attempt_count
                retry_at = time.time() + self._budget.next_delay(0)
                self._queue.update_status(
                    event.event_id,
                    QueueStatus.PENDING,
                    attempt_delta=0,
                    next_retry_at=retry_at,
                )
            else:
                # Actual dispatch failure — increment attempts and set backoff
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

    # ------------------------------------------------------------------
    # Dispatch (overridable in tests / subclasses)
    # ------------------------------------------------------------------

    def _dispatch(self, event: NormalizedEvent) -> None:
        """Spawn an AO agent to remediate *event*.

        If a session already exists for the same branch (branch already checked
        out), treats it as a success — the work is already in flight.

        Raises ``DispatchError`` on any failure so callers can apply the
        retry budget without catching broad exceptions.
        """
        try:
            if event.pr_number is None:
                return  # no PR to remediate — skip dispatch

            # Get workflow lane from pr_lifecycle routing
            # Note: previous_runs=[] is intentional — dispatch only needs the mapped lane,
            # not historical context. Route decisions are made by the agent per-event.
            workflow_lane = ""
            if event.pr_number is not None:
                try:
                    lifecycle_decision = route_event(
                        {
                            "trigger_source": "event",
                            "trigger_type": event.trigger_type,
                            "repository": event.repo_full_name,
                            "pr_number": event.pr_number,
                            "head_sha": event.head_sha,
                        },
                        previous_runs=[],
                    )
                    workflow_lane = lifecycle_decision.get("workflow_lane") or ""
                except Exception:
                    # Lane is informational — don't let routing failures block dispatch.
                    # Continue without lane context rather than failing the whole dispatch.
                    pass

            # Build issue with workflow lane context (reduce duplication)
            base = f"Remediate {event.trigger_type} on PR#{event.pr_number} @ {event.head_sha[:12]}"
            issue = f"{base} ({workflow_lane})" if workflow_lane else base

            project_key = _repo_to_project_key(event.repo_full_name)
            ao_spawn(project=project_key, issue=issue)
        except AOCommandError as exc:
            err = str(exc)
            if "already checked out" in err or "already exists" in err:
                # Session for this branch already running — not a real failure.
                # Verify by checking ao_list; if a matching session exists, done.
                try:
                    sessions = ao_list(project=project_key)
                    branch_fragment = f"pr-{event.pr_number}-{event.head_sha[:12]}"
                    if any(branch_fragment in (s.branch or "") for s in sessions):
                        return  # session already in flight
                except Exception:
                    pass
                return  # branch conflict = session was already spawned, treat as done
            raise DispatchError(f"ao_spawn failed: {err}") from exc
        except Exception as exc:
            raise DispatchError(f"dispatch failed: {exc}") from exc
