"""Webhook reconciler: cron-style recovery for stuck/stale queue entries.

Runs periodic passes over the RemediationQueue to:
- Reset IN_PROGRESS events that have been stuck too long
- Reset or expire stale PENDING events (checking GitHub PR state)
- Re-enqueue FAILED events within retry budget
- Expire ancient events via mark_stale
"""
from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
import time
from contextlib import closing
from dataclasses import dataclass

from orchestration.webhook_queue import QueueStatus, RemediationQueue

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config / Stats dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ReconcilerConfig:
    """Tunable knobs for the reconciler's recovery thresholds."""

    stale_pending_hours: int = 2
    """Re-examine PENDING events older than this many hours."""

    stuck_in_progress_minutes: int = 30
    """Reset IN_PROGRESS to PENDING after this many minutes with no progress."""

    max_failed_requeue: int = 1
    """Re-enqueue FAILED events up to this many times (times queue max_attempts)."""

    poll_interval_seconds: int = 300
    """Sleep duration between run_once() calls in run_loop()."""


@dataclass
class ReconcilerStats:
    """Counters returned by a single run_once() pass."""

    stale_reset: int = 0
    """PENDING events whose attempt_count was reset (PR still open)."""

    stuck_reset: int = 0
    """IN_PROGRESS events reset back to PENDING."""

    failed_requeued: int = 0
    """FAILED events reset to PENDING for another attempt."""

    gh_polls: int = 0
    """Number of GitHub API calls made during the pass."""


# ---------------------------------------------------------------------------
# GitHub PR open check (fail-open)
# ---------------------------------------------------------------------------


