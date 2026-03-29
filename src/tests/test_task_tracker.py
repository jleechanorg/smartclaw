"""Tests for task_tracker: cross-session task and subtask state management.

These tests verify the TaskTracker class handles:
- Creating tasks with subtasks, each with its own budget
- Linking subtasks to AO session IDs
- Marking subtasks complete when AO session reaches merged
- Marking tasks complete when all subtasks complete
- Marking tasks failed when any subtask exhausts budget + escalation
- Persisting to JSON file at ~/.openclaw/state/tasks.json
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

# These imports will fail until task_tracker.py is implemented (TDD)
from orchestration.task_tracker import (
    TaskTracker,
    Task,
    Subtask,
    TaskStatus,
    SubtaskStatus,
)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_state_dir(tmp_path) -> Path:
    """Create a temporary state directory for task files."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    return state_dir


@pytest.fixture
def tasks_file(temp_state_dir) -> Path:
    """Path to the tasks JSON file."""
    return temp_state_dir / "tasks.json"


@pytest.fixture
def tracker(tasks_file: Path) -> TaskTracker:
    """Create a TaskTracker instance backed by the temp file."""
    return TaskTracker(tasks_path=tasks_file)


# ---------------------------------------------------------------------------
# Test: Task creation with subtasks
# ---------------------------------------------------------------------------


def test_create_task_with_subtasks(tracker: TaskTracker) -> None:
    """Creating a task should add it to the tracker with all subtasks."""
    task_id = tracker.create_task(
        description="Implement auth middleware",
        subtask_descriptions=["Create auth module", "Add tests", "Update docs"],
    )

    assert task_id is not None
    task = tracker.get_task(task_id)
    assert task is not None
    assert task.description == "Implement auth middleware"
    assert len(task.subtasks) == 3
    assert task.status == TaskStatus.PENDING


def test_create_task_generates_unique_ids(tracker: TaskTracker) -> None:
    """Each created task should get a unique ID."""
    task_id_1 = tracker.create_task("First task", ["Subtask 1"])
    task_id_2 = tracker.create_task("Second task", ["Subtask 2"])

    assert task_id_1 != task_id_2


def test_subtasks_have_unique_ids(tracker: TaskTracker) -> None:
    """Each subtask within a task should get a unique ID."""
    task_id = tracker.create_task(
        "Task with subtasks", ["Subtask A", "Subtask B", "Subtask C"]
    )

    task = tracker.get_task(task_id)
    subtask_ids = [st.subtask_id for st in task.subtasks]

    assert len(subtask_ids) == len(set(subtask_ids))


# ---------------------------------------------------------------------------
# Test: Link subtask to AO session ID
# ---------------------------------------------------------------------------


def test_link_subtask_to_session(tracker: TaskTracker) -> None:
    """Subtask should be linkable to an AO session ID."""
    task_id = tracker.create_task("Test task", ["Subtask 1"])
    task = tracker.get_task(task_id)
    subtask_id = task.subtasks[0].subtask_id
    session_id = "ao-session-12345"

    tracker.link_session(subtask_id, session_id)

    updated_task = tracker.get_task(task_id)
    updated_subtask = next(st for st in updated_task.subtasks if st.subtask_id == subtask_id)

    assert updated_subtask.ao_session_id == session_id


def test_link_to_nonexistent_subtask_no_error(tracker: TaskTracker) -> None:
    """Linking to nonexistent subtask should not raise error."""
    # Should not raise
    tracker.link_session("nonexistent-subtask", "session-123")


def test_link_session_updates_existing(tracker: TaskTracker) -> None:
    """Linking a session to an already-linked subtask should update it."""
    task_id = tracker.create_task("Test task", ["Subtask 1"])
    task = tracker.get_task(task_id)
    subtask_id = task.subtasks[0].subtask_id

    tracker.link_session(subtask_id, "session-1")
    tracker.link_session(subtask_id, "session-2")

    updated_task = tracker.get_task(task_id)
    updated_subtask = next(st for st in updated_task.subtasks if st.subtask_id == subtask_id)

    assert updated_subtask.ao_session_id == "session-2"


# ---------------------------------------------------------------------------
# Test: Mark subtask complete when AO session reaches merged
# ---------------------------------------------------------------------------


