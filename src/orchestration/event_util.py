"""Event type normalization utilities for GitHub webhooks.

Extracts normalized trigger types from various GitHub event payloads.
"""

from __future__ import annotations


def normalize_trigger_type(event_type: str, payload: dict) -> str | None:
    """Return a normalised trigger type string, or None when unrecognised.

    Mirrors webhook_bridge._normalize_trigger_type and webhook_queue._trigger_type_for.
    """
    action = payload.get("action")
    if event_type == "pull_request" and isinstance(action, str):
        return f"pull_request.{action}"
    if event_type == "pull_request_review" and isinstance(action, str):
        return f"pull_request_review.{action}"
    if event_type == "pull_request_review_comment" and isinstance(action, str):
        return f"pull_request_review_comment.{action}"
    if event_type == "check_suite":
        check_suite = payload.get("check_suite")
        if (
            isinstance(check_suite, dict)
            and action == "completed"
            and isinstance(check_suite.get("conclusion"), str)
        ):
            return f"check_suite.completed.{check_suite['conclusion']}"
    if event_type == "check_run":
        check_run = payload.get("check_run")
        if (
            isinstance(check_run, dict)
            and action == "completed"
            and isinstance(check_run.get("conclusion"), str)
        ):
            return f"check_run.completed.{check_run['conclusion']}"
    return None


def trigger_type_for(event_type: str, payload: dict) -> str | None:
    """Return a normalised trigger type string, or None when unrecognised.

    Simplified version without conclusion extraction.
    """
    action = payload.get("action")
    if event_type == "pull_request" and isinstance(action, str):
        return f"pull_request.{action}"
    if event_type == "pull_request_review" and isinstance(action, str):
        return f"pull_request_review.{action}"
    if event_type == "pull_request_review_comment" and isinstance(action, str):
        return f"pull_request_review_comment.{action}"
    if event_type == "check_suite" and action == "completed":
        return "check_suite.completed"
    if event_type == "check_run" and action == "completed":
        return "check_run.completed"
    return None
