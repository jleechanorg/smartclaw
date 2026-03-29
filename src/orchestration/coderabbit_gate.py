"""CodeRabbit merge gate — check CodeRabbit review status on PRs.

This module provides a fail-open gate: if CodeRabbit approves or is rate-limited,
the gate passes. If CodeRabbit requests changes, the gate blocks. If there's
no CodeRabbit review or GitHub API errors, the gate passes with a warning.
"""

from __future__ import annotations

import json
import re
import subprocess
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# CodeRabbit bot login on GitHub (actual GitHub username is coderabbitai[bot])
CODERABBIT_LOGIN = "coderabbitai[bot]"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class GateResult:
    """Result of a CodeRabbit gate check.

    Attributes:
        passed: True if the gate passes (CodeRabbit approved or not required).
        reason: Human-readable explanation of the result.
        reviewer_login: The CodeRabbit bot login if a review exists, else None.
    """

    passed: bool
    reason: str
    reviewer_login: Optional[str] = None


class CodeRabbitGateError(Exception):
    """Raised when the gate check cannot be performed."""
    pass


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------


def _run_gh(args: list[str]) -> str:
    """Run a gh CLI command and return stdout.

    Raises:
        RuntimeError: If the command fails or gh is not found.
    """
    try:
        result = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"gh {' '.join(args)} failed: {exc.stderr or exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"gh {' '.join(args)} timed out after 30s") from exc
    except FileNotFoundError as exc:
        raise RuntimeError("gh CLI not found") from exc


def _get_reviews(owner: str, repo: str, pr_number: int) -> list[dict]:
    """Fetch reviews for a PR from GitHub.

    Returns:
        List of review dicts with keys: author, state, body, submitted_at.
    """
    args = [
        "pr", "view", str(pr_number),
        "--repo", f"{owner}/{repo}",
        "--json", "reviews",
    ]
    raw = _run_gh(args)
    data = json.loads(raw)

    reviews = []
    for r in data.get("reviews", []):
        state_raw = (r.get("state") or "").upper()
        state_map = {
            "APPROVED": "approved",
            "CHANGES_REQUESTED": "changes_requested",
            "DISMISSED": "dismissed",
            "PENDING": "pending",
            "COMMENTED": "commented",
        }
        state = state_map.get(state_raw, "commented")

        reviews.append({
            "author": (r.get("author") or {}).get("login", "unknown"),
            "state": state,
            "body": r.get("body") or "",
            "submitted_at": r.get("submittedAt"),
        })

    return reviews


def _get_unresolved_thread_ids(owner: str, repo: str, pr_number: int) -> set[int]:
    """Return the set of pull_request_review_comment IDs in unresolved threads.

    Uses GraphQL to get threads with isResolved==False, then collects all
    comment database IDs within those threads so callers can cross-reference
    against the REST pull-review-comments endpoint.

    Returns an empty set on any error (fail-open: unresolved filter skipped).
    """
    query = """
    query($owner: String!, $repo: String!, $pr: Int!) {
      repository(owner: $owner, name: $repo) {
        pullRequest(number: $pr) {
          reviewThreads(first: 100) {
            nodes {
              isResolved
              comments(first: 50) {
                nodes { databaseId }
              }
            }
          }
        }
      }
    }
    """
    try:
        raw = _run_gh([
            "api", "graphql",
            "-f", f"query={query}",
            "-f", f"owner={owner}",
            "-f", f"repo={repo}",
            "-F", f"pr={pr_number}",
        ])
        data = json.loads(raw)
        threads = (
            data.get("data", {})
            .get("repository", {})
            .get("pullRequest", {})
            .get("reviewThreads", {})
            .get("nodes", [])
        )
        ids: set[int] = set()
        for thread in threads:
            if thread.get("isResolved"):
                continue
            for comment in thread.get("comments", {}).get("nodes", []):
                db_id = comment.get("databaseId")
                if db_id is not None:
                    ids.add(db_id)
        return ids
    except Exception as e:
        logger.warning(f"Could not fetch unresolved thread IDs: {e}")
        return set()


