"""Subtask event stream — SSE for real-time monitoring.

Provides Server-Sent Events endpoint for clients to receive
real-time updates on subtask progress.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Generator

logger = logging.getLogger(__name__)

# SSE Constants
SSE_KEEPALIVE_INTERVAL = 30  # seconds
DEFAULT_PORT = 19889


class SubtaskEventType(StrEnum):
    """Subtask event type enum."""

    STARTED = "subtask_started"
    PROGRESS = "subtask_progress"
    COMPLETED = "subtask_completed"
    FAILED = "subtask_failed"
    CANCELLED = "subtask_cancelled"


@dataclass
class SubtaskEvent:
    """Subtask event data class.

    Attributes:
        event_type: The type of subtask event
        task_id: The parent task identifier
        subtask_id: The subtask identifier
        session_id: The AO session ID (if applicable)
        message: Human-readable message
        timestamp: ISO format timestamp
    """

    event_type: SubtaskEventType
    task_id: str
    subtask_id: str
    session_id: str | None = None
    message: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "event_type": self.event_type,
            "task_id": self.task_id,
            "subtask_id": self.subtask_id,
            "session_id": self.session_id,
            "message": self.message,
            "timestamp": self.timestamp,
        }


class SubtaskEventEmitter:
    """Thread-safe event emitter for subtask status updates.

    This class manages subscriptions and emits events to all subscribers
    using thread-safe queues.

    Usage:
        emitter = SubtaskEventEmitter()
        subscriber_queue = emitter.subscribe()
        emitter.emit(SubtaskEvent(...))
        # In subscriber thread:
        event = subscriber_queue.get()
    """

    def __init__(self) -> None:
        """Initialize the event emitter."""
        self._subscribers: list[queue.Queue] = []
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        """Add a new subscriber and return their queue.

        Returns:
            A queue that will receive emitted events
        """
        with self._lock:
            q: queue.Queue = queue.Queue(maxsize=100)
            self._subscribers.append(q)
            return q

    def unsubscribe(self, q: queue.Queue) -> None:
        """Remove a subscriber queue.

        Args:
            q: The queue to remove
        """
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def emit(self, event: SubtaskEvent) -> None:
        """Emit event to all subscribers.

        Args:
            event: The event to emit
        """
        # Build the event message
        message = self._format_sse_event(event)

        with self._lock:
            # Make a copy to avoid holding lock while delivering
            subscribers = list(self._subscribers)

        for subscriber in subscribers:
            try:
                subscriber.put_nowait(message)
            except queue.Full:
                # Subscriber queue is full - drop the message
                logger.warning("Subscriber queue full, dropping event: %s", event.event_type)

    def _format_sse_event(self, event: SubtaskEvent) -> str:
        """Format event as SSE message.

        Args:
            event: The event to format

        Returns:
            SSE formatted string
        """
        return f"event: {event.event_type}\ndata: {json.dumps(event.to_dict())}\n\n"


def sse_handler(request) -> Generator[str, None, None]:
    """Flask/httpx handler for SSE stream.

    This is a generator function that yields SSE-formatted event strings.
    It handles keepalive heartbeats and graceful client disconnection.

    Args:
        request: The HTTP request object (framework-specific)

    Yields:
        SSE-formatted event strings

    Usage (Flask example):
        @app.route('/events')
        def events():
            return Response(sse_handler(request), mimetype='text/event-stream')
    """
    emitter = get_emitter()
    subscriber_queue = emitter.subscribe()

    try:
        while True:
            try:
                # Get event with timeout for keepalive
                event_msg = subscriber_queue.get(timeout=SSE_KEEPALIVE_INTERVAL)
                yield event_msg
            except queue.Empty:
                # Send keepalive comment
                yield ": keepalive\n\n"

    except GeneratorExit:
        # Client disconnected
        emitter.unsubscribe(subscriber_queue)
        logger.debug("SSE client disconnected")


def format_sse_event(event: SubtaskEvent) -> str:
    """Format a SubtaskEvent as an SSE string.

    Args:
        event: The event to format

    Returns:
        SSE formatted string
    """
    return f"event: {event.event_type}\ndata: {json.dumps(event.to_dict())}\n\n"


def start_event_server(host: str = "127.0.0.1", port: int = DEFAULT_PORT):
    """Start the SSE event server (placeholder for future implementation).

    This is a placeholder for running a standalone SSE server.
    Currently, SSE is intended to be integrated into an existing web server.

    Args:
        host: Host to bind to
        port: Port to listen on

    Note:
        This function is a placeholder. In practice, SSE would be
        integrated into an existing Flask/httpx application.
    """
    logger.info("SSE event server would start on %s:%d (not implemented)", host, port)
    logger.info("Integrate sse_handler into your web framework instead")


# Module-level emitter instance
_emitter: SubtaskEventEmitter | None = None
_emitter_lock = threading.Lock()


def get_emitter() -> SubtaskEventEmitter:
    """Get the module-level event emitter instance.

    Returns:
        The global SubtaskEventEmitter instance
    """
    global _emitter
    with _emitter_lock:
        if _emitter is None:
            _emitter = SubtaskEventEmitter()
        return _emitter


def emit_subtask_event(
    event_type: SubtaskEventType,
    task_id: str,
    subtask_id: str,
    session_id: str | None = None,
    message: str = "",
) -> None:
    """Convenience function to emit a subtask event.

    Args:
        event_type: The type of event
        task_id: The parent task ID
        subtask_id: The subtask ID
        session_id: The AO session ID (if applicable)
        message: Human-readable message
    """
    event = SubtaskEvent(
        event_type=event_type,
        task_id=task_id,
        subtask_id=subtask_id,
        session_id=session_id,
        message=message,
    )
    get_emitter().emit(event)
