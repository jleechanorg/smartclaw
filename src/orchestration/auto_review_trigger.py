"""Auto-review trigger: wire AO merge-ready to OpenClaw review.

This module implements the bridge between AO's merge.ready events and OpenClaw's
autonomous PR review system. When AO reports a PR is approved and CI green,
this module triggers an OpenClaw review before notifying Jeffrey.

Key responsibilities:
- Handle merge.ready events from AO
- Build review context from PR data
- Execute LLM-powered review
- Post review results to GitHub / notify Jeffrey / dispatch fixes
- Track reviewed PRs for idempotency
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from orchestration.action_executor import send_escalation_notification
from orchestration.ao_cli import ao_send
from orchestration.ao_events import AOEvent
from orchestration.jsonfile_util import atomic_json_write_single
from orchestration.path_util import ensure_state_dir
from orchestration.pr_reviewer import ReviewContext, build_review_context
from orchestration.pr_review_decision import ReviewDecision, review_pr

logger = logging.getLogger(__name__)

# Constants
OPENCLAW_HOME = Path.home() / ".openclaw"
OPENCLAW_STATE_DIR = OPENCLAW_HOME / "state"
REVIEWED_PRS_PATH = OPENCLAW_STATE_DIR / "reviewed_prs.json"


# ---------------------------------------------------------------------------
# Idempotency tracking
# ---------------------------------------------------------------------------


def _load_reviewed_prs() -> dict:
    """Load the set of already-reviewed PRs from state file.

    Returns:
        Dict mapping "owner/repo#pr_number" to {"reviewed_at": timestamp}
    """
    try:
        ensure_state_dir(OPENCLAW_STATE_DIR)
        if not REVIEWED_PRS_PATH.exists():
            return {}
        with open(REVIEWED_PRS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Failed to load reviewed PRs: {e}")
        return {}


def _save_reviewed_prs(data: dict) -> bool:
    """Save the set of reviewed PRs to state file.

    Args:
        data: Dict mapping "owner/repo#pr_number" to {"reviewed_at": timestamp}

    Returns:
        True if save succeeded, False otherwise.
    """
    try:
        atomic_json_write_single(data, REVIEWED_PRS_PATH)
        return True
    except Exception as e:
        logger.error(f"Failed to save reviewed PRs: {e}")
        return False


def has_been_reviewed(owner: str, repo: str, pr_number: int) -> bool:
    """Check if a PR has already been reviewed by OpenClaw.

    Args:
        owner: Repository owner (e.g., "jleechanorg")
        repo: Repository name (e.g., "claw")
        pr_number: PR number (e.g., 42)

    Returns:
        True if the PR has already been reviewed, False otherwise.
    """
    pr_key = f"{owner}/{repo}#{pr_number}"
    reviewed_prs = _load_reviewed_prs()
    return pr_key in reviewed_prs


def mark_as_reviewed(owner: str, repo: str, pr_number: int) -> None:
    """Mark a PR as reviewed by OpenClaw.

    Args:
        owner: Repository owner
        repo: Repository name
        pr_number: PR number
    """
    pr_key = f"{owner}/{repo}#{pr_number}"
    reviewed_prs = _load_reviewed_prs()
    reviewed_prs[pr_key] = {
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
    }
    if _save_reviewed_prs(reviewed_prs):
        logger.info(f"Marked {pr_key} as reviewed")
    else:
        logger.warning(f"Failed to persist reviewed status for {pr_key}")


# ---------------------------------------------------------------------------
# Review flow
# ---------------------------------------------------------------------------


def _check_idempotency(event: AOEvent) -> bool:
    """Check if this PR has already been reviewed (idempotency).

    Args:
        event: The AOEvent to check.

    Returns:
        True if already reviewed (should skip), False otherwise.
    """
    # Parse project_id to get owner/repo
    project_id = event.project_id
    if "/" not in project_id:
        logger.warning(f"Invalid project_id format: {project_id}")
        return False  # Don't skip - try to process anyway

    owner, repo = project_id.split("/", 1)

    # Get PR number from event data
    pr_number = event.data.get("pr_number")
    if pr_number is None:
        # Try to extract from PR URL if present
        pr_url = event.data.get("pr_url", "")
        if "/pull/" in pr_url:
            try:
                pr_number = int(pr_url.split("/pull/")[-1].split("/")[0].split("#")[-1])
            except (ValueError, IndexError):
                logger.warning(f"Could not extract PR number from URL: {pr_url}")
                return False  # Don't skip

    if pr_number is None:
        logger.warning(f"Could not determine PR number for {owner}/{repo}, skipping idempotency check")
        return False  # Don't skip - can't determine if already reviewed

    return has_been_reviewed(owner, repo, pr_number)


def _post_gh_review(
    owner: str,
    repo: str,
    pr_number: int,
    decision: ReviewDecision,
) -> bool:
    """Post a review to GitHub via gh CLI.

    Args:
        owner: Repository owner.
        repo: Repository name.
        pr_number: PR number.
        decision: The review decision to post.

    Returns:
        True if review was posted successfully.
    """
    # Map action to GitHub review event
    event_map = {
        "approve": "APPROVE",
        "request_changes": "REQUEST_CHANGES",
    }
    event = event_map.get(decision.action)
    if not event:
        return False

    # Build gh command
    cmd = [
        "gh", "pr", "review",
        str(pr_number),
        "--repo", f"{owner}/{repo}",
        "--" + event.lower().replace("_", "-"),
        "--body", decision.summary,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
        )
        logger.info(f"Posted {event} review to {owner}/{repo}#{pr_number}")
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to post review: {e.stderr}")
        return False


def _notify_jeffrey_approval(message: str) -> bool:
    """Notify Jeffrey that OpenClaw approved a PR.

    Args:
        message: The pre-formatted message to send to Jeffrey.

    Returns:
        True if message was sent successfully.
    """
    return send_escalation_notification(message)


def _notify_jeffrey_escalation(message: str) -> bool:
    """Notify Jeffrey that OpenClaw escalated a PR.

    Args:
        message: The pre-formatted message to send to Jeffrey.

    Returns:
        True if message was sent successfully.
    """
    return send_escalation_notification(message)


def _dispatch_fix_agent(
    session_id: str,
    decision: ReviewDecision,
) -> bool:
    """Dispatch a fix agent to address review comments.

    Sends a message to the existing AO session with the review feedback.

    Args:
        session_id: The AO session ID to send the fix request to.
        decision: The review decision with comments to address.

    Returns:
        True if message was sent successfully.
    """
    # Build fix message from review comments
    message = f"""Review requested changes. Please address the following:

