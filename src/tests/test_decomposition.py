"""Tests for decomposition_dispatcher: spawn AO sessions for subtasks.

These tests verify:
- Given subtask list -> spawns AO session for each via ao_spawn
- Parallel spawns limited to configurable max (default: 4)
- Failed spawn -> retry once, then mark subtask as blocked
- All sessions spawned -> task tracker updated with session IDs
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

# These imports will fail until decomposition_dispatcher.py is implemented (TDD)
from orchestration.decomposition_dispatcher import (
    DecompositionDispatcher,
    DispatchResult,
    dispatch_subtasks,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_state_dir(tmp_path) -> Path:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    return state_dir


@pytest.fixture
def tasks_file(temp_state_dir) -> Path:
    return temp_state_dir / "tasks.json"


@pytest.fixture
def mock_ao_cli():
    cli = MagicMock()
    cli.spawn = MagicMock()
    return cli


# ---------------------------------------------------------------------------
# Test: Spawn AO session for each subtask
# ---------------------------------------------------------------------------


def test_dispatch_spawns_session_for_each_subtask(temp_state_dir, tasks_file, mock_ao_cli) -> None:
    """Each subtask should result in an AO session being spawned."""
    from orchestration.task_tracker import TaskTracker

    tracker = TaskTracker(tasks_path=tasks_file)
    task_id = tracker.create_task("Implement feature", ["S1", "S2", "S3"])

    mock_ao_cli.spawn.side_effect = ["session-001", "session-002", "session-003"]

    dispatcher = DecompositionDispatcher(tracker=tracker, ao_cli=mock_ao_cli, max_parallel=4)
    results = dispatcher.dispatch_subtasks(task_id)

    assert len(results) == 3
    for result in results:
        assert result.session_id is not None
        assert result.error is None


def test_dispatch_links_sessions_to_subtasks(temp_state_dir, tasks_file, mock_ao_cli) -> None:
    """Dispatched sessions should be linked to their subtasks in tracker."""
    from orchestration.task_tracker import TaskTracker

    tracker = TaskTracker(tasks_path=tasks_file)
    task_id = tracker.create_task("Test task", ["S1", "S2"])

    mock_ao_cli.spawn.side_effect = ["session-x", "session-y"]

    dispatcher = DecompositionDispatcher(tracker=tracker, ao_cli=mock_ao_cli, max_parallel=4)
    dispatcher.dispatch_subtasks(task_id)

    task = tracker.get_task(task_id)
    linked = {st.subtask_id: st.ao_session_id for st in task.subtasks}

    assert linked[task.subtasks[0].subtask_id] == "session-x"
    assert linked[task.subtasks[1].subtask_id] == "session-y"


# ---------------------------------------------------------------------------
# Test: Parallel spawns limited to configurable max (default: 4)
# ---------------------------------------------------------------------------


def test_parallel_limit_default_four(temp_state_dir, tasks_file, mock_ao_cli) -> None:
    """Default max_parallel should be 4."""
    from orchestration.task_tracker import TaskTracker

    tracker = TaskTracker(tasks_path=tasks_file)
    task_id = tracker.create_task("Test task", ["S1", "S2", "S3", "S4", "S5"])

    mock_ao_cli.spawn.side_effect = lambda *a, **kw: f"session-{mock_ao_cli.spawn.call_count + 1}"

    dispatcher = DecompositionDispatcher(tracker=tracker, ao_cli=mock_ao_cli)
    dispatcher.dispatch_subtasks(task_id)

    assert mock_ao_cli.spawn.call_count == 5


def test_parallel_limit_custom_value(temp_state_dir, tasks_file, mock_ao_cli) -> None:
    """max_parallel should be configurable."""
    from orchestration.task_tracker import TaskTracker

    tracker = TaskTracker(tasks_path=tasks_file)
    task_id = tracker.create_task("Test task", ["S1", "S2", "S3"])

    mock_ao_cli.spawn.side_effect = lambda *a, **kw: f"session-{mock_ao_cli.spawn.call_count + 1}"

    dispatcher = DecompositionDispatcher(tracker=tracker, ao_cli=mock_ao_cli, max_parallel=2)
    dispatcher.dispatch_subtasks(task_id)

    assert mock_ao_cli.spawn.call_count == 3


# ---------------------------------------------------------------------------
# Test: Failed spawn -> retry once, then mark subtask as blocked
# ---------------------------------------------------------------------------


def test_retry_on_first_spawn_failure(temp_state_dir, tasks_file, mock_ao_cli) -> None:
    """Transient failure on first spawn should be retried once."""
    from orchestration.task_tracker import TaskTracker

    tracker = TaskTracker(tasks_path=tasks_file)
    task_id = tracker.create_task("Test task", ["S1"])

    # OSError is a transient failure type that triggers a retry
    mock_ao_cli.spawn.side_effect = [OSError("network error"), "session-retry"]

    dispatcher = DecompositionDispatcher(tracker=tracker, ao_cli=mock_ao_cli, max_parallel=4)
    results = dispatcher.dispatch_subtasks(task_id)

    assert results[0].session_id == "session-retry"
    assert results[0].error is None
    assert mock_ao_cli.spawn.call_count == 2


def test_mark_blocked_after_retry_exhausted(temp_state_dir, tasks_file, mock_ao_cli) -> None:
    """Subtask should be marked blocked after transient retry also fails."""
    from orchestration.task_tracker import TaskTracker

    tracker = TaskTracker(tasks_path=tasks_file)
    task_id = tracker.create_task("Test task", ["S1"])

    # Both failures are transient: first triggers retry, second exhausts it
    mock_ao_cli.spawn.side_effect = [OSError("Error 1"), OSError("Error 2")]

    dispatcher = DecompositionDispatcher(tracker=tracker, ao_cli=mock_ao_cli, max_parallel=4)
    results = dispatcher.dispatch_subtasks(task_id)

    assert results[0].session_id is None
    assert results[0].error is not None
    assert results[0].blocked is True


def test_multiple_subtasks_some_blocked(temp_state_dir, tasks_file, mock_ao_cli) -> None:
    """Some subtasks can succeed while others are blocked."""
    from orchestration.task_tracker import TaskTracker

    tracker = TaskTracker(tasks_path=tasks_file)
    task_id = tracker.create_task("Test task", ["S1", "S2", "S3"])

    # S2 fails with a non-transient exception (immediately blocked, no retry).
    # Use a function-based side_effect to handle parallel execution correctly.
    call_counts: dict[str, int] = {}

    def spawn_side_effect(project_id: object, subtask_id: str) -> str:
        call_counts[subtask_id] = call_counts.get(subtask_id, 0) + 1
        if "S1" in subtask_id or subtask_id.endswith("-0"):
            return "session-001"
        if "S2" in subtask_id or subtask_id.endswith("-1"):
            raise Exception("non-transient error")  # noqa: TRY002
        return "session-003"

    mock_ao_cli.spawn.side_effect = spawn_side_effect

    dispatcher = DecompositionDispatcher(tracker=tracker, ao_cli=mock_ao_cli, max_parallel=4)
    results = dispatcher.dispatch_subtasks(task_id)

    # Results may arrive in any order due to parallel dispatch; key by subtask_id
    by_id = {r.subtask_id: r for r in results}
    s1_id = next(st.subtask_id for st in tracker.get_task(task_id).subtasks if "S1" in st.description)
    s2_id = next(st.subtask_id for st in tracker.get_task(task_id).subtasks if "S2" in st.description)
    s3_id = next(st.subtask_id for st in tracker.get_task(task_id).subtasks if "S3" in st.description)

    assert by_id[s1_id].session_id == "session-001"
    assert by_id[s1_id].blocked is False
    assert by_id[s2_id].blocked is True
    assert by_id[s3_id].session_id == "session-003"


# ---------------------------------------------------------------------------
# Test: Task tracker updated with session IDs
# ---------------------------------------------------------------------------


def test_tracker_updated_with_all_session_ids(temp_state_dir, tasks_file, mock_ao_cli) -> None:
    """All successful session IDs should be linked in tracker."""
    from orchestration.task_tracker import TaskTracker

    tracker = TaskTracker(tasks_path=tasks_file)
    task_id = tracker.create_task("Test task", ["S1", "S2", "S3"])

    mock_ao_cli.spawn.side_effect = ["A", "B", "C"]

    dispatcher = DecompositionDispatcher(tracker=tracker, ao_cli=mock_ao_cli, max_parallel=4)
    dispatcher.dispatch_subtasks(task_id)

    task = tracker.get_task(task_id)
    linked = [st.ao_session_id for st in task.subtasks if st.ao_session_id]

    assert len(linked) == 3


def test_task_status_in_progress_after_spawn(temp_state_dir, tasks_file, mock_ao_cli) -> None:
    """Task status should be updated to in_progress after spawning."""
    from orchestration.task_tracker import TaskTracker, TaskStatus

    tracker = TaskTracker(tasks_path=tasks_file)
    task_id = tracker.create_task("Test task", ["S1"])

    mock_ao_cli.spawn.return_value = "session-001"

    dispatcher = DecompositionDispatcher(tracker=tracker, ao_cli=mock_ao_cli, max_parallel=4)
    dispatcher.dispatch_subtasks(task_id)

    task = tracker.get_task(task_id)
    assert task.status == TaskStatus.IN_PROGRESS


# ---------------------------------------------------------------------------
# Test: DispatchResult dataclass and module-level function
# ---------------------------------------------------------------------------


def test_dispatch_result_dataclass() -> None:
    """DispatchResult should hold subtask_id, session_id, error, and blocked."""
    result = DispatchResult(subtask_id="st-001", session_id="sess-123", error=None, blocked=False)

    assert result.subtask_id == "st-001"
    assert result.session_id == "sess-123"
    assert result.error is None
    assert result.blocked is False


def test_dispatch_result_with_error() -> None:
    """DispatchResult should hold error information when spawn fails."""
    result = DispatchResult(subtask_id="st-001", session_id=None, error="CLI error", blocked=True)

    assert result.session_id is None
    assert result.error == "CLI error"
    assert result.blocked is True


def test_module_level_dispatch_function(temp_state_dir, tasks_file, mock_ao_cli) -> None:
    """Module-level dispatch_subtasks should work as convenience function."""
    from orchestration.task_tracker import TaskTracker

    tracker = TaskTracker(tasks_path=tasks_file)
    task_id = tracker.create_task("Test task", ["S1"])

    mock_ao_cli.spawn.return_value = "session-001"

    results = dispatch_subtasks(task_id=task_id, tracker=tracker, ao_cli=mock_ao_cli, max_parallel=4)

    assert len(results) == 1
    assert results[0].session_id == "session-001"


# ---------------------------------------------------------------------------
# Test: Edge cases
# ---------------------------------------------------------------------------


def test_empty_subtask_list(temp_state_dir, tasks_file, mock_ao_cli) -> None:
    """Dispatching task with no subtasks should return empty list."""
    from orchestration.task_tracker import TaskTracker

    tracker = TaskTracker(tasks_path=tasks_file)
    task_id = tracker.create_task("Empty task", [])

    dispatcher = DecompositionDispatcher(tracker=tracker, ao_cli=mock_ao_cli, max_parallel=4)
    results = dispatcher.dispatch_subtasks(task_id)

    assert results == []
    mock_ao_cli.spawn.assert_not_called()


def test_max_parallel_must_be_positive(temp_state_dir, tasks_file) -> None:
    """max_parallel must be a positive integer."""
    from orchestration.task_tracker import TaskTracker

    tracker = TaskTracker(tasks_path=tasks_file)

    ao_cli = MagicMock()
    with pytest.raises(ValueError):
        DecompositionDispatcher(tracker=tracker, ao_cli=ao_cli, max_parallel=0)
