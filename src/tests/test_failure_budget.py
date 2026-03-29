"""Tests for failure_budget: file-backed budget tracking with persistence.

These tests verify the FailureBudget class handles:
- JSON file load/save
- Cross-session time tracking for subtasks
- Per-subtask (30min) and per-task (2 strategy changes) budget limits
- Reset on AO session merged
- Process restart survival (file-backed)
- Concurrent access safety (atomic write)
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

# These imports will fail until failure_budget.py is implemented (TDD)
from orchestration.failure_budget import (
    FailureBudget,
    BudgetEntry,
    BudgetExceededError,
)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_state_dir(tmp_path) -> Path:
    """Create a temporary state directory for budget files."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    return state_dir


@pytest.fixture
def budget_file(temp_state_dir) -> Path:
    """Path to the failure budget JSON file."""
    return temp_state_dir / "failure_budgets.json"


@pytest.fixture
def budget(budget_file: Path) -> FailureBudget:
    """Create a FailureBudget instance backed by the temp file."""
    return FailureBudget(budget_path=budget_file)


# ---------------------------------------------------------------------------
# Test: JSON file load/save
# ---------------------------------------------------------------------------


def test_budget_saves_to_json_file(budget_file: Path, budget: FailureBudget) -> None:
    """Budget should persist to JSON file after modifications."""
    budget.record_escalation(
        subtask_id="subtask-001",
        task_id="task-001",
        reaction_key="ci-failed",
    )

    # Force save
    budget.save()

    # Verify file exists and contains data
    assert budget_file.exists()
    with open(budget_file) as f:
        data = json.load(f)

    assert "subtask-001" in data["subtasks"]
    assert data["subtasks"]["subtask-001"]["attempts"] == 1


def test_budget_loads_from_json_file(budget_file: Path) -> None:
    """Budget should load existing state from JSON file."""
    # Pre-write some data
    initial_data = {
        "subtasks": {
            "subtask-001": {
                "subtask_id": "subtask-001",
                "task_id": "task-001",
                "attempts": 2,
                "strategy_changes": 1,
                "first_escalation": "2026-03-14T10:00:00Z",
            }
        },
        "tasks": {
            "task-001": {"strategy_changes": 1}
        },
    }
    with open(budget_file, "w") as f:
        json.dump(initial_data, f)

    # Load budget from file
    loaded_budget = FailureBudget(budget_path=budget_file)

    assert loaded_budget.get_attempts("subtask-001") == 2
    assert loaded_budget.get_strategy_changes("task-001") == 1


def test_budget_creates_file_if_missing(budget_file: Path) -> None:
    """Budget should create the JSON file if it doesn't exist."""
    assert not budget_file.exists()

    budget = FailureBudget(budget_path=budget_file)
    # Trigger a save by recording something
    budget.record_escalation(
        subtask_id="subtask-001",
        task_id="task-001",
        reaction_key="ci-failed",
    )

    assert budget_file.exists()


# ---------------------------------------------------------------------------
# Test: Cross-session time tracking for subtask
# ---------------------------------------------------------------------------


def test_track_cross_session_elapsed_time(budget: FailureBudget) -> None:
    """Budget should track total elapsed time across sessions for a subtask."""
    # Record first escalation
    budget.record_escalation(
        subtask_id="subtask-001",
        task_id="task-001",
        reaction_key="ci-failed",
    )

    # Simulate time passing (in real use, this would be across sessions)
    entry = budget._subtasks["subtask-001"]
    # Manually set first_escalation to 20 minutes ago
    entry.first_escalation = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()

    # Check elapsed time
    elapsed = budget.get_elapsed_minutes("subtask-001")
    assert elapsed >= 20


def test_subtask_timeout_30min(budget: FailureBudget) -> None:
    """Subtask should expire after 30 minutes of total elapsed time."""
    # Record escalation
    budget.record_escalation(
        subtask_id="subtask-001",
        task_id="task-001",
        reaction_key="ci-failed",
    )

    # Set first_escalation to 35 minutes ago (beyond 30min threshold)
    entry = budget._subtasks["subtask-001"]
    entry.first_escalation = (datetime.now(timezone.utc) - timedelta(minutes=35)).isoformat()

    # Should be considered expired
    assert budget.is_subtask_expired("subtask-001", timeout_minutes=30) is True


