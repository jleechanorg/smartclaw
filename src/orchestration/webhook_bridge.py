"""GitHub lifecycle ingress helpers for mctrl."""

from __future__ import annotations

import hashlib
import hmac
import os
from enum import StrEnum

from orchestration.event_util import normalize_trigger_type
from orchestration.gh_integration import (
    extract_head_sha,
    parse_github_webhook_actor,
    parse_github_webhook_author_association,
    parse_github_webhook_pr_number,
    parse_github_webhook_repo,
)
from orchestration.pr_lifecycle import route_event

TRUSTED_AUTHOR_ASSOCIATIONS: frozenset[str] = frozenset({
    "OWNER",
    "MEMBER",
    "COLLABORATOR",
})

PR_SPECIFIC_EVENTS: frozenset[str] = frozenset({
    "issue_comment",
    "pull_request",
    "pull_request_review",
    "pull_request_review_comment",
})


class GitHubEventMode(StrEnum):
    """How GitHub events are received by the pipeline."""

    WEBHOOK = "webhook"
    POLLING = "polling"


def receive_github_event(
    payload: dict,
    event_type: str,
    *,
    trusted_associations: frozenset[str] | None = None,
    previous_runs: list[dict] | None = None,
    webhook_secret: str | None = None,
    signature_header: str | None = None,
    raw_body: bytes | None = None,
) -> dict | None:
    """Normalize an incoming GitHub webhook payload into an mctrl-ready event."""
    secret = webhook_secret or os.environ.get("GITHUB_WEBHOOK_SECRET")
    if secret:
        # Use 'is None' to allow empty body (b"") while rejecting missing body
        if signature_header is None or raw_body is None:
            return None
        try:
            expected = "sha256=" + hmac.new(
                secret.encode(), raw_body, hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(expected, signature_header):
                return None
        except Exception:
            return None

    supported_events = {
        "issue_comment",
        "pull_request",
        "pull_request_review",
        "pull_request_review_comment",
        "check_suite",
        "check_run",
    }
    if event_type not in supported_events:
        return None

    pr_number = parse_github_webhook_pr_number(payload)
    # PR-specific events require a PR number; check_suite/check_run may not have one
    if event_type in PR_SPECIFIC_EVENTS and pr_number is None:
        return None

    repo = parse_github_webhook_repo(payload)
    if not repo:
        return None

    actor = parse_github_webhook_actor(payload)
    author_association = parse_github_webhook_author_association(payload)

    if event_type == "issue_comment":
        allowed = TRUSTED_AUTHOR_ASSOCIATIONS if trusted_associations is None else trusted_associations
        if author_association not in allowed:
            return None

    result = {
        "event_type": event_type,
        "action": payload.get("action", ""),
        "pr_number": pr_number,
        "repo": repo,
        "actor": actor,
        "author_association": author_association,
        "raw": payload,
    }
    trigger_type = normalize_trigger_type(event_type, payload)
    head_sha = extract_head_sha(payload)
    if not trigger_type or not head_sha:
        return result

    lifecycle_decision = route_event(
        {
            "trigger_source": "event",
            "trigger_type": trigger_type,
            "repository": repo,
            "pr_number": pr_number,
            "head_sha": head_sha,
            "draft": bool(
                isinstance(payload.get("pull_request"), dict)
                and payload["pull_request"].get("draft")
            ),
        },
        previous_runs=previous_runs or [],
    )
    return {**result, **lifecycle_decision}


def current_github_event_mode() -> GitHubEventMode:
    """Return active GitHub event mode based on webhook secret presence."""
    return GitHubEventMode.WEBHOOK if os.environ.get("GITHUB_WEBHOOK_SECRET") else GitHubEventMode.POLLING