def test_mark_subtask_complete_on_merge(tracker: TaskTracker) -> None:
    """Subtask should be marked complete when AO session reaches merged."""
    task_id = tracker.create_task("Test task", ["Subtask 1"])
    task = tracker.get_task(task_id)
    subtask_id = task.subtasks[0].subtask_id
    session_id = "ao-session-12345"

    tracker.link_session(subtask_id, session_id)
    tracker.update_from_ao_event(session_id, "merged")

    updated_task = tracker.get_task(task_id)
    updated_subtask = next(st for st in updated_task.subtasks if st.subtask_id == subtask_id)

    assert updated_subtask.status == SubtaskStatus.COMPLETED


def test_merged_event_only_affects_linked_subtask(tracker: TaskTracker) -> None:
    """Merged event should only affect the subtask with matching session ID."""
    task_id = tracker.create_task("Test task", ["Subtask 1", "Subtask 2"])
    task = tracker.get_task(task_id)
    subtask1_id = task.subtasks[0].subtask_id
    subtask2_id = task.subtasks[1].subtask_id

    # Link only first subtask to a session
    tracker.link_session(subtask1_id, "session-1")

    # Update with merged on session-1
    tracker.update_from_ao_event("session-1", "merged")

    updated_task = tracker.get_task(task_id)
    subtask1 = next(st for st in updated_task.subtasks if st.subtask_id == subtask1_id)
    subtask2 = next(st for st in updated_task.subtasks if st.subtask_id == subtask2_id)

    assert subtask1.status == SubtaskStatus.COMPLETED
    assert subtask2.status == SubtaskStatus.PENDING


def test_multiple_subtasks_can_merge_independently(tracker: TaskTracker) -> None:
    """Multiple subtasks can reach merged status independently."""
    task_id = tracker.create_task("Test task", ["Subtask 1", "Subtask 2"])
    task = tracker.get_task(task_id)

    tracker.link_session(task.subtasks[0].subtask_id, "session-1")
    tracker.link_session(task.subtasks[1].subtask_id, "session-2")

    # After linking, both subtasks should be in progress
    task = tracker.get_task(task_id)
    assert task.subtasks[0].status == SubtaskStatus.IN_PROGRESS
    assert task.subtasks[1].status == SubtaskStatus.IN_PROGRESS

    # First subtask merges
    tracker.update_from_ao_event("session-1", "merged")

    task = tracker.get_task(task_id)
    assert task.subtasks[0].status == SubtaskStatus.COMPLETED
    assert task.subtasks[1].status == SubtaskStatus.IN_PROGRESS

    # Second subtask merges
    tracker.update_from_ao_event("session-2", "merged")

    task = tracker.get_task(task_id)
    assert task.subtasks[1].status == SubtaskStatus.COMPLETED


# ---------------------------------------------------------------------------
# Test: Task complete when all subtasks complete
# ---------------------------------------------------------------------------


def test_task_complete_when_all_subtasks_complete(tracker: TaskTracker) -> None:
    """Task should be marked complete when all subtasks are complete."""
    task_id = tracker.create_task("Test task", ["Subtask 1", "Subtask 2"])
    task = tracker.get_task(task_id)

    # Complete both subtasks
    tracker.link_session(task.subtasks[0].subtask_id, "session-1")
    tracker.link_session(task.subtasks[1].subtask_id, "session-2")

    tracker.update_from_ao_event("session-1", "merged")
    tracker.update_from_ao_event("session-2", "merged")

    final_task = tracker.get_task(task_id)

    assert final_task.status == TaskStatus.COMPLETED


def test_task_incomplete_with_pending_subtasks(tracker: TaskTracker) -> None:
    """Task should not be complete if any subtask is still pending."""
    task_id = tracker.create_task("Test task", ["Subtask 1", "Subtask 2"])
    task = tracker.get_task(task_id)

    tracker.link_session(task.subtasks[0].subtask_id, "session-1")
    tracker.update_from_ao_event("session-1", "merged")

    final_task = tracker.get_task(task_id)

    assert final_task.status == TaskStatus.IN_PROGRESS


def test_is_complete_helper(tracker: TaskTracker) -> None:
    """is_complete() should return True only when all subtasks complete."""
    task_id = tracker.create_task("Test task", ["Subtask 1", "Subtask 2"])
    task = tracker.get_task(task_id)

    assert tracker.is_complete(task_id) is False

    tracker.link_session(task.subtasks[0].subtask_id, "session-1")
    tracker.link_session(task.subtasks[1].subtask_id, "session-2")
    tracker.update_from_ao_event("session-1", "merged")
    tracker.update_from_ao_event("session-2", "merged")

    assert tracker.is_complete(task_id) is True


