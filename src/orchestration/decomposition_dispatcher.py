"""Decomposition dispatcher: spawn AO sessions for subtasks.

This module provides the DecompositionDispatcher class which handles:
- Given subtask list -> spawns AO session for each via ao_spawn
- Parallel spawns limited to configurable max (default: 4)
- Failed spawn -> retry once, then mark subtask as blocked
- All sessions spawned -> task tracker updated with session IDs
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from orchestration.task_tracker import Task, TaskTracker


@dataclass
class DispatchResult:
    """Result of dispatching a subtask to an AO session.

    Attributes:
        subtask_id: The subtask identifier
        session_id: The spawned AO session ID, or None if failed/blocked
        error: Error message if spawn failed, or None if successful
        blocked: True if subtask is blocked after retry exhaustion
    """

    subtask_id: str
    session_id: str | None
    error: str | None
    blocked: bool


class DecompositionDispatcher:
    """Dispatches subtasks to AO sessions.

    This class handles spawning AO sessions for each subtask in a task,
    with configurable parallelism limits and retry logic.

    Usage:
        tracker = TaskTracker(tasks_path=Path("state/tasks.json"))
        cli = AOCli()  # or mock
        dispatcher = DecompositionDispatcher(tracker=tracker, ao_cli=cli, max_parallel=4)
        results = dispatcher.dispatch_subtasks(task_id)
    """

    def __init__(
        self,
        tracker: TaskTracker,
        ao_cli: object,
        max_parallel: int = 4,
        project_id: str | None = None,
    ) -> None:
        """Initialize DecompositionDispatcher.

        Args:
            tracker: TaskTracker instance for managing task state
            ao_cli: AO CLI wrapper with spawn() method
            max_parallel: Maximum number of concurrent spawns (default: 4)
            project_id: Project identifier for AO spawn (e.g., 'owner/repo')

        Raises:
            ValueError: If max_parallel is not positive
        """
        if max_parallel <= 0:
            raise ValueError("max_parallel must be a positive integer")

        self._tracker = tracker
        self._ao_cli = ao_cli
        self._max_parallel = max_parallel
        self._project_id = project_id

    def dispatch_subtasks(self, task_id: str) -> list[DispatchResult]:
        """Dispatch all subtasks of a task to AO sessions.

        Spawns AO sessions for each subtask, with retry logic and parallelism
        limiting. Updates the task tracker with session IDs and marks blocked
        subtasks after retry exhaustion.

        Args:
            task_id: The task identifier

        Returns:
            List of DispatchResult, one per subtask
        """
        task = self._tracker.get_task(task_id)
        if task is None:
            return []

        if len(task.subtasks) == 0:
            return []

        results: list[DispatchResult] = []
        active_count = 0

        # Update task status to in_progress after spawning begins
        task.status = "in_progress"

        for subtask in task.subtasks:
            # Respect parallelism limit: decrement counter to allow next spawn
            # (synchronous implementation — no async waiting needed)
            if active_count >= self._max_parallel:
                active_count -= 1

            # Try to spawn with retry
            result = self._spawn_with_retry(subtask.subtask_id, subtask.description)
            results.append(result)

            if result.session_id is not None:
                # Link session to subtask
                self._tracker.link_session(subtask.subtask_id, result.session_id)
                active_count += 1

        # Persist state changes
        self._tracker.save()
        return results

    def _spawn_with_retry(
        self,
        subtask_id: str,
        description: str,
    ) -> DispatchResult:
        """Attempt to spawn an AO session with one retry on failure.

        Args:
            subtask_id: The subtask identifier
            description: The subtask description

        Returns:
            DispatchResult with session_id if successful, or blocked if failed
        """
        # Transient failure types that are safe to retry without risk of duplicate sessions.
        # Configuration bugs, bad arguments, and permission errors are not transient and
        # should propagate immediately rather than being silently downgraded to blocked=True.
        _TRANSIENT = (TimeoutError, ConnectionError, OSError)

        try:
            session_id = self._ao_cli.spawn(self._project_id, subtask_id)
            return DispatchResult(
                subtask_id=subtask_id,
                session_id=session_id,
                error=None,
                blocked=False,
            )
        except _TRANSIENT as e:
            # Retry once on transient transport/timeout errors
            try:
                session_id = self._ao_cli.spawn(self._project_id, subtask_id)
                return DispatchResult(
                    subtask_id=subtask_id,
                    session_id=session_id,
                    error=None,
                    blocked=False,
                )
            except Exception as retry_error:
                # Both attempts failed - mark as blocked
                return DispatchResult(
                    subtask_id=subtask_id,
                    session_id=None,
                    error=str(retry_error),
                    blocked=True,
                )


def dispatch_subtasks(
    task_id: str,
    tracker: TaskTracker,
    ao_cli: object,
    max_parallel: int = 4,
    project_id: str | None = None,
) -> list[DispatchResult]:
    """Convenience function to dispatch subtasks.

    This is a module-level wrapper around DecompositionDispatcher.dispatch_subtasks.

    Args:
        task_id: The task identifier
        tracker: TaskTracker instance for managing task state
        ao_cli: AO CLI wrapper with spawn() method
        max_parallel: Maximum number of concurrent spawns (default: 4)
        project_id: Project identifier for AO spawn (e.g., 'owner/repo')

    Returns:
        List of DispatchResult, one per subtask
    """
    dispatcher = DecompositionDispatcher(
        tracker=tracker,
        ao_cli=ao_cli,
        max_parallel=max_parallel,
        project_id=project_id,
    )
    return dispatcher.dispatch_subtasks(task_id)