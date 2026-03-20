"""AO webhook event parser for notifier-openclaw payloads.

This module parses incoming webhook events from AO's notifier-openclaw plugin
into typed AOEvent objects with EscalationContext extraction.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class AOWebhookError(Exception):
    """Raised when webhook payload validation fails."""

    pass


@dataclass(frozen=True)
class EscalationContext:
    """Extracted escalation context from AO webhook data dict.

    This captures the relevant fields from the 'data' portion of the webhook
    payload that are specific to escalation events (reaction.escalated).
    """

    reaction_key: str
    attempts: int
    first_triggered: str | None = None


@dataclass
class AOEvent:
    """Typed wrapper for AO webhook payload from notifier-openclaw.

    Attributes:
        event_type: The AO event type (e.g., 'reaction.escalated', 'session.stuck')
        priority: Event priority ('high', 'medium', 'low')
        session_id: The AO session ID that generated this event
        project_id: The project identifier (e.g., 'owner/repo')
        message: Human-readable message describing the event
        data: Additional event-specific data dict
    """

    event_type: str
    priority: str
    session_id: str
    project_id: str
    message: str
    data: dict = field(default_factory=dict)

    @property
    def escalation_context(self) -> EscalationContext | None:
        """Extract EscalationContext from data dict if applicable.

        Only reaction.escalated events contain the fields needed for EscalationContext.
        Returns None if the required fields are not present in data.
        """
        if self.event_type != "reaction.escalated":
            return None

        reaction_key = self.data.get("reactionKey")
        attempts = self.data.get("attempts")
        first_triggered = self.data.get("first_triggered")

        if reaction_key is None or attempts is None:
            return None

        return EscalationContext(
            reaction_key=str(reaction_key),
            attempts=int(attempts),
            first_triggered=str(first_triggered) if first_triggered else None,
        )


def parse_ao_webhook(payload: dict) -> AOEvent:
    """Validate and convert raw AO webhook JSON into an AOEvent.

    Args:
        payload: Raw dictionary from webhook POST body (JSON decoded)

    Returns:
        Validated AOEvent instance

    Raises:
        AOWebhookError: If required fields are missing or malformed
    """
    if not payload:
        raise AOWebhookError("Empty payload: event_type, session_id, and project_id are required")

    # Support two formats:
    # 1. Flat (internal/test): {event_type, session_id, project_id, ...}
    # 2. AO native: {type: "notification", event: {type, sessionId, projectId, ...}}
    if "event" in payload and isinstance(payload["event"], dict):
        # AO native format — unwrap the nested event object
        ev = payload["event"]
        event_type = ev.get("type")
        session_id = ev.get("sessionId")
        project_id = ev.get("projectId")
        priority = ev.get("priority", "low")
        message = ev.get("message", "")
        raw_data = ev.get("data")
    else:
        # Flat format
        event_type = payload.get("event_type")
        session_id = payload.get("session_id")
        project_id = payload.get("project_id")
        priority = payload.get("priority", "low")
        message = payload.get("message", "")
        raw_data = payload.get("data")

    if event_type is None:
        raise AOWebhookError("Missing required field: event_type")

    if session_id is None:
        raise AOWebhookError("Missing required field: session_id")

    if project_id is None:
        raise AOWebhookError("Missing required field: project_id")

    # Validate data field if present (must be a dict)
    if raw_data is not None and not isinstance(raw_data, dict):
        raise AOWebhookError("Field 'data' must be a dictionary")

    data: dict = raw_data if raw_data is not None else {}

    return AOEvent(
        event_type=str(event_type),
        priority=str(priority),
        session_id=str(session_id),
        project_id=str(project_id),
        message=str(message),
        data=data,
    )