# ---------------------------------------------------------------------------
# Test: Task failed when subtask exhausts budget + escalation
# ---------------------------------------------------------------------------


def test_mark_subtask_failed_on_budget_exhausted(tracker: TaskTracker) -> None:
    """Subtask should be marked failed when budget is exhausted."""
    task_id = tracker.create_task("Test task", ["Subtask 1"])
    task = tracker.get_task(task_id)
    subtask_id = task.subtasks[0].subtask_id

    tracker.link_session(subtask_id, "session-1")
    tracker.update_from_ao_event("session-1", "budget_exhausted")

    updated_task = tracker.get_task(task_id)
    updated_subtask = next(st for st in updated_task.subtasks if st.subtask_id == subtask_id)

    assert updated_subtask.status == SubtaskStatus.FAILED


def test_task_failed_when_any_subtask_fails(tracker: TaskTracker) -> None:
    """Task should be marked failed when any subtask fails."""
    task_id = tracker.create_task("Test task", ["Subtask 1", "Subtask 2"])
    task = tracker.get_task(task_id)

    tracker.link_session(task.subtasks[0].subtask_id, "session-1")
    tracker.link_session(task.subtasks[1].subtask_id, "session-2")

    # First subtask exhausts budget
    tracker.update_from_ao_event("session-1", "budget_exhausted")

    final_task = tracker.get_task(task_id)

    assert final_task.status == TaskStatus.FAILED


def test_task_not_failed_until_all_subtasks_resolved(tracker: TaskTracker) -> None:
    """Task should not be failed if only one subtask is in progress."""
    task_id = tracker.create_task("Test task", ["Subtask 1", "Subtask 2"])
    task = tracker.get_task(task_id)

    tracker.link_session(task.subtasks[0].subtask_id, "session-1")
    tracker.link_session(task.subtasks[1].subtask_id, "session-2")

    # session-1 is still in progress (not merged, not failed)
    tracker.update_from_ao_event("session-1", "in_progress")

    final_task = tracker.get_task(task_id)

    assert final_task.status == TaskStatus.IN_PROGRESS


# ---------------------------------------------------------------------------
# Test: Persistence to JSON file
# ---------------------------------------------------------------------------


def test_tracker_saves_to_json_file(tracker: TaskTracker, tasks_file: Path) -> None:
    """Tracker should persist tasks to JSON file."""
    tracker.create_task("Test task", ["Subtask 1"])

    assert tasks_file.exists()

    with open(tasks_file) as f:
        data = json.load(f)

    assert len(data["tasks"]) == 1


def test_tracker_loads_from_json_file(tasks_file: Path) -> None:
    """Tracker should load existing tasks from JSON file."""
    # Pre-write some data
    initial_data = {
        "tasks": {
            "task-001": {
                "task_id": "task-001",
                "description": "Pre-existing task",
                "status": "completed",
                "created_at": "2026-03-14T10:00:00Z",
                "subtasks": [
                    {
                        "subtask_id": "subtask-001",
                        "description": "Subtask 1",
                        "status": "completed",
                        "ao_session_id": None,
                    }
                ],
            }
        }
    }
    with open(tasks_file, "w") as f:
        json.dump(initial_data, f)

    # Load tracker from file
    loaded_tracker = TaskTracker(tasks_path=tasks_file)
    task = loaded_tracker.get_task("task-001")

    assert task is not None
    assert task.description == "Pre-existing task"
    assert task.status == TaskStatus.COMPLETED


def test_tracker_creates_file_if_missing(tasks_file: Path) -> None:
    """Tracker should create the JSON file if it doesn't exist."""
    assert not tasks_file.exists()

    tracker = TaskTracker(tasks_path=tasks_file)
    tracker.create_task("New task", ["Subtask"])

    assert tasks_file.exists()


