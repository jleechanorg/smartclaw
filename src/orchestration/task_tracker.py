"""Task tracker for cross-session task and subtask state management.

This module provides the TaskTracker class which handles:
- Creating tasks with subtasks - each subtask gets its own budget
- Linking subtasks to AO session IDs - track which AO session is working on which subtask
- Marking subtasks complete when AO session reaches `merged` - react to AO events
- Task complete when all subtasks complete - aggregate status
- Task failed when any subtask exhausts budget + escalation - propagate failure
- Persist to JSON file - atomic write pattern (write to temp then rename)
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

from orchestration.subtask_events import (
    SubtaskEventType,
    emit_subtask_event,
)


class TaskStatus(StrEnum):
    """Task status enum."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class SubtaskStatus(StrEnum):
    """Subtask status enum."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class Subtask:
    """Subtask within a task."""

    subtask_id: str
    description: str
    ao_session_id: str | None = None
    status: str = SubtaskStatus.PENDING
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "subtask_id": self.subtask_id,
            "description": self.description,
            "status": self.status,
            "ao_session_id": self.ao_session_id,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Subtask:
        """Create Subtask from dictionary."""
        return cls(
            subtask_id=data["subtask_id"],
            description=data["description"],
            status=data.get("status", SubtaskStatus.PENDING),
            ao_session_id=data.get("ao_session_id"),
            created_at=data.get("created_at", datetime.now(timezone.utc).isoformat()),
        )


@dataclass
class Task:
    """Task containing subtasks."""

    task_id: str
    description: str
    subtasks: list[Subtask] = field(default_factory=list)
    status: str = TaskStatus.PENDING
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "task_id": self.task_id,
            "description": self.description,
            "status": self.status,
            "created_at": self.created_at,
            "subtasks": [st.to_dict() for st in self.subtasks],
        }

    @classmethod
    def from_dict(cls, data: dict) -> Task:
        """Create Task from dictionary."""
        subtasks = [Subtask.from_dict(st) for st in data.get("subtasks", [])]
        return cls(
            task_id=data["task_id"],
            description=data["description"],
            status=data.get("status", TaskStatus.PENDING),
            created_at=data.get("created_at", datetime.now(timezone.utc).isoformat()),
            subtasks=subtasks,
        )


class TaskTracker:
    """Tracks cross-session task and subtask state.

    This class persists tasks to a JSON file to survive process restarts.
    It tracks:
    - Tasks with their subtasks
    - AO session linkage to subtasks
    - Subtask completion based on AO events
    - Task completion/failure aggregation

    Usage:
        tracker = TaskTracker(tasks_path=Path("state/tasks.json"))
        task_id = tracker.create_task("Implement auth", ["Create module", "Add tests"])
        tracker.link_session(subtask_id, "ao-session-123")
        tracker.update_from_ao_event("ao-session-123", "merged")
    """

    def __init__(self, tasks_path: Path) -> None:
        """Initialize TaskTracker, loading from JSON or creating new.

        Args:
            tasks_path: Path to the JSON file for persistence
        """
        self._tasks_path = tasks_path
        self._tasks: dict[str, Task] = {}
        self._lock_path = Path(str(tasks_path) + ".lock")
        self._session_to_subtask: dict[str, str] = {}  # session_id -> subtask_id
        self._load()

    def _acquire_lock(self) -> int:
        """Acquire exclusive lock on the tasks file.

        Returns:
            File descriptor for the lock file.
        """
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_path.touch()
        lock_file = open(self._lock_path, "r+")
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        return lock_file

    def _release_lock(self, lock_file) -> None:
        """Release the lock on the tasks file."""
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()

    def _load(self) -> None:
        """Load tasks state from JSON file with locking."""
        if not self._tasks_path.exists():
            return

        lock_file = self._acquire_lock()
        try:
            with open(self._tasks_path) as f:
                data = json.load(f)

            tasks_data = data.get("tasks", {})
            for task_id, task_data in tasks_data.items():
                task = Task.from_dict(task_data)
                self._tasks[task_id] = task

                # Rebuild session to subtask mapping
                for subtask in task.subtasks:
                    if subtask.ao_session_id:
                        self._session_to_subtask[subtask.ao_session_id] = subtask.subtask_id
        finally:
            self._release_lock(lock_file)

    def save(self) -> None:
        """Atomically save tasks state to JSON file with locking.

        Uses write-to-temp-then-rename for atomicity. Reloads latest state
        before saving to handle concurrent modifications.
        """
        lock_file = self._acquire_lock()
        try:
            # Reload latest state from file to handle concurrent modifications
            if self._tasks_path.exists():
                with open(self._tasks_path) as f:
                    data = json.load(f)

                loaded_tasks = data.get("tasks", {})
                for task_id, task_data in loaded_tasks.items():
                    if task_id not in self._tasks:
                        self._tasks[task_id] = Task.from_dict(task_data)

            # Save merged state
            data = {
                "tasks": {
                    task_id: task.to_dict()
                    for task_id, task in self._tasks.items()
                }
            }

            # Atomic write: write to temp file then rename
            dir_path = self._tasks_path.parent
            dir_path.mkdir(parents=True, exist_ok=True)

            temp_fd, temp_path = tempfile.mkstemp(
                dir=dir_path, prefix=".tt_", suffix=".tmp"
            )
            try:
                with os.fdopen(temp_fd, "w") as f:
                    json.dump(data, f)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temp_path, self._tasks_path)
            except Exception:
                if os.path.exists(temp_path):
                    try:
                        os.unlink(temp_path)
                    except OSError:
                        pass
                raise
        finally:
            self._release_lock(lock_file)

    def create_task(self, description: str, subtask_descriptions: list[str]) -> str:
        """Create a new task with given subtasks. Returns task_id.

        Args:
            description: Task description
            subtask_descriptions: List of subtask descriptions

        Returns:
            The generated task_id
        """
        task_id = f"task-{uuid.uuid4().hex[:8]}"

        # Create subtasks with unique IDs
        subtasks = []
        for i, subtask_desc in enumerate(subtask_descriptions):
            subtask_id = f"{task_id}-st-{i}"
            subtasks.append(Subtask(
                subtask_id=subtask_id,
                description=subtask_desc,
            ))

        # Determine initial task status
        # If no subtasks, task is immediately complete
        status = TaskStatus.COMPLETED if len(subtasks) == 0 else TaskStatus.PENDING

        task = Task(
            task_id=task_id,
            description=description,
            subtasks=subtasks,
            status=status,
        )

        self._tasks[task_id] = task
        self.save()
        return task_id

    def get_task(self, task_id: str) -> Task | None:
        """Get task by ID, or None if not found.

        Args:
            task_id: The task identifier

        Returns:
            Task if found, None otherwise
        """
        return self._tasks.get(task_id)

    def link_session(self, subtask_id: str, session_id: str) -> None:
        """Link a subtask to an AO session ID.

        Args:
            subtask_id: The subtask identifier
            session_id: The AO session identifier
        """
        # Find subtask across all tasks
        for task in self._tasks.values():
            for subtask in task.subtasks:
                if subtask.subtask_id == subtask_id:
                    subtask.ao_session_id = session_id
                    subtask.status = SubtaskStatus.IN_PROGRESS
                    self._session_to_subtask[session_id] = subtask_id

                    # Save state first (before emitting event) so event reflects persisted state
                    self.save()

                    # Emit event: subtask started (after state is persisted)
                    emit_subtask_event(
                        event_type=SubtaskEventType.STARTED,
                        task_id=task.task_id,
                        subtask_id=subtask_id,
                        session_id=session_id,
                        message=f"Subtask '{subtask.description}' started",
                    )

                    return

    def update_from_ao_event(self, session_id: str, event_type: str) -> None:
        """Update subtask status based on AO event.

        Args:
            session_id: The AO session identifier
            event_type: The event type. Values:
                - "merged" -> mark subtask COMPLETED
                - "failed" or "budget_exhausted" -> mark subtask FAILED
                - "in_progress" -> mark subtask IN_PROGRESS
                - other values -> mark subtask IN_PROGRESS
        """
        # Find subtask by session ID
        subtask_id = self._session_to_subtask.get(session_id)
        if subtask_id is None:
            return

        # Find and update subtask
        for task in self._tasks.values():
            for subtask in task.subtasks:
                if subtask.subtask_id == subtask_id:
                    old_status = subtask.status
                    if event_type == "merged":
                        subtask.status = SubtaskStatus.COMPLETED
                        emit_subtask_event(
                            event_type=SubtaskEventType.COMPLETED,
                            task_id=task.task_id,
                            subtask_id=subtask_id,
                            session_id=session_id,
                            message=f"Subtask '{subtask.description}' completed",
                        )
                    elif event_type in ("failed", "budget_exhausted"):
                        subtask.status = SubtaskStatus.FAILED
                        emit_subtask_event(
                            event_type=SubtaskEventType.FAILED,
                            task_id=task.task_id,
                            subtask_id=subtask_id,
                            session_id=session_id,
                            message=f"Subtask '{subtask.description}' failed: {event_type}",
                        )
                    else:
                        subtask.status = SubtaskStatus.IN_PROGRESS
                        # Only emit progress if status actually changed
                        if old_status != SubtaskStatus.IN_PROGRESS:
                            emit_subtask_event(
                                event_type=SubtaskEventType.PROGRESS,
                                task_id=task.task_id,
                                subtask_id=subtask_id,
                                session_id=session_id,
                                message=f"Subtask '{subtask.description}' in progress",
                            )

                    # Update task status based on subtask states
                    self._update_task_status(task)
                    self.save()
                    return

    def _update_task_status(self, task: Task) -> None:
        """Update task status based on subtask states.

        Args:
            task: The task to update
        """
        if len(task.subtasks) == 0:
            task.status = TaskStatus.COMPLETED
            return

        # Check if any subtask is failed
        any_failed = any(st.status == SubtaskStatus.FAILED for st in task.subtasks)
        if any_failed:
            task.status = TaskStatus.FAILED
            return

        # Check if all subtasks are completed
        all_completed = all(st.status == SubtaskStatus.COMPLETED for st in task.subtasks)
        if all_completed:
            task.status = TaskStatus.COMPLETED
            return

        # If we have at least one completed or in_progress, task is in_progress
        any_completed = any(st.status == SubtaskStatus.COMPLETED for st in task.subtasks)
        any_in_progress = any(st.status == SubtaskStatus.IN_PROGRESS for st in task.subtasks)
        if any_completed or any_in_progress:
            task.status = TaskStatus.IN_PROGRESS
            return

        # Otherwise pending
        task.status = TaskStatus.PENDING

    def is_complete(self, task_id: str) -> bool:
        """Check if task is complete (all subtasks COMPLETED).

        Args:
            task_id: The task identifier

        Returns:
            True if task is complete, False otherwise
        """
        task = self._tasks.get(task_id)
        if task is None:
            return False

        if len(task.subtasks) == 0:
            return True

        return all(st.status == SubtaskStatus.COMPLETED for st in task.subtasks)

    def get_tasks_by_status(self, status: str) -> list[Task]:
        """Get all tasks with given status.

        Args:
            status: The status to filter by (use TaskStatus values)

        Returns:
            List of tasks with matching status
        """
        return [task for task in self._tasks.values() if task.status == status]

    def get_subtask_by_session(self, session_id: str) -> Subtask | None:
        """Find subtask by its AO session ID.

        Args:
            session_id: The AO session identifier

        Returns:
            Subtask if found, None otherwise
        """
        subtask_id = self._session_to_subtask.get(session_id)
        if subtask_id is None:
            return None

        for task in self._tasks.values():
            for subtask in task.subtasks:
                if subtask.subtask_id == subtask_id:
                    return subtask

        return None
