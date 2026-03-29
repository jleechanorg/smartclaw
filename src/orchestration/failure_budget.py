"""Failure budget tracking for cross-session escalation handling.

This module provides the FailureBudget class which tracks failure state
across AO sessions to implement per-subtask (30min) and per-task (2 strategy
changes) budget limits for the escalation router.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


class BudgetExceededError(Exception):
    """Raised when a budget limit is exceeded."""

    pass


@dataclass
class BudgetEntry:
    """Entry tracking failure state for a single subtask.

    Attributes:
        subtask_id: The unique identifier for the subtask
        task_id: The parent task identifier
        attempts: Number of escalation attempts for this subtask
        strategy_changes: Number of strategy changes at subtask level (for compat)
        first_escalation: ISO8601 timestamp of first escalation attempt
    """

    subtask_id: str
    task_id: str
    attempts: int = 0
    strategy_changes: int = 0
    first_escalation: str | None = None

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "subtask_id": self.subtask_id,
            "task_id": self.task_id,
            "attempts": self.attempts,
            "strategy_changes": self.strategy_changes,
            "first_escalation": self.first_escalation,
        }

    @classmethod
    def from_dict(cls, data: dict) -> BudgetEntry:
        """Create BudgetEntry from dictionary."""
        return cls(
            subtask_id=data["subtask_id"],
            task_id=data["task_id"],
            attempts=data.get("attempts", 0),
            strategy_changes=data.get("strategy_changes", 0),
            first_escalation=data.get("first_escalation"),
        )


@dataclass
class TaskEntry:
    """Entry tracking strategy changes at task level.

    Attributes:
        task_id: The unique identifier for the task
        strategy_changes: Number of strategy changes attempted
    """

    task_id: str
    strategy_changes: int = 0

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "task_id": self.task_id,
            "strategy_changes": self.strategy_changes,
        }

    @classmethod
    def from_dict(cls, data: dict, task_id: str | None = None) -> TaskEntry:
        """Create TaskEntry from dictionary.

        Args:
            data: Dictionary with task data
            task_id: Optional task_id (useful when loading from file where key is the task_id)
        """
        return cls(
            task_id=task_id or data.get("task_id", ""),
            strategy_changes=data.get("strategy_changes", 0),
        )


class FailureBudget:
    """Tracks cross-session failure state for escalation handling.

    This class persists failure budgets to a JSON file to survive process
    restarts. It tracks:
    - Per-subtask elapsed time (default 30min timeout)
    - Per-task strategy changes (default 2 max changes)

    Usage:
        budget = FailureBudget(budget_path=Path("state/failure_budgets.json"))
        budget.record_escalation("subtask-001", "task-001", "ci-failed")
        if budget.is_subtask_expired("subtask-001"):
            # Handle timeout
            pass
    """

    def __init__(self, budget_path: Path | None = None) -> None:
        """Initialize FailureBudget, loading from JSON or creating new.

        Args:
            budget_path: Path to the JSON file for persistence. If None, uses
                         in-memory storage (for testing or non-persistent use).
        """
        self._budget_path = budget_path
        self._subtasks: dict[str, BudgetEntry] = {}
        self._tasks: dict[str, TaskEntry] = {}
        self._lock_path = Path(str(budget_path) + ".lock") if budget_path else None
        if budget_path:
            self._load()

    def _acquire_lock(self) -> int:
        """Acquire exclusive lock on the budget file.

        Returns:
            File descriptor for the lock file. Returns -1 for in-memory budgets.
        """
        if not self._lock_path:
            return -1  # In-memory mode, no lock needed
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_path.touch()
        lock_file = open(self._lock_path, "r+")
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        return lock_file

    def _release_lock(self, lock_file) -> None:
        """Release the lock on the budget file."""
        if lock_file == -1:
            return  # In-memory mode, no lock to release
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()

    def _load(self) -> None:
        """Load budget state from JSON file with locking."""
        if not self._budget_path or not self._budget_path.exists():
            return

        lock_file = self._acquire_lock()
        try:
            with open(self._budget_path) as f:
                data = json.load(f)

            # Load subtasks
            subtasks_data = data.get("subtasks", {})
            for subtask_id, entry_data in subtasks_data.items():
                self._subtasks[subtask_id] = BudgetEntry.from_dict(entry_data)

            # Load tasks
            tasks_data = data.get("tasks", {})
            for task_id, entry_data in tasks_data.items():
                self._tasks[task_id] = TaskEntry.from_dict(entry_data, task_id=task_id)
        finally:
            self._release_lock(lock_file)

    def _write_locked(
        self,
        subtasks: dict[str, BudgetEntry],
        tasks: dict[str, TaskEntry],
    ) -> None:
        """Atomically write *subtasks* and *tasks* to the budget file.

        Must be called while the caller already holds the exclusive file lock.
        Uses write-to-temp-then-rename for atomicity.
        """
        data = {
            "subtasks": {sid: e.to_dict() for sid, e in subtasks.items()},
            "tasks": {tid: e.to_dict() for tid, e in tasks.items()},
        }
        dir_path = self._budget_path.parent  # type: ignore[union-attr]
        dir_path.mkdir(parents=True, exist_ok=True)
        temp_fd, temp_path = tempfile.mkstemp(dir=dir_path, prefix=".fb_", suffix=".tmp")
        try:
            with os.fdopen(temp_fd, "w") as f:
                json.dump(data, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, self._budget_path)  # type: ignore[arg-type]
        except Exception:
            if os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
            raise

    def _load_from_disk_locked(self) -> tuple[dict[str, BudgetEntry], dict[str, TaskEntry]]:
        """Read and return the current on-disk budget state.

        Must be called while the caller already holds the exclusive file lock.
        Returns (subtasks, tasks) dicts populated from disk, or empty dicts if
        the file does not exist yet.
        """
        subtasks: dict[str, BudgetEntry] = {}
        tasks: dict[str, TaskEntry] = {}
        if self._budget_path and self._budget_path.exists():
            try:
                with open(self._budget_path) as f:
                    data = json.load(f)
                for sid, entry_data in data.get("subtasks", {}).items():
                    subtasks[sid] = BudgetEntry.from_dict(entry_data)
                for tid, entry_data in data.get("tasks", {}).items():
                    tasks[tid] = TaskEntry.from_dict(entry_data, task_id=tid)
            except (json.JSONDecodeError, OSError):
                pass  # Corrupt file — start from empty; will be overwritten below
        return subtasks, tasks

    def save(self) -> None:
        """Atomically save the current in-memory budget state to the JSON file.

        Acquires the exclusive file lock, merges the latest on-disk state with
        the in-memory state (disk wins for keys that exist only on disk; in-memory
        wins for keys mutated locally), then writes atomically.
        """
        if not self._budget_path:
            return  # In-memory budget, no persistence needed

        lock_file = self._acquire_lock()
        try:
            # Read the freshest on-disk state while holding the lock so we do
            # not silently drop concurrent writes from other processes.
            disk_subtasks, disk_tasks = self._load_from_disk_locked()

            # Merge: disk provides the base; in-memory local mutations overlay it.
            merged_subtasks = {**disk_subtasks, **self._subtasks}
            merged_tasks = {**disk_tasks, **self._tasks}

            # Persist merged state and update the in-memory cache so subsequent
            # in-process reads reflect the latest data.
            self._write_locked(merged_subtasks, merged_tasks)
            self._subtasks = merged_subtasks
            self._tasks = merged_tasks
        finally:
            self._release_lock(lock_file)

    def record_escalation(
        self, subtask_id: str, task_id: str, reaction_key: str
    ) -> None:
        """Record an escalation attempt for a subtask.

        Reads the latest on-disk state inside the exclusive file lock before
        incrementing the attempt counter, so concurrent writers cannot overwrite
        each other's updates.

        Args:
            subtask_id: The subtask identifier
            task_id: The parent task identifier
            reaction_key: The reaction key that triggered escalation (unused but kept for API)
        """
        if not self._budget_path:
            # In-memory mode: mutate directly, no locking needed.
            if subtask_id in self._subtasks:
                self._subtasks[subtask_id].attempts += 1
            else:
                self._subtasks[subtask_id] = BudgetEntry(
                    subtask_id=subtask_id,
                    task_id=task_id,
                    attempts=1,
                    first_escalation=datetime.now(timezone.utc).isoformat(),
                )
            return

        lock_file = self._acquire_lock()
        try:
            # Read the authoritative on-disk state while holding the lock.
            disk_subtasks, disk_tasks = self._load_from_disk_locked()

            # Apply the increment on top of the freshest disk data.
            if subtask_id in disk_subtasks:
                disk_subtasks[subtask_id].attempts += 1
            else:
                disk_subtasks[subtask_id] = BudgetEntry(
                    subtask_id=subtask_id,
                    task_id=task_id,
                    attempts=1,
                    first_escalation=datetime.now(timezone.utc).isoformat(),
                )

            # Merge remaining in-memory tasks (other processes may not have them).
            merged_tasks = {**disk_tasks, **self._tasks}

            self._write_locked(disk_subtasks, merged_tasks)

            # Update the in-memory cache to reflect the committed state.
            self._subtasks = disk_subtasks
            self._tasks = merged_tasks
        finally:
            self._release_lock(lock_file)

    def record_strategy_change(self, task_id: str) -> None:
        """Record a strategy change at task level.

        Reads the latest on-disk state inside the exclusive file lock before
        incrementing the strategy-change counter, so concurrent writers cannot
        overwrite each other's updates.

        Args:
            task_id: The task identifier
        """
        if not self._budget_path:
            # In-memory mode: mutate directly, no locking needed.
            if task_id in self._tasks:
                self._tasks[task_id].strategy_changes += 1
            else:
                self._tasks[task_id] = TaskEntry(task_id=task_id, strategy_changes=1)
            return

        lock_file = self._acquire_lock()
        try:
            # Read the authoritative on-disk state while holding the lock.
            disk_subtasks, disk_tasks = self._load_from_disk_locked()

            # Apply the increment on top of the freshest disk data.
            if task_id in disk_tasks:
                disk_tasks[task_id].strategy_changes += 1
            else:
                disk_tasks[task_id] = TaskEntry(task_id=task_id, strategy_changes=1)

            # Merge remaining in-memory subtasks.
            merged_subtasks = {**disk_subtasks, **self._subtasks}

            self._write_locked(merged_subtasks, disk_tasks)

            # Update the in-memory cache to reflect the committed state.
            self._subtasks = merged_subtasks
            self._tasks = disk_tasks
        finally:
            self._release_lock(lock_file)

    def is_subtask_expired(self, subtask_id: str, timeout_minutes: int = 30) -> bool:
        """Check if subtask has exceeded the timeout threshold.

        Args:
            subtask_id: The subtask identifier
            timeout_minutes: Timeout threshold in minutes (default 30)

        Returns:
            True if elapsed time exceeds timeout, False otherwise
        """
        if subtask_id not in self._subtasks:
            return False

        elapsed = self.get_elapsed_minutes(subtask_id)
        return elapsed >= timeout_minutes

    def is_task_exhausted(self, task_id: str, max_changes: int = 2) -> bool:
        """Check if task has exhausted its strategy change budget.

        Args:
            task_id: The task identifier
            max_changes: Maximum allowed strategy changes (default 2)

        Returns:
            True if strategy changes exceed limit, False otherwise
        """
        changes = self.get_strategy_changes(task_id)
        return changes >= max_changes

    def reset_subtask(self, subtask_id: str) -> None:
        """Remove subtask entry (called on merge).

        Args:
            subtask_id: The subtask identifier to reset
        """
        self._subtasks.pop(subtask_id, None)

    def summary(self) -> dict:
        """Return nested dict of all tracked state.

        Returns:
            Dict with task-level strategy changes and nested subtask entries
        """
        result = {}

        # Group subtasks by task_id
        for subtask_id, entry in self._subtasks.items():
            task_id = entry.task_id
            if task_id not in result:
                result[task_id] = {
                    "strategy_changes": self.get_strategy_changes(task_id),
                    "subtasks": {},
                }
            result[task_id]["subtasks"][subtask_id] = entry.to_dict()

        return result

    def get_attempts(self, subtask_id: str) -> int:
        """Get attempt count for a subtask.

        Args:
            subtask_id: The subtask identifier

        Returns:
            Number of attempts, or 0 if not found
        """
        if subtask_id not in self._subtasks:
            return 0
        return self._subtasks[subtask_id].attempts

    def get_strategy_changes(self, task_id: str) -> int:
        """Get strategy change count for a task.

        Args:
            task_id: The task identifier

        Returns:
            Number of strategy changes, or 0 if not found
        """
        if task_id not in self._tasks:
            return 0
        return self._tasks[task_id].strategy_changes

    def get_elapsed_minutes(self, subtask_id: str) -> float:
        """Compute elapsed time from first_escalation.

        Args:
            subtask_id: The subtask identifier

        Returns:
            Elapsed time in minutes, or 0 if no first_escalation set
        """
        if subtask_id not in self._subtasks:
            return 0.0

        first_escalation = self._subtasks[subtask_id].first_escalation
        if first_escalation is None:
            return 0.0

        # Parse ISO8601 timestamp
        first_time = datetime.fromisoformat(first_escalation.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        elapsed = now - first_time
        return elapsed.total_seconds() / 60.0
