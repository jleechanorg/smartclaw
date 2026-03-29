"""PR review decision engine — pure LLM review over full context.

This module implements autonomous PR review where the LLM makes all decisions
based on full context. There is no hardcoded review logic, no deterministic
gating, no keyword matching. The LLM receives everything and decides.

Key design principles:
- The entire ReviewContext is serialized into a single LLM prompt.
- No pre-filtering, no if-statements on path globs, no line-count thresholds.
- The prompt instructs the LLM to use CLAUDE.md rules, memory, and prior
  patterns as its review criteria — the LLM applies them through inference.
- Posts review via gh api if action is approve or request_changes.
- Sends Slack DM to Jeffrey if escalating.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol

from orchestration.pr_reviewer import ReviewContext

logger = logging.getLogger(__name__)

# Constants
JEFFREY_DM_CHANNEL = os.environ.get("SMARTCLAW_DM_CHANNEL", "")
OPENCLAW_STATE_DIR = os.path.expanduser("~/.openclaw/state")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ReviewComment:
    """Inline review comment for a specific file/line.

    Attributes:
        path: File path relative to repo root.
        line: Line number for the comment.
        body: Comment body text.
    """

    path: str
    line: int
    body: str


@dataclass
class ReviewDecision:
    """Decision made by the LLM review engine.

    Attributes:
        action: One of "approve", "request_changes", "escalate_to_jeffrey".
        confidence: Confidence score between 0 and 1.
        summary: Human-readable summary of the decision.
        comments: List of inline review comments.
    """

    action: str
    confidence: float
    summary: str
    comments: list[ReviewComment]

    def __post_init__(self) -> None:
        """Validate action is one of the allowed values."""
        valid_actions = {"approve", "request_changes", "escalate_to_jeffrey", "skip"}
        if self.action not in valid_actions:
            raise ValueError(f"Invalid action: {self.action}. Must be one of {valid_actions}")


# ---------------------------------------------------------------------------
# Protocols for dependency injection (easier to mock in tests)
# ---------------------------------------------------------------------------


class LLMCaller(Protocol):
    """Protocol for LLM API calls - implemented by mocks and real client."""

    def __call__(self, prompt: str) -> str:
        """Call LLM with prompt and return response."""
        ...


class SlackPoster(Protocol):
    """Protocol for posting Slack messages - implemented by mocks and real client."""

    def send_dm(self, message: str, channel: str | None = None) -> bool:
        """Send a DM to a channel or user."""
        ...


class GHReviewPoster(Protocol):
    """Protocol for posting GitHub PR reviews."""

    def post_review(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        event: str,
        body: str,
        comments: list[dict],
    ) -> bool:
        """Post a review to a PR."""
        ...


# ---------------------------------------------------------------------------
# Internal LLM prompt construction
# ---------------------------------------------------------------------------


def _build_review_prompt(context: ReviewContext) -> str:
    """Build the full prompt for LLM review.

    The prompt includes all context and clear instructions for the LLM to
    make a review decision based on inference, not hardcoded rules.

    Args:
        context: Complete review context from pr_reviewer.

    Returns:
        Formatted prompt string for LLM consumption.
    """
    prompt = f"""\
# Autonomous PR Review

You are reviewing a pull request as an autonomous coding assistant. Your goal is to
determine whether this PR should be approved, have changes requested, or be escalated
to a human (Jeffrey) for review.

## Review Guidelines

Use the following sources to make your decision:
- CLAUDE.md rules (provided below)
- Project memories and prior feedback
- Prior review patterns on similar PRs
- CI status and test results
- Code quality and best practices

IMPORTANT: You must make decisions through inference over the full context below.
Do NOT use hardcoded rules, path patterns, or line-count thresholds. Read everything
and reason about what is appropriate.

## Context

### PR Diff
```
{context.diff}
```

### Commits
{json.dumps(context.commits, indent=2)}

### CI Status
{json.dumps(context.ci_status, indent=2)}

### CLAUDE.md Rules
{context.claude_md_rules or "(No CLAUDE.md rules found)"}

### OpenClaw Memories
{context.memories or "(No memories found)"}

### Prior Review Patterns
{context.prior_patterns or "(No prior patterns found)"}

## Decision Format

Respond with a JSON object containing your decision:

```json
{{
  "action": "approve" | "request_changes" | "escalate_to_jeffrey",
  "confidence": 0.0-1.0,
  "summary": "Brief explanation of your decision",
  "comments": [
    {{"path": "src/file.py", "line": 42, "body": "Suggestion or concern"}}
  ]
}}
```

## Decision Criteria

