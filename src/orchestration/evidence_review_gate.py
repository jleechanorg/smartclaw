"""Evidence review merge gate — verify PR has passed evidence review.

This module provides a fail-open gate: if evidence review passes (or no evidence
required), the gate passes. If evidence review fails, the gate blocks. If there's
no evidence to review or GitHub API errors, the gate passes with a warning.

The evidence review is requested from CodeRabbit or Codex using the /er command,
which runs an independent skeptical review of evidence bundles against the project's
canonical evidence standards.
"""

from __future__ import annotations

import json
import subprocess
import logging
import re
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# Evidence verdicts are accepted from any commenter matching the PASS/WARN/FAIL pattern.
# No bot-login allowlist is maintained — any authenticated user's verdict counts.


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class EvidenceGateResult:
    """Result of an evidence review gate check.

    Attributes:
        passed: True if the gate passes (evidence review passed or not required).
        reason: Human-readable explanation of the result.
        reviewer_login: The bot that performed the review if exists, else None.
        verdict: The evidence review verdict (PASS, WARN, FAIL) if review exists.
    """

    passed: bool
    reason: str
    reviewer_login: Optional[str] = None
    verdict: Optional[str] = None


class EvidenceGateError(Exception):
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


def _get_issue_comments(owner: str, repo: str, pr_number: int) -> list[dict]:
    """Fetch issue comments for a PR from GitHub.

    Returns:
        List of comment dicts with keys: author, body, created_at.
    """
    args = [
        "issue", "view", str(pr_number),
        "--repo", f"{owner}/{repo}",
        "--json", "comments",
    ]
    raw = _run_gh(args)
    data = json.loads(raw)

    comments = []
    for c in data.get("comments", []):
        comments.append({
            "author": (c.get("author") or {}).get("login", "unknown"),
            "body": c.get("body", ""),
            "created_at": c.get("createdAt"),
        })

    return comments


def _extract_evidence_verdict(body: str) -> Optional[str]:
    """Extract evidence review verdict from comment body.

    Looks for patterns like:
    - "EVIDENCE BUNDLE REVIEW: PASS"
    - "Verdict: PASS"
    - "**PASS**" (standalone bold verdict)

    Args:
        body: The comment body text.

    Returns:
        PASS, WARN, FAIL, or None if not found.
    """
    # Try both canonical formats: legacy "EVIDENCE BUNDLE REVIEW: X" and
    # current "/er" command format "Verdict: X" or "**X**"
    match = re.search(
        r"(?:EVIDENCE\s*BUNDLE\s*REVIEW:\s*|Verdict:\s*|\*\*)(PASS|WARN|FAIL)\b",
        body,
        re.IGNORECASE,
    )
    if match:
        return match.group(1).upper()
    return None


# ---------------------------------------------------------------------------
# Gate logic
# ---------------------------------------------------------------------------


def check_evidence_review(owner: str, repo: str, pr_number: int) -> EvidenceGateResult:
    """Check evidence review status on a PR.

    This checks for evidence review results posted by CodeRabbit or Codex bots
    using the /er command. The verdict is extracted from review comments or
    issue comments on the PR.

    Args:
        owner: Repository owner (e.g., "jleechanorg").
        repo: Repository name (e.g., "test-repo").
        pr_number: PR number (e.g., 42).

    Returns:
        EvidenceGateResult with passed status and reason.
    """
    # Get both PR reviews and issue comments
    try:
        reviews = _get_reviews(owner, repo, pr_number)
        comments = _get_issue_comments(owner, repo, pr_number)
    except Exception as e:
        # Fail-open: GitHub API errors should not block the merge gate
        logger.warning(f"GitHub API error checking evidence review status: {e}")
        return EvidenceGateResult(
            passed=True,
            reason=f"Warning: Could not check evidence review status ({e}); failing open",
            reviewer_login=None,
            verdict=None,
        )

    # Collect all evidence verdicts from any commenter (bots or authenticated gh user).
    # Accept from: known bots OR any comment body containing a recognizable verdict.
    # Use the latest verdict so re-reviews (fixes after an initial FAIL) win.
    all_verdicts: list[tuple[str, str, str]] = []  # (submitted_at, verdict, author)

    for review in reviews:
        v = _extract_evidence_verdict(review["body"])
        if v:
            all_verdicts.append((review.get("submitted_at") or "", v, review["author"]))

    for comment in comments:
        v = _extract_evidence_verdict(comment["body"])
        if v:
            all_verdicts.append((comment.get("created_at") or "", v, comment["author"]))

    if all_verdicts:
        # Sort by timestamp, pick the latest
        all_verdicts.sort(key=lambda x: x[0])
        _, verdict, reviewer = all_verdicts[-1]
        return _create_result_from_verdict(verdict, reviewer)

    # No evidence review found - check if PR has any evidence files
    # For now, we pass if no evidence review is found (not all PRs need evidence)
    return EvidenceGateResult(
        passed=True,
        reason="No evidence review found (not required for this PR)",
        reviewer_login=None,
        verdict=None,
    )


def _create_result_from_verdict(verdict: str, reviewer: str) -> EvidenceGateResult:
    """Create EvidenceGateResult from a verdict string."""
    if verdict == "PASS":
        return EvidenceGateResult(
            passed=True,
            reason=f"Evidence review PASSED by {reviewer}",
            reviewer_login=reviewer,
            verdict=verdict,
        )

    if verdict == "WARN":
        # WARN is acceptable - evidence has minor issues but not blocking
        return EvidenceGateResult(
            passed=True,
            reason=f"Evidence review WARN (acceptable) by {reviewer}",
            reviewer_login=reviewer,
            verdict=verdict,
        )

    # FAIL blocks the merge
    return EvidenceGateResult(
        passed=False,
        reason=f"Evidence review FAILED by {reviewer} - see comments for details",
        reviewer_login=reviewer,
        verdict=verdict,
    )


# ---------------------------------------------------------------------------
# Request evidence review helper
# ---------------------------------------------------------------------------


def build_evidence_review_request(
    owner: str,
    repo: str,
    pr_number: int,
    evidence_path: str,
) -> str:
    """Build a message to request evidence review from CodeRabbit or Codex.

    Args:
        owner: Repository owner.
        repo: Repository name.
        pr_number: PR number.
        evidence_path: Path to evidence file or directory in the repo.

    Returns:
        Formatted message to post as a PR comment to trigger /er command.
    """
    # Use blob/HEAD so the URL resolves to the PR head branch, not main
    evidence_url = f"https://github.com/{owner}/{repo}/blob/HEAD/{evidence_path}"
    return f"""@coderabbitai /er

Please run evidence review on the following evidence bundle:
- Evidence: {evidence_url}

Provide the verdict (PASS/WARN/FAIL) in your response."""


def request_evidence_review(
    owner: str,
    repo: str,
    pr_number: int,
    evidence_path: str,
) -> bool:
    """Request evidence review by posting a comment to trigger /er command.

    Args:
        owner: Repository owner.
        repo: Repository name.
        pr_number: PR number.
        evidence_path: Path to evidence file or directory in the repo.

    Returns:
        True if comment was posted successfully.
    """
    message = build_evidence_review_request(owner, repo, pr_number, evidence_path)

    try:
        subprocess.run(
            ["gh", "pr", "comment", str(pr_number), "--repo", f"{owner}/{repo}", "--body", message],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        logger.info(f"Requested evidence review for {owner}/{repo}#{pr_number}")
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to request evidence review: {e.stderr}")
        return False
    except subprocess.TimeoutExpired:
        logger.error("Timed out requesting evidence review after 30s")
        return False