def test_subtask_not_expired_within_30min(budget: FailureBudget) -> None:
    """Subtask should NOT be expired within 30 minutes."""
    # Record escalation
    budget.record_escalation(
        subtask_id="subtask-001",
        task_id="task-001",
        reaction_key="ci-failed",
    )

    # Set first_escalation to 20 minutes ago (within threshold)
    entry = budget._subtasks["subtask-001"]
    entry.first_escalation = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()

    # Should NOT be considered expired
    assert budget.is_subtask_expired("subtask-001", timeout_minutes=30) is False


# ---------------------------------------------------------------------------
# Test: Per-task strategy change budget
# ---------------------------------------------------------------------------


def test_task_exhausted_after_2_strategy_changes(budget: FailureBudget) -> None:
    """Task should be exhausted after 2 strategy changes."""
    # Record 2 strategy changes
    budget.record_strategy_change("task-001")
    budget.record_strategy_change("task-001")

    assert budget.is_task_exhausted("task-001", max_changes=2) is True


def test_task_not_exhausted_within_limit(budget: FailureBudget) -> None:
    """Task should NOT be exhausted within strategy change limit."""
    # Record 1 strategy change
    budget.record_strategy_change("task-001")

    assert budget.is_task_exhausted("task-001", max_changes=2) is False


def test_strategy_change_tracked_across_subtasks(budget: FailureBudget) -> None:
    """Strategy changes should be tracked at task level, not per subtask."""
    # Record escalations on different subtasks
    budget.record_escalation("subtask-001", "task-001", "ci-failed")
    budget.record_escalation("subtask-002", "task-001", "ci-failed")

    # Both should share the same task's strategy change count
    budget.record_strategy_change("task-001")

    # Task-level strategy changes should be tracked
    assert budget.get_strategy_changes("task-001") == 1


# ---------------------------------------------------------------------------
# Test: Reset budget when subtask reaches merged
# ---------------------------------------------------------------------------


def test_reset_subtask_on_merge(budget: FailureBudget) -> None:
    """Budget should reset when subtask's AO session reaches merged."""
    # Record some failures
    budget.record_escalation("subtask-001", "task-001", "ci-failed")
    budget.record_escalation("subtask-001", "task-001", "ci-failed")

    assert budget.get_attempts("subtask-001") == 2

    # Simulate merge event
    budget.reset_subtask("subtask-001")

    # Budget should be reset
    assert budget.get_attempts("subtask-001") == 0


def test_reset_subtask_removes_entry_completely(budget: FailureBudget) -> None:
    """Reset should remove the subtask entry entirely."""
    budget.record_escalation("subtask-001", "task-001", "ci-failed")
    assert "subtask-001" in budget._subtasks

    budget.reset_subtask("subtask-001")

    assert "subtask-001" not in budget._subtasks


# ---------------------------------------------------------------------------
# Test: Budget survives process restart
# ---------------------------------------------------------------------------


def test_budget_persists_across_instances(budget_file: Path) -> None:
    """Budget should survive process restart - data should persist in file."""
    # Create first instance, record data, save
    budget1 = FailureBudget(budget_path=budget_file)
    budget1.record_escalation("subtask-001", "task-001", "ci-failed")
    budget1.record_strategy_change("task-001")
    budget1.save()

    # Simulate process restart - create new instance from same file
    budget2 = FailureBudget(budget_path=budget_file)

    # Should have loaded the previous state
    assert budget2.get_attempts("subtask-001") == 1
    assert budget2.get_strategy_changes("task-001") == 1


def test_budget_file_format_valid_json(budget: FailureBudget, budget_file: Path) -> None:
    """Budget file should contain valid JSON that can be parsed."""
    budget.record_escalation("subtask-001", "task-001", "ci-failed")
    budget.save()

    with open(budget_file) as f:
        content = f.read()

    # Should not raise
    parsed = json.loads(content)
    assert isinstance(parsed, dict)