- **approve**: PR is clean, follows rules, CI passes, no major concerns
- **request_changes**: PR has issues that should be fixed before merge
- **escalate_to_jeffrey**: Unsure, high-risk changes, or needs human judgment

Consider:
- Security risks (credentials, secrets, auth changes)
- Large diffs that may be hard to review thoroughly
- Lack of context (unknown repo, no prior patterns)
- Your own confidence level - if uncertain, escalate

Respond now with your decision in JSON format.
"""
    return prompt


def _parse_llm_response(response: str) -> ReviewDecision:
    """Parse LLM response into ReviewDecision.

    Handles JSON parsing and validation.

    Args:
        response: Raw LLM response string.

    Returns:
        Parsed ReviewDecision.

    Raises:
        ValueError: If response cannot be parsed or is invalid.
    """
    try:
        # Try to extract JSON from response (may have markdown formatting)
        json_start = response.find("{")
        json_end = response.rfind("}")
        if json_start == -1 or json_end == -1:
            raise ValueError("No JSON found in response")

        json_str = response[json_start : json_end + 1]
        data = json.loads(json_str)

        # Validate required fields
        action = data.get("action", "")
        if action not in {"approve", "request_changes", "escalate_to_jeffrey"}:
            # Default to escalate for safety if invalid action
            action = "escalate_to_jeffrey"

        confidence = float(data.get("confidence", 0.5))
        confidence = max(0.0, min(1.0, confidence))  # Clamp to [0, 1]

        summary = data.get("summary", "No summary provided")

        # Parse comments
        comments = []
        for c in data.get("comments", []):
            if isinstance(c, dict):
                comments.append(
                    ReviewComment(
                        path=str(c.get("path", "")),
                        line=int(c.get("line", 0)),
                        body=str(c.get("body", "")),
                    )
                )

        return ReviewDecision(
            action=action,
            confidence=confidence,
            summary=summary,
            comments=comments,
        )

    except (json.JSONDecodeError, ValueError, TypeError) as e:
        logger.warning(f"Failed to parse LLM response: {e}")
        # Default to escalate on parse failure for safety
        return ReviewDecision(
            action="escalate_to_jeffrey",
            confidence=0.0,
            summary=f"Failed to parse LLM response: {e}",
            comments=[],
        )


# ---------------------------------------------------------------------------
# External LLM call (can be mocked in tests)
# ---------------------------------------------------------------------------


def _call_llm(prompt: str) -> str:
    """Call the LLM with a prompt.

    Uses claude-sonnet-4-6 via Anthropic SDK.
    Falls back to subprocess call to `claude -p` if SDK not available.

    Args:
        prompt: The prompt to send to the LLM.

    Returns:
        LLM response as string.

    Raises:
        RuntimeError: If LLM call fails.
    """
    # Try anthropic SDK first
    try:
        import anthropic

        client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        logger.info("Called LLM via Anthropic SDK")
        return msg.content[0].text
    except ImportError:
        logger.info("Anthropic SDK not available, falling back to subprocess")
    except Exception as e:
        logger.warning(f"Anthropic SDK call failed: {e}, falling back to subprocess")

    # Fallback: use subprocess with claude CLI
    try:
        result = subprocess.run(
            ["claude", "-p", "--model", "claude-sonnet-4-6"],
            input=prompt,
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
        )
        logger.info("Called LLM via claude CLI subprocess")
        return result.stdout
    except subprocess.CalledProcessError as e:
        logger.error(f"claude CLI failed: {e.stderr}")
        raise RuntimeError(f"LLM call failed: {e.stderr}") from e
    except FileNotFoundError:
        raise RuntimeError("LLM call failed: neither anthropic SDK nor claude CLI available") from None
    except subprocess.TimeoutExpired:
        raise RuntimeError("LLM call timed out after 120 seconds") from None


# ---------------------------------------------------------------------------
# GitHub review posting
# ---------------------------------------------------------------------------


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
    if decision.comments:
        # Create temporary file for review body with comments
        review_body = decision.summary

        # Use gh pr review with --body and --comments
        cmd = [
            "gh", "pr", "review",
            str(pr_number),
            "--repo", f"{owner}/{repo}",
            "--" + event.lower().replace("_", "-"),
            "--body", review_body,
        ]

        # Add comments via separate calls (gh doesn't support inline in one call)
        for comment in decision.comments:
            comment_cmd = [
                "gh", "pr", "comment",
                str(pr_number),
                "--repo", f"{owner}/{repo}",
                "--body", f"[{comment.path}:{comment.line}] {comment.body}",
            ]
            try:
                subprocess.run(
                    comment_cmd,
                    capture_output=True,
                    text=True,
                    check=True,
                )
            except subprocess.CalledProcessError as e:
                logger.warning(f"Failed to post comment: {e}")
    else:
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


# ---------------------------------------------------------------------------
# Slack notification for escalation
# ---------------------------------------------------------------------------


def _notify_jeffrey_escalation(
    decision: ReviewDecision,
    pr_url: str,
) -> bool:
    """Send Slack DM to Jeffrey about escalated PR.

    Args:
        decision: The review decision with notes.
        pr_url: URL of the PR being escalated.

    Returns:
        True if message was sent successfully.
    """
    # Build escalation message
    message = f"""\