def check_pr_open(repo_full_name: str, pr_number: int) -> bool:
    """Return True if the GitHub PR is still open, False if closed/merged.

    Uses ``gh api`` subprocess call.  Fail-open: returns True on any error so
    that transient outages do not cause premature STALE marking.
    """
    try:
        result = subprocess.run(
            ["gh", "api", f"repos/{repo_full_name}/pulls/{pr_number}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            logger.warning(
                "gh api non-zero exit for %s#%s: %s",
                repo_full_name,
                pr_number,
                result.stderr.strip(),
            )
            return True  # fail-open
        data = json.loads(result.stdout)
        return str(data.get("state", "open")).lower() == "open"
    except Exception as exc:
        logger.warning(
            "check_pr_open failed for %s#%s: %s", repo_full_name, pr_number, exc
        )
        return True  # fail-open


# ---------------------------------------------------------------------------
# Internal SQL helpers
# ---------------------------------------------------------------------------

_SELECT_STUCK_SQL = """
SELECT event_id, repo_full_name, pr_number, attempt_count
FROM remediation_queue
WHERE status = 'IN_PROGRESS'
  AND COALESCE(updated_at, CAST(strftime('%s', enqueued_at) AS REAL)) <= ?
"""

_RESET_STUCK_SQL = """
UPDATE remediation_queue
SET status = 'PENDING', updated_at = ?
WHERE event_id = ?
  AND status = 'IN_PROGRESS'
"""

_SELECT_STALE_PENDING_SQL = """
SELECT event_id, repo_full_name, pr_number, attempt_count
FROM remediation_queue
WHERE status = 'PENDING'
  AND COALESCE(updated_at, CAST(strftime('%s', enqueued_at) AS REAL)) <= ?
"""

_SELECT_FAILED_SQL = """
SELECT event_id, repo_full_name, pr_number, attempt_count
FROM remediation_queue
WHERE status = 'FAILED'
"""

_RESET_ATTEMPT_COUNT_SQL = """
UPDATE remediation_queue
SET attempt_count = 0, updated_at = ?
WHERE event_id = ?
"""

_SET_STATUS_SQL = """
UPDATE remediation_queue
SET status = ?, updated_at = ?
WHERE event_id = ?
"""


class WebhookReconciler:
    """Periodic reconciler for the RemediationQueue.

    Parameters
    ----------
    queue:
        The queue to reconcile.
    config:
        Optional tuning config.  Uses defaults when omitted.
    """

    def __init__(
        self,
        queue: RemediationQueue,
        config: ReconcilerConfig | None = None,
    ) -> None:
        self._queue = queue
        self._config = config or ReconcilerConfig()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_once(self) -> ReconcilerStats:
        """Execute one full reconciliation pass.

        Steps:
        1. Reset stuck IN_PROGRESS → PENDING
        2. Check stale PENDING events against GitHub; reset or mark STALE
        3. Re-enqueue FAILED events within retry budget
        4. Expire ancient events via queue.mark_stale(max_age_hours=48)
        """
        stats = ReconcilerStats()
        cfg = self._config

        self._reset_stuck_in_progress(stats, cfg.stuck_in_progress_minutes)
        self._recover_stale_pending(stats, cfg.stale_pending_hours, cfg.max_failed_requeue)
        self._requeue_failed(stats, cfg.max_failed_requeue)
        self._queue.mark_stale(max_age_hours=48)

        return stats

    def run_loop(self) -> None:
        """Run run_once() in a perpetual loop, sleeping poll_interval_seconds between passes."""
        while True:
            try:
                stats = self.run_once()
                logger.info(
                    "Reconciler pass complete: stuck_reset=%d stale_reset=%d "
                    "failed_requeued=%d gh_polls=%d",
                    stats.stuck_reset,
                    stats.stale_reset,
                    stats.failed_requeued,
                    stats.gh_polls,
                )
            except Exception as exc:
                logger.exception("Reconciler pass raised an unhandled exception: %s", exc)
            time.sleep(self._config.poll_interval_seconds)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._queue._db_path)

    def _reset_stuck_in_progress(
        self, stats: ReconcilerStats, threshold_minutes: int
    ) -> None:
        """Reset IN_PROGRESS events older than threshold_minutes back to PENDING."""
        now = time.time()
        cutoff = now - (threshold_minutes * 60)
        with closing(self._connect()) as conn:
            rows = conn.execute(_SELECT_STUCK_SQL, (cutoff,)).fetchall()

        for event_id, _repo, _pr, _attempts in rows:
            now = time.time()
            with closing(self._connect()) as conn:
                cursor = conn.execute(_RESET_STUCK_SQL, (now, event_id))
                conn.commit()
                if cursor.rowcount > 0:
                    stats.stuck_reset += 1
                    logger.info("Reset stuck IN_PROGRESS event %s to PENDING", event_id)

    def _recover_stale_pending(
        self,
        stats: ReconcilerStats,
        stale_pending_hours: int,
        max_failed_requeue: int,
    ) -> None:
        """Check stale PENDING events against GitHub; reset attempt_count or mark STALE."""
        now = time.time()
        cutoff = now - (stale_pending_hours * 3600)
        with closing(self._connect()) as conn:
            rows = conn.execute(_SELECT_STALE_PENDING_SQL, (cutoff,)).fetchall()

        for event_id, repo_full_name, pr_number, _attempts in rows:
            if pr_number is not None and repo_full_name:
                still_open = check_pr_open(repo_full_name, pr_number)
                stats.gh_polls += 1
            else:
                still_open = True

            if still_open:
                now = time.time()
                with closing(self._connect()) as conn:
                    conn.execute(_RESET_ATTEMPT_COUNT_SQL, (now, event_id))
                    conn.commit()
                stats.stale_reset += 1
                logger.info("Reset attempt_count for stale PENDING event %s", event_id)
            else:
                with closing(self._connect()) as conn:
                    conn.execute(_SET_STATUS_SQL, (str(QueueStatus.STALE), time.time(), event_id))
                    conn.commit()
                logger.info("Marked stale PENDING event %s STALE (PR closed)", event_id)

    def _requeue_failed(self, stats: ReconcilerStats, max_failed_requeue: int) -> None:
        """Re-enqueue FAILED events that are within the requeue budget."""
        max_attempts_ceiling = max_failed_requeue * 3
        with closing(self._connect()) as conn:
            rows = conn.execute(_SELECT_FAILED_SQL).fetchall()

        for event_id, _repo, _pr, attempt_count in rows:
            if attempt_count < max_attempts_ceiling:
                with closing(self._connect()) as conn:
                    conn.execute(_SET_STATUS_SQL, (str(QueueStatus.PENDING), time.time(), event_id))
                    conn.commit()
                stats.failed_requeued += 1
                logger.info(
                    "Re-enqueued FAILED event %s (attempt_count=%d)", event_id, attempt_count
                )