def test_tracker_persists_across_instances(tasks_file: Path) -> None:
    """Tracker data should persist across process restarts."""
    # Create first instance
    tracker1 = TaskTracker(tasks_path=tasks_file)
    task_id = tracker1.create_task("Test task", ["Subtask 1"])
    tracker1.link_session(task_id + "-st-0", "session-1")

    # Simulate process restart - create new instance
    tracker2 = TaskTracker(tasks_path=tasks_file)
    task = tracker2.get_task(task_id)

    assert task is not None
    assert task.subtasks[0].ao_session_id == "session-1"


def test_atomic_write_prevents_corruption(tasks_file: Path) -> None:
    """Concurrent writes should use atomic write pattern."""
    tracker = TaskTracker(tasks_path=tasks_file)

    errors: list[Exception] = []

    def writer(task_num: int) -> None:
        try:
            t = TaskTracker(tasks_path=tasks_file)
            t.create_task(f"Task {task_num}", [f"Subtask {task_num}"])
        except Exception as e:
            errors.append(e)

    threads = [
        threading.Thread(target=writer, args=(1,)),
        threading.Thread(target=writer, args=(2,)),
    ]

    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0

    # File should still be valid JSON
    with open(tasks_file) as f:
        data = json.load(f)

    assert len(data["tasks"]) == 2


# ---------------------------------------------------------------------------
# Test: Get tasks by various filters
# ---------------------------------------------------------------------------


def test_get_tasks_by_status(tracker: TaskTracker) -> None:
    """Should be able to filter tasks by status."""
    task1_id = tracker.create_task("Task 1", ["Subtask"])
    task2_id = tracker.create_task("Task 2", ["Subtask"])

    # Complete task1
    task = tracker.get_task(task1_id)
    tracker.link_session(task.subtasks[0].subtask_id, "session-1")
    tracker.update_from_ao_event("session-1", "merged")

    pending_tasks = tracker.get_tasks_by_status(TaskStatus.PENDING)
    completed_tasks = tracker.get_tasks_by_status(TaskStatus.COMPLETED)

    assert len(pending_tasks) == 1
    assert pending_tasks[0].task_id == task2_id
    assert len(completed_tasks) == 1
    assert completed_tasks[0].task_id == task1_id


def test_get_subtask_by_session(tracker: TaskTracker) -> None:
    """Should be able to find subtask by its AO session ID."""
    task_id = tracker.create_task("Test task", ["Subtask 1", "Subtask 2"])
    task = tracker.get_task(task_id)
    subtask_id = task.subtasks[0].subtask_id

    tracker.link_session(subtask_id, "session-123")

    found_subtask = tracker.get_subtask_by_session("session-123")

    assert found_subtask is not None
    assert found_subtask.subtask_id == subtask_id


def test_get_subtask_by_nonexistent_session(tracker: TaskTracker) -> None:
    """Getting subtask by nonexistent session should return None."""
    result = tracker.get_subtask_by_session("nonexistent-session")
    assert result is None


# ---------------------------------------------------------------------------
# Test: Edge cases
# ---------------------------------------------------------------------------


def test_create_task_with_no_subtasks(tracker: TaskTracker) -> None:
    """Creating a task with no subtasks should create an empty task."""
    task_id = tracker.create_task("Empty task", [])

    task = tracker.get_task(task_id)
    assert len(task.subtasks) == 0
    assert task.status == TaskStatus.COMPLETED  # No pending subtasks = complete


def test_update_nonexistent_session_no_error(tracker: TaskTracker) -> None:
    """Updating nonexistent session should not raise error."""
    # Should not raise
    tracker.update_from_ao_event("nonexistent-session", "merged")


def test_get_nonexistent_task_returns_none(tracker: TaskTracker) -> None:
    """Getting nonexistent task should return None."""
    result = tracker.get_task("nonexistent-task")
    assert result is None


def test_task_status_enum_values(tracker: TaskTracker) -> None:
    """Task status should have expected enum values."""
    assert TaskStatus.PENDING.value == "pending"
    assert TaskStatus.IN_PROGRESS.value == "in_progress"
    assert TaskStatus.COMPLETED.value == "completed"
    assert TaskStatus.FAILED.value == "failed"


def test_subtask_status_enum_values(tracker: TaskTracker) -> None:
    """Subtask status should have expected enum values."""
    assert SubtaskStatus.PENDING.value == "pending"
    assert SubtaskStatus.IN_PROGRESS.value == "in_progress"
    assert SubtaskStatus.COMPLETED.value == "completed"
    assert SubtaskStatus.FAILED.value == "failed"
