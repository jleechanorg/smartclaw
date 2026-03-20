"""Tests for subtask_events: SSE event streaming for real-time monitoring.

These tests verify:
- Single subscriber receives emitted events
- Multiple subscribers all receive events
- Thread safety under concurrent emit/subscribe
- SSE format correctness
"""
from __future__ import annotations

import json
import queue
import threading
import time

import pytest

from orchestration.subtask_events import (
    SubtaskEvent,
    SubtaskEventEmitter,
    SubtaskEventType,
    format_sse_event,
    get_emitter,
)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def emitter() -> SubtaskEventEmitter:
    """Create a fresh SubtaskEventEmitter for each test."""
    return SubtaskEventEmitter()


# ---------------------------------------------------------------------------
# Test: Single subscriber receives events
# ---------------------------------------------------------------------------


def test_single_subscriber_receives_event(emitter: SubtaskEventEmitter) -> None:
    """Single subscriber should receive emitted events."""
    subscriber = emitter.subscribe()

    event = SubtaskEvent(
        event_type=SubtaskEventType.STARTED,
        task_id="task-1",
        subtask_id="task-1-st-0",
        session_id="session-123",
        message="Subtask started",
    )
    emitter.emit(event)

    # Should receive the event
    received = subscriber.get(timeout=1)
    assert "subtask_started" in received
    assert "task-1" in received


def test_single_subscriber_receives_multiple_events(emitter: SubtaskEventEmitter) -> None:
    """Subscriber should receive multiple events in order."""
    subscriber = emitter.subscribe()

    events = [
        SubtaskEvent(
            event_type=SubtaskEventType.STARTED,
            task_id="task-1",
            subtask_id="task-1-st-0",
        ),
        SubtaskEvent(
            event_type=SubtaskEventType.PROGRESS,
            task_id="task-1",
            subtask_id="task-1-st-0",
            message="Processing...",
        ),
        SubtaskEvent(
            event_type=SubtaskEventType.COMPLETED,
            task_id="task-1",
            subtask_id="task-1-st-0",
            message="Done!",
        ),
    ]

    for event in events:
        emitter.emit(event)

    # Should receive all three events
    for expected in events:
        received = subscriber.get(timeout=1)
        assert expected.event_type in received


# ---------------------------------------------------------------------------
# Test: Multiple subscribers receive events
# ---------------------------------------------------------------------------


def test_multiple_subscribers_all_receive_events(emitter: SubtaskEventEmitter) -> None:
    """All subscribers should receive emitted events."""
    sub1 = emitter.subscribe()
    sub2 = emitter.subscribe()
    sub3 = emitter.subscribe()

    event = SubtaskEvent(
        event_type=SubtaskEventType.PROGRESS,
        task_id="task-1",
        subtask_id="task-1-st-0",
        message="Test message",
    )
    emitter.emit(event)

    # All subscribers should receive the event
    assert sub1.get(timeout=1) is not None
    assert sub2.get(timeout=1) is not None
    assert sub3.get(timeout=1) is not None


def test_new_subscriber_does_not_receive_past_events(emitter: SubtaskEventEmitter) -> None:
    """New subscriber should not receive events emitted before subscription."""
    # Emit an event before subscribing
    event1 = SubtaskEvent(
        event_type=SubtaskEventType.STARTED,
        task_id="task-1",
        subtask_id="task-1-st-0",
    )
    emitter.emit(event1)

    # Now subscribe
    subscriber = emitter.subscribe()

    # Emit another event after subscribing
    event2 = SubtaskEvent(
        event_type=SubtaskEventType.COMPLETED,
        task_id="task-1",
        subtask_id="task-1-st-0",
    )
    emitter.emit(event2)

    # Should only receive the second event
    received = subscriber.get(timeout=1)
    assert "subtask_completed" in received


# ---------------------------------------------------------------------------
# Test: Thread safety
# ---------------------------------------------------------------------------


def test_concurrent_emit_and_subscribe(emitter: SubtaskEventEmitter) -> None:
    """Should handle concurrent emit and subscribe operations."""
    errors: list[Exception] = []

    def emitter_thread():
        try:
            for i in range(50):
                event = SubtaskEvent(
                    event_type=SubtaskEventType.PROGRESS,
                    task_id=f"task-{i % 3}",
                    subtask_id=f"task-{i % 3}-st-{i}",
                    message=f"Event {i}",
                )
                emitter.emit(event)
                time.sleep(0.001)
        except Exception as e:
            errors.append(e)

    def subscriber_thread(sub: queue.Queue, name: str):
        try:
            received = 0
            while received < 50:
                try:
                    sub.get(timeout=2)
                    received += 1
                except queue.Empty:
                    break
        except Exception as e:
            errors.append(e)

    # Create subscribers
    sub1 = emitter.subscribe()
    sub2 = emitter.subscribe()

    # Start threads
    threads = [
        threading.Thread(target=emitter_thread),
        threading.Thread(target=subscriber_thread, args=(sub1, "sub1")),
        threading.Thread(target=subscriber_thread, args=(sub2, "sub2")),
    ]

    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0