# ---------------------------------------------------------------------------
# Test: Concurrent access safety (atomic write)
# ---------------------------------------------------------------------------


def test_concurrent_writes_are_atomic(budget_file: Path) -> None:
    """Concurrent writes should not corrupt the budget file."""
    budget = FailureBudget(budget_path=budget_file)
    errors: list[Exception] = []

    def writer(subtask_id: str, count: int) -> None:
        try:
            b = FailureBudget(budget_path=budget_file)
            for i in range(count):
                b.record_escalation(subtask_id, f"task-{subtask_id}", "ci-failed")
                b.save()
        except Exception as e:
            errors.append(e)

    # Run concurrent writers
    threads = [
        threading.Thread(target=writer, args=("subtask-A", 5)),
        threading.Thread(target=writer, args=("subtask-B", 5)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # No errors should occur
    assert len(errors) == 0

    # File should still be valid JSON
    with open(budget_file) as f:
        data = json.load(f)

    # Should have both subtasks
    assert "subtask-A" in data["subtasks"]
    assert "subtask-B" in data["subtasks"]


def test_read_while_writing_no_corruption(budget_file: Path) -> None:
    """Reading while writing should not return corrupted data."""
    budget = FailureBudget(budget_path=budget_file)
    budget.record_strategy_change("task-001")
    budget.save()

    errors: list[Exception] = []

    def writer() -> None:
        try:
            for i in range(10):
                b = FailureBudget(budget_path=budget_file)
                b.record_escalation(f"subtask-{i}", "task-001", "ci-failed")
                b.save()
                time.sleep(0.01)
        except Exception as e:
            errors.append(e)

    def reader() -> None:
        try:
            for _ in range(10):
                b = FailureBudget(budget_path=budget_file)
                _ = b.summary()
                time.sleep(0.01)
        except Exception as e:
            errors.append(e)

    t1 = threading.Thread(target=writer)
    t2 = threading.Thread(target=reader)

    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # No errors - particularly no JSON decode errors
    assert len(errors) == 0


# ---------------------------------------------------------------------------
# Test: Summary and reporting
# ---------------------------------------------------------------------------


def test_summary_includes_all_subtasks(budget: FailureBudget) -> None:
    """Summary should include all tracked subtasks and tasks."""
    budget.record_escalation("subtask-001", "task-001", "ci-failed")
    budget.record_strategy_change("task-001")
    budget.record_escalation("subtask-002", "task-001", "ci-failed")

    summary = budget.summary()

    assert "task-001" in summary
    assert summary["task-001"]["strategy_changes"] == 1
    assert "subtask-001" in summary["task-001"]["subtasks"]
    assert "subtask-002" in summary["task-001"]["subtasks"]


def test_summary_empty_when_no_data(budget: FailureBudget) -> None:
    """Summary should return empty dict when no data tracked."""
    summary = budget.summary()
    assert summary == {}


# ---------------------------------------------------------------------------
# Test: Edge cases
# ---------------------------------------------------------------------------


def test_record_escalation_creates_new_entry(budget: FailureBudget) -> None:
    """Recording escalation on new subtask should create new entry."""
    assert "subtask-new" not in budget._subtasks

    budget.record_escalation("subtask-new", "task-new", "ci-failed")

    assert "subtask-new" in budget._subtasks
    entry = budget._subtasks["subtask-new"]
    assert entry.attempts == 1
    assert entry.task_id == "task-new"


def test_get_attempts_nonexistent_returns_zero(budget: FailureBudget) -> None:
    """Getting attempts for nonexistent subtask should return 0."""
    assert budget.get_attempts("nonexistent") == 0


def test_get_strategy_changes_nonexistent_returns_zero(budget: FailureBudget) -> None:
    """Getting strategy changes for nonexistent task should return 0."""
    assert budget.get_strategy_changes("nonexistent") == 0


def test_reset_nonexistent_subtask_no_error(budget: FailureBudget) -> None:
    """Resetting nonexistent subtask should not raise error."""
    # Should not raise
    budget.reset_subtask("nonexistent")