def _get_review_comments(owner: str, repo: str, pr_number: int) -> list[dict]:
    """Fetch inline review comments from unresolved threads for a PR.

    Only returns comments that belong to unresolved review threads (via GraphQL)
    AND are still in context (position != null). Resolved threads and outdated
    comments are excluded so the gate is not blocked by already-addressed issues.

    Returns:
        List of comment dicts with keys: author, body.
    """
    unresolved_ids = _get_unresolved_thread_ids(owner, repo, pr_number)

    args = [
        "api",
        f"/repos/{owner}/{repo}/pulls/{pr_number}/comments",
        "--jq", ".",
    ]
    raw = _run_gh(args)
    data = json.loads(raw)

    comments = []
    for c in data:
        # Skip outdated comments (line context no longer in current diff)
        if c.get("position") is None:
            continue
        # Skip comments from resolved threads (unresolved_ids empty = fail-open)
        if unresolved_ids and c.get("id") not in unresolved_ids:
            continue
        comments.append({
            "author": (c.get("user") or {}).get("login", "unknown"),
            "body": c.get("body") or "",
        })

    return comments


# ---------------------------------------------------------------------------
# Gate logic
# ---------------------------------------------------------------------------


def check_coderabbit(owner: str, repo: str, pr_number: int) -> GateResult:
    """Check CodeRabbit review status on a PR.

    Args:
        owner: Repository owner (e.g., "jleechanorg").
        repo: Repository name (e.g., "test-repo").
        pr_number: PR number (e.g., 42).

    Returns:
        GateResult with passed status and reason.
    """
    try:
        reviews = _get_reviews(owner, repo, pr_number)
    except Exception as e:
        # Fail-open: GitHub API errors should not block the merge gate
        logger.warning(f"GitHub API error checking CodeRabbit status: {e}")
        return GateResult(
            passed=True,
            reason=f"Warning: Could not check CodeRabbit status ({e}); failing open",
            reviewer_login=None,
        )

    # Filter to only CodeRabbit reviews
    coderabbit_reviews = [r for r in reviews if r["author"] == CODERABBIT_LOGIN]

    if not coderabbit_reviews:
        # No CodeRabbit review - not required, so passes
        return GateResult(
            passed=True,
            reason="No CodeRabbit review found (not required)",
            reviewer_login=None,
        )

    # Get the latest CodeRabbit review (reviews are in chronological order)
    latest = coderabbit_reviews[-1]
    state = latest["state"]
    reviewer = latest["author"]

    if state == "approved":
        return GateResult(
            passed=True,
            reason=f"CodeRabbit approved",
            reviewer_login=reviewer,
        )

    if state == "changes_requested":
        return GateResult(
            passed=False,
            reason=f"CodeRabbit requested changes",
            reviewer_login=reviewer,
        )

    # Rate-limited or other states (commented, pending) are acceptable
    # CodeRabbit is still processing or was rate-limited
    # BUT: Check if COMMENTED review has Critical/Major issues - those block
    if state == "commented":
        # Check review body first (no API call needed - already have it)
        review_body = latest.get("body") or ""
        if _has_blocking_coderabbit_issues(review_body):
            return GateResult(
                passed=False,
                reason=f"CodeRabbit COMMENTED with blocking issues (Critical/Major)",
                reviewer_login=reviewer,
            )
        
        # Check inline comments for blocking issues
        try:
            comments = _get_review_comments(owner, repo, pr_number)
            coderabbit_comments = [c for c in comments if c["author"] == CODERABBIT_LOGIN]
            
            for comment in coderabbit_comments:
                if _has_blocking_coderabbit_issues(comment["body"]):
                    return GateResult(
                        passed=False,
                        reason=f"CodeRabbit COMMENTED with blocking issues (Critical/Major)",
                        reviewer_login=reviewer,
                    )
        except Exception as e:
            # Fail-open on inline comment fetch errors (review body already checked above)
            logger.warning(f"Could not check CodeRabbit inline comments: {e}")
    
    return GateResult(
        passed=True,
        reason=f"CodeRabbit status: {state} (acceptable)",
        reviewer_login=reviewer,
    )


def _has_blocking_coderabbit_issues(body: str) -> bool:
    """Check if CodeRabbit comment body has Critical or Major severity issues.
    
    Args:
        body: The CodeRabbit review comment body.
    
    Returns:
        True if there are Critical or Major issues that should block merge.
    """
    # Look for severity indicators in the comment
    # CodeRabbit uses: 🔴 Critical, 🟠 Major, 🟡 Minor, 🧹 Nitpick
    
    # Check for blocking severities - match emoji + keyword, no \b since CR wraps
    # in underscores (_🔴 Critical_) where _ is a word char and \b would not match.
    blocking_patterns = [
        r"🔴\s*Critical(?![a-zA-Z0-9])",
        r"🟠\s*Major(?![a-zA-Z0-9])",
    ]
    for pattern in blocking_patterns:
        if re.search(pattern, body, re.IGNORECASE):
            return True
    return False
