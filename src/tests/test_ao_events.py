"""Tests for ao_events: parsing AO webhook payload format from notifier-openclaw."""

from __future__ import annotations

import pytest
from dataclasses import dataclass

# These imports will fail until ao_events.py is implemented (TDD)
from orchestration.ao_events import (
    AOEvent,
    EscalationContext,
    parse_ao_webhook,
    AOWebhookError,
)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@dataclass
class EscalatedPayload:
    """Valid payload for reaction.escalated event."""

    @staticmethod
    def raw() -> dict:
        return {
            "event_type": "reaction.escalated",
            "priority": "high",
            "session_id": "ao-session-123",
            "project_id": "jleechanorg/claw",
            "message": "CI failed after 3 attempts",
            "data": {
                "sessionId": "ao-session-123",
                "projectId": "jleechanorg/claw",
                "reactionKey": "ci-failed",
                "attempts": 3,
                "first_triggered": "2026-03-14T10:30:00Z",
            },
        }


@dataclass
class StuckSessionPayload:
    """Valid payload for session.stuck event."""

    @staticmethod
    def raw() -> dict:
        return {
            "event_type": "session.stuck",
            "priority": "medium",
            "session_id": "ao-session-456",
            "project_id": "worldarchitect/project",
            "message": "Session idle for 15 minutes",
            "data": {
                "sessionId": "ao-session-456",
                "projectId": "worldarchitect/project",
                "idle_duration_minutes": 15,
                "last_activity": "2026-03-14T10:15:00Z",
            },
        }


@dataclass
class MergeReadyPayload:
    """Valid payload for merge.ready event."""

    @staticmethod
    def raw() -> dict:
        return {
            "event_type": "merge.ready",
            "priority": "low",
            "session_id": "ao-session-789",
            "project_id": "jleechanorg/claw",
            "message": "PR ready for merge",
            "data": {
                "sessionId": "ao-session-789",
                "projectId": "jleechanorg/claw",
                "pr_url": "https://github.com/jleechanorg/claw/pull/42",
                "pr_number": 42,
                "branch": "feature/fix-ci",
            },
        }


# ---------------------------------------------------------------------------
# parse_ao_webhook tests
# ---------------------------------------------------------------------------


def test_parse_reaction_escalated() -> None:
    """Parse reaction.escalated event with session context."""
    payload = EscalatedPayload.raw()
    event = parse_ao_webhook(payload)

    assert isinstance(event, AOEvent)
    assert event.event_type == "reaction.escalated"
    assert event.priority == "high"
    assert event.session_id == "ao-session-123"
    assert event.project_id == "jleechanorg/claw"
    assert event.message == "CI failed after 3 attempts"

    # Verify escalation context extraction
    assert event.data is not None
    assert event.data.get("reactionKey") == "ci-failed"
    assert event.data.get("attempts") == 3
    assert event.data.get("sessionId") == "ao-session-123"
    assert event.data.get("projectId") == "jleechanorg/claw"


def test_parse_session_stuck() -> None:
    """Parse session.stuck event with idle duration."""
    payload = StuckSessionPayload.raw()
    event = parse_ao_webhook(payload)

    assert isinstance(event, AOEvent)
    assert event.event_type == "session.stuck"
    assert event.priority == "medium"
    assert event.session_id == "ao-session-456"
    assert event.project_id == "worldarchitect/project"

    # Verify data dict contains idle duration
    assert event.data is not None
    assert event.data.get("idle_duration_minutes") == 15


def test_parse_merge_ready() -> None:
    """Parse merge.ready event with PR URL."""
    payload = MergeReadyPayload.raw()
    event = parse_ao_webhook(payload)

    assert isinstance(event, AOEvent)
    assert event.event_type == "merge.ready"
    assert event.priority == "low"
    assert event.session_id == "ao-session-789"

    # Verify PR details in data dict
    assert event.data is not None
    assert event.data.get("pr_url") == "https://github.com/jleechanorg/claw/pull/42"
    assert event.data.get("pr_number") == 42
    assert event.data.get("branch") == "feature/fix-ci"


def test_parse_missing_event_type() -> None:
    """Missing event_type should raise structured error, not crash."""
    payload = {
        "priority": "high",
        "session_id": "ao-session-123",
        "project_id": "jleechanorg/claw",
    }

    with pytest.raises(AOWebhookError) as exc_info:
        parse_ao_webhook(payload)

    assert "event_type" in str(exc_info.value).lower()


def test_parse_missing_session_id() -> None:
    """Missing session_id should raise structured error."""
    payload = {
        "event_type": "reaction.escalated",
        "project_id": "jleechanorg/claw",
    }

    with pytest.raises(AOWebhookError) as exc_info:
        parse_ao_webhook(payload)

    assert "session_id" in str(exc_info.value).lower()


def test_parse_missing_project_id() -> None:
    """Missing project_id should raise structured error."""
    payload = {
        "event_type": "reaction.escalated",
        "session_id": "ao-session-123",
    }

    with pytest.raises(AOWebhookError) as exc_info:
        parse_ao_webhook(payload)

    assert "project_id" in str(exc_info.value).lower()


def test_parse_malformed_data_field() -> None:
    """Malformed data field (non-dict) should raise structured error."""
    payload = {
        "event_type": "reaction.escalated",
        "session_id": "ao-session-123",
        "project_id": "jleechanorg/claw",
        "data": "not-a-dict",  # Should be a dict
    }

    with pytest.raises(AOWebhookError) as exc_info:
        parse_ao_webhook(payload)

    assert "data" in str(exc_info.value).lower()


def test_parse_empty_payload() -> None:
    """Empty payload should raise structured error."""
    with pytest.raises(AOWebhookError) as exc_info:
        parse_ao_webhook({})

    # Should mention required fields
    error_msg = str(exc_info.value).lower()
    assert "event_type" in error_msg or "session_id" in error_msg


def test_parse_unknown_event_type_allowed() -> None:
    """Unknown event_type should be allowed (extensible)."""
    payload = {
        "event_type": "custom.event",
        "priority": "low",
        "session_id": "ao-session-999",
        "project_id": "test/project",
        "message": "Custom event",
        "data": {},
    }

    event = parse_ao_webhook(payload)

    assert event.event_type == "custom.event"
    assert event.session_id == "ao-session-999"


def test_parse_all_fields_extracted_from_data() -> None:
    """Verify all required fields are extracted from data dict."""
    payload = EscalatedPayload.raw()
    event = parse_ao_webhook(payload)

    # All these should be extractable from the data dict
    assert event.data["sessionId"] == "ao-session-123"
    assert event.data["projectId"] == "jleechanorg/claw"
    assert event.data["reactionKey"] == "ci-failed"
    assert event.data["attempts"] == 3


def test_parse_data_optional() -> None:
    """data field should be optional (defaults to empty dict)."""
    payload = {
        "event_type": "heartbeat",
        "priority": "low",
        "session_id": "ao-session-000",
        "project_id": "test/project",
        "message": "heartbeat",
    }

    event = parse_ao_webhook(payload)

    assert event.data == {}


def test_escalation_context_extraction() -> None:
    """EscalationContext dataclass should extract relevant fields."""
    payload = EscalatedPayload.raw()
    event = parse_ao_webhook(payload)

    # If EscalationContext is implemented, verify it extracts correctly
    if hasattr(event, "escalation_context"):
        ctx = event.escalation_context
        assert ctx.reaction_key == "ci-failed"
        assert ctx.attempts == 3
