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

    def save(self) -> None:
        """Atomically save budget state to JSON file with locking.

        Uses write-to-temp-then-rename for atomicity. Reloads latest state
        before saving to handle concurrent modifications.
        """
        if not self._budget_path:
            return  # In-memory budget, no persistence needed
        
        lock_file = self._acquire_lock()
        try:
            # Reload latest state from file to handle concurrent modifications
            if self._budget_path.exists():
                with open(self._budget_path) as f:
                    data = json.load(f)

                # Merge loaded subtasks with local changes (local takes precedence for same key)
                loaded_subtasks = data.get("subtasks", {})
                for subtask_id, entry_data in loaded_subtasks.items():
                    if subtask_id not in self._subtasks:
                        self._subtasks[subtask_id] = BudgetEntry.from_dict(entry_data)

                # Merge loaded tasks
                loaded_tasks = data.get("tasks", {})
                for task_id, entry_data in loaded_tasks.items():
                    if task_id not in self._tasks:
                        self._tasks[task_id] = TaskEntry.from_dict(entry_data, task_id=task_id)

            # Now save merged state
            data = {
                "subtasks": {
                    subtask_id: entry.to_dict()
                    for subtask_id, entry in self._subtasks.items()
                },
                "tasks": {task_id: entry.to_dict() for task_id, entry in self._tasks.items()},
            }

            # Atomic write: write to temp file then rename
            dir_path = self._budget_path.parent
            dir_path.mkdir(parents=True, exist_ok=True)

            # Create temp file with unique name to avoid concurrent conflicts
            temp_fd, temp_path = tempfile.mkstemp(
                dir=dir_path, prefix=".fb_", suffix=".tmp"
            )
            try:
                with os.fdopen(temp_fd, "w") as f:
                    json.dump(data, f)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temp_path, self._budget_path)
            except Exception:
                # Clean up temp file on failure
                if os.path.exists(temp_path):
                    try:
                        os.unlink(temp_path)
                    except OSError:
                        pass
                raise
        finally:
            self._release_lock(lock_file)

    def record_escalation(
        self, subtask_id: str, task_id: str, reaction_key: str
    ) -> None:
        """Record an escalation attempt for a subtask.

        Args:
            subtask_id: The subtask identifier
            task_id: The parent task identifier
            reaction_key: The reaction key that triggered escalation (unused but kept for API)
        """
        if subtask_id in self._subtasks:
            entry = self._subtasks[subtask_id]
            entry.attempts += 1
        else:
            self._subtasks[subtask_id] = BudgetEntry(
                subtask_id=subtask_id,
                task_id=task_id,
                attempts=1,
                first_escalation=datetime.now(timezone.utc).isoformat(),
            )
        self.save()

    def record_strategy_change(self, task_id: str) -> None:
        """Record a strategy change at task level.

        Args:
            task_id: The task identifier
        """
        if task_id in self._tasks:
            self._tasks[task_id].strategy_changes += 1
        else:
            self._tasks[task_id] = TaskEntry(task_id=task_id, strategy_changes=1)
        self.save()

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