def test_concurrent_emits_from_multiple_threads(emitter: SubtaskEventEmitter) -> None:
    """Should handle concurrent emits from multiple threads."""
    subscriber = emitter.subscribe()
    errors: list[Exception] = []

    def emit_events(thread_id: int):
        try:
            for i in range(20):
                event = SubtaskEvent(
                    event_type=SubtaskEventType.PROGRESS,
                    task_id=f"task-{thread_id}",
                    subtask_id=f"task-{thread_id}-st-{i}",
                    message=f"Thread {thread_id} event {i}",
                )
                emitter.emit(event)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=emit_events, args=(i,)) for i in range(4)]

    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0

    # Subscriber should receive all events (80 total)
    received = 0
    while True:
        try:
            subscriber.get(timeout=2)
            received += 1
        except queue.Empty:
            break

    assert received == 80


# ---------------------------------------------------------------------------
# Test: SSE format correctness
# ---------------------------------------------------------------------------


def test_sse_format_correctness() -> None:
    """SSE events should be formatted correctly."""
    event = SubtaskEvent(
        event_type=SubtaskEventType.COMPLETED,
        task_id="task-abc",
        subtask_id="task-abc-st-0",
        session_id="session-xyz",
        message="Subtask completed successfully",
    )

    formatted = format_sse_event(event)

    # Check SSE format: event: TYPE\ndata: JSON\n\n
    assert formatted.startswith("event: ")
    assert "subtask_completed" in formatted.split("\n")[0]
    assert "data: " in formatted
    assert formatted.endswith("\n\n")

    # Check JSON data is valid
    data_line = formatted.split("data: ")[1].split("\n\n")[0]
    data = json.loads(data_line)

    assert data["event_type"] == "subtask_completed"
    assert data["task_id"] == "task-abc"
    assert data["subtask_id"] == "task-abc-st-0"
    assert data["session_id"] == "session-xyz"
    assert data["message"] == "Subtask completed successfully"
    assert "timestamp" in data


def test_sse_format_all_event_types() -> None:
    """All event types should format correctly."""
    for event_type in SubtaskEventType:
        event = SubtaskEvent(
            event_type=event_type,
            task_id="task-1",
            subtask_id="task-1-st-0",
        )
        formatted = format_sse_event(event)

        assert f"event: {event_type}" in formatted
        assert "data: {" in formatted


# ---------------------------------------------------------------------------
# Test: SubtaskEvent data class
# ---------------------------------------------------------------------------


def test_subtask_event_to_dict() -> None:
    """SubtaskEvent should serialize to dict correctly."""
    event = SubtaskEvent(
        event_type=SubtaskEventType.FAILED,
        task_id="task-1",
        subtask_id="task-1-st-0",
        session_id="session-123",
        message="Error occurred",
    )

    data = event.to_dict()

    assert data["event_type"] == "subtask_failed"
    assert data["task_id"] == "task-1"
    assert data["subtask_id"] == "task-1-st-0"
    assert data["session_id"] == "session-123"
    assert data["message"] == "Error occurred"
    assert "timestamp" in data


def test_subtask_event_timestamp_default() -> None:
    """SubtaskEvent should auto-generate timestamp if not provided."""
    event = SubtaskEvent(
        event_type=SubtaskEventType.STARTED,
        task_id="task-1",
        subtask_id="task-1-st-0",
    )

    assert event.timestamp is not None
    assert "T" in event.timestamp  # ISO format contains T


# ---------------------------------------------------------------------------
# Test: Unsubscribe
# ---------------------------------------------------------------------------


def test_unsubscribe_removes_subscriber(emitter: SubtaskEventEmitter) -> None:
    """Unsubscribing should remove the subscriber."""
    sub = emitter.subscribe()

    # Emit an event
    event = SubtaskEvent(
        event_type=SubtaskEventType.STARTED,
        task_id="task-1",
        subtask_id="task-1-st-0",
    )
    emitter.emit(event)

    # Should receive the event
    assert sub.get(timeout=1) is not None

    # Unsubscribe
    emitter.unsubscribe(sub)

    # Emit another event
    event2 = SubtaskEvent(
        event_type=SubtaskEventType.COMPLETED,
        task_id="task-1",
        subtask_id="task-1-st-0",
    )
    emitter.emit(event2)

    # Should not receive (queue should be empty, not get blocked)
    try:
        sub.get(timeout=0.1)
        # If we get here, the event was received (bad)
        assert False, "Event should not have been received after unsubscribe"
    except queue.Empty:
        pass  # Expected


# ---------------------------------------------------------------------------
# Test: Queue overflow handling
# ---------------------------------------------------------------------------


def test_subscriber_queue_overflow_drops_message(emitter: SubtaskEventEmitter) -> None:
    """When subscriber queue is full, messages should be dropped."""
    # Create subscriber with maxsize=2
    q: queue.Queue = queue.Queue(maxsize=2)
    emitter._subscribers.append(q)

    # Fill the queue
    q.put("dummy1")
    q.put("dummy2")

    # Try to emit - should not raise, just drop
    event = SubtaskEvent(
        event_type=SubtaskEventType.PROGRESS,
        task_id="task-1",
        subtask_id="task-1-st-0",
    )

    # Should not raise
    emitter.emit(event)

    # Original messages should still be there
    assert q.get(timeout=1) == "dummy1"
    assert q.get(timeout=1) == "dummy2"


# ---------------------------------------------------------------------------
# Test: Module-level emitter singleton
# ---------------------------------------------------------------------------


def test_get_emitter_returns_singleton() -> None:
    """get_emitter should return the same instance."""
    emitter1 = get_emitter()
    emitter2 = get_emitter()

    assert emitter1 is emitter2