{decision.summary}

"""

    for comment in decision.comments:
        message += f"- {comment.path}:{comment.line}: {comment.body}\n"

    message += "\nPlease make the necessary changes and resubmit."

    try:
        ao_send(session_id, message)
        logger.info(f"Dispatched fix agent to session {session_id}")
        return True
    except Exception as e:
        logger.error(f"Failed to dispatch fix agent: {e}")
        return False


def _extract_pr_info(event: AOEvent) -> tuple[str, str, int, str]:
    """Extract PR information from an AOEvent.

    Args:
        event: The AOEvent to extract from.

    Returns:
        Tuple of (owner, repo, pr_number, pr_url)

    Raises:
        ValueError: If PR info cannot be extracted.
    """
    # Parse project_id to get owner/repo
    project_id = event.project_id
    if "/" not in project_id:
        raise ValueError(f"Invalid project_id format: {project_id}")

    owner, repo = project_id.split("/", 1)

    # Get PR number from event data
    pr_number = event.data.get("pr_number")
    pr_url = event.data.get("pr_url", "")

    if pr_number is None:
        # Try to extract from PR URL if present
        if "/pull/" in pr_url:
            try:
                pr_number = int(pr_url.split("/pull/")[-1].split("/")[0].split("#")[-1])
            except (ValueError, IndexError):
                raise ValueError(f"Could not extract PR number from URL: {pr_url}")
        else:
            raise ValueError("No pr_number or pr_url in event data")

    return owner, repo, pr_number, pr_url


def handle_merge_ready(event: AOEvent) -> ReviewDecision:
    """Handle a merge.ready event from AO.

    This is the main entry point. It:
    1. Checks idempotency (skip if already reviewed)
    2. Extracts PR info from the event
    3. Builds review context
    4. Calls LLM review
    5. Posts review / notifies Jeffrey / dispatches fix based on decision

    Args:
        event: The AOEvent with merge.ready type.

    Returns:
        ReviewDecision from the LLM review.
    """
    # Only handle merge.ready events
    if event.event_type != "merge.ready":
        return ReviewDecision(
            action="skip",
            confidence=1.0,
            summary="Not a merge.ready event",
            comments=[],
        )

    # Check idempotency
    if _check_idempotency(event):
        logger.info(f"PR already reviewed, skipping")
        return ReviewDecision(
            action="skip",
            confidence=1.0,
            summary="Already reviewed by OpenClaw",
            comments=[],
        )

    # Extract PR info
    try:
        owner, repo, pr_number, pr_url = _extract_pr_info(event)
    except ValueError as e:
        logger.error(f"Failed to extract PR info: {e}")
        return ReviewDecision(
            action="skip",
            confidence=0.0,
            summary=f"Failed to extract PR info: {e}",
            comments=[],
        )

    # Get session ID for dispatching fixes (if needed)
    session_id = event.session_id

    # Build review context
    try:
        context = build_review_context(owner, repo, pr_number)
    except Exception as e:
        logger.error(f"Failed to build review context: {e}")
        return ReviewDecision(
            action="escalate_to_jeffrey",
            confidence=0.0,
            summary=f"Failed to build review context: {e}",
            comments=[],
        )

    try:
        decision = review_pr(
            context,
            pr_owner=owner,
            pr_repo=repo,
            pr_number=pr_number,
            pr_url=pr_url,
        )
    except Exception as e:
        logger.error(f"Failed to review PR: {e}")
        return ReviewDecision(
            action="escalate_to_jeffrey",
            confidence=0.0,
            summary=f"Failed to review PR: {e}",
            comments=[],
        )

    # Handle decision (side effects)
    side_effects_success = True
    
    if decision.action == "approve":
        # Post review to GH and notify Jeffrey
        if not _post_gh_review(owner, repo, pr_number, decision):
            side_effects_success = False
        
        approval_message = f"""*OpenClaw Approved — Ready to Merge*

PR: {pr_url}

*Summary:*
{decision.summary}

You can merge when ready.
"""
        if not _notify_jeffrey_approval(approval_message):
            side_effects_success = False

    elif decision.action == "request_changes":
        # Post review to GH and dispatch fix agent
        if not _post_gh_review(owner, repo, pr_number, decision):
            side_effects_success = False
        if not _dispatch_fix_agent(session_id, decision):
            side_effects_success = False

    elif decision.action == "escalate_to_jeffrey":
        # Notify Jeffrey with notes
        escalation_message = f"""*PR Review Escalation — Needs Your Eyes*

PR: {pr_url}

*Confidence:* {decision.confidence:.0%}

*Summary:*
{decision.summary}

*Comments:*
"""
        for comment in decision.comments:
            escalation_message += f"- {comment.path}:{comment.line} — {comment.body}\n"
        if not _notify_jeffrey_escalation(escalation_message):
            side_effects_success = False

    # Mark as reviewed AFTER side-effects succeed (idempotency)
    if side_effects_success:
        mark_as_reviewed(owner, repo, pr_number)
    else:
        logger.warning(f"Side effects failed for {owner}/{repo}#{pr_number}, not marking as reviewed (will retry)")

    return decision