*PR Review Escalation*

PR: {pr_url}

*Decision:* Escalated to Jeffrey
*Confidence:* {decision.confidence:.0%}

*Summary:*
{decision.summary}

*Comments:*
"""
    for comment in decision.comments:
        message += f"- {comment.path}:{comment.line} — {comment.body}\n"

    # Use curl with bot token (similar pattern to openclaw_notifier.py)
    token = os.environ.get("OPENCLAW_SLACK_BOT_TOKEN")
    if not token:
        logger.error("OPENCLAW_SLACK_BOT_TOKEN not set")
        return False

    url = "https://slack.com/api/chat.postMessage"
    payload = json.dumps({
        "channel": JEFFREY_DM_CHANNEL,
        "text": message,
        "unfurl_links": False,
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            result = json.loads(response.read().decode("utf-8"))
            if result.get("ok"):
                logger.info(f"Sent escalation DM to Jeffrey for {pr_url}")
                return True
            else:
                logger.error(f"Slack API error: {result.get('error')}")
                return False
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        logger.error(f"Failed to send Slack DM: {e}")
        return False


# ---------------------------------------------------------------------------
# Main review function
# ---------------------------------------------------------------------------


def review_pr(
    context: ReviewContext,
    *,
    pr_owner: str | None = None,
    pr_repo: str | None = None,
    pr_number: int | None = None,
    pr_url: str | None = None,
    llm_caller: LLMCaller | None = None,
    gh_poster: GHReviewPoster | None = None,
    slack_poster: SlackPoster | None = None,
) -> ReviewDecision:
    """Review a PR using LLM over full context.

    This is the main entry point for PR review. It:
    1. Builds a prompt from the full ReviewContext
    2. Calls the LLM to make a decision
    3. Posts the review to GitHub (if approve/request_changes)
    4. Notifies Jeffrey via Slack (if escalating)

    Args:
        context: Complete review context from pr_reviewer.
        pr_owner: Repository owner (for posting review).
        pr_repo: Repository name (for posting review).
        pr_number: PR number (for posting review).
        pr_url: PR URL (for Slack notification).
        llm_caller: Optional LLM caller (defaults to _call_llm).
        gh_poster: Optional GitHub review poster (defaults to gh CLI).
        slack_poster: Optional Slack poster (defaults to curl).

    Returns:
        ReviewDecision with action, confidence, summary, and comments.
    """
    # Build prompt from context
    prompt = _build_review_prompt(context)

    # Call LLM (use provided caller or default)
    try:
        if llm_caller is not None:
            response = llm_caller(prompt)
        else:
            response = _call_llm(prompt)
    except RuntimeError as e:
        # LLM failures should not crash the review flow; escalate to human
        logger.warning(f"LLM call failed: {e}")
        return ReviewDecision(
            action="escalate_to_jeffrey",
            confidence=0.0,
            summary=f"LLM call failed: {e}. Manual review required.",
            comments=[],
        )

    # Parse LLM response
    decision = _parse_llm_response(response)

    # Handle action
    if decision.action in ("approve", "request_changes"):
        # Post review to GitHub if we have PR info
        if pr_owner and pr_repo and pr_number:
            if gh_poster is not None:
                gh_poster.post_review(
                    pr_owner, pr_repo, pr_number,
                    decision.action, decision.summary,
                    [{"path": c.path, "line": c.line, "body": c.body} for c in decision.comments],
                )
            else:
                _post_gh_review(pr_owner, pr_repo, pr_number, decision)

    elif decision.action == "escalate_to_jeffrey":
        # Send Slack DM to Jeffrey
        if pr_url:
            if slack_poster is not None:
                slack_poster.send_dm(
                    f"PR Review Escalation\n\n{decision.summary}",
                    channel=JEFFREY_DM_CHANNEL,
                )
            else:
                _notify_jeffrey_escalation(decision, pr_url)

    return decision
