"""Unified Merge Gate - enforces all 6 PR merge conditions.

This module provides a single check_merge_ready() function that validates
all 6 conditions before allowing a PR to be merged:

1. CI green — all required checks passing (check_ci_status)
2. No merge conflicts — PR is MERGEABLE (check_mergeable)
3. CodeRabbit approved — CR must post APPROVED state; COMMENTED alone is not sufficient (check_coderabbit)
4. Bugbot clean + no blocking inline comments — CR/Bugbot Critical/Major block;
   Low/Medium are informational (check_blocking_comments, covers conditions 4+5)
5. All inline comments resolved — included in check_blocking_comments above
6. Evidence PASS — /er returns PASS if evidence* or testing_* files exist (check_evidence_pass)

Note: OpenClaw LLM review (formerly condition 7) is disabled until the auto-trigger
mechanism is built (orch-j9e0.4). check_openclaw_review() remains available.

Five check functions implement the 6 conditions because check_blocking_comments
covers both condition 4 (bugbot clean) and condition 5 (inline comments resolved).

Usage:
    python -m orchestration.merge_gate <owner> <repo> <pr_number>

TODO: This module is ~680 lines.  Plan to split into sub-modules when stable:
  - merge_gate/conditions.py  (individual check_* functions)
  - merge_gate/models.py      (ConditionResult, MergeVerdict)
  - merge_gate/__main__.py    (CLI entry point)
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

from orchestration.code_path_classifier import is_code_path


@dataclass
class ConditionResult:
    """Result of a single merge condition check."""

    name: str
    passed: bool
    details: str = ""
    blocked: bool = False


@dataclass
class MergeVerdict:
    """Result of checking all merge conditions."""

    pr_url: str
    can_merge: bool
    conditions: list[ConditionResult] = field(default_factory=list)
    blocked_reasons: list[str] = field(default_factory=list)

    @property
    def ci_green(self) -> bool:
        return any(c.name == "CI green" and c.passed for c in self.conditions)

    @property
    def mergeable(self) -> bool:
        return any(c.name == "MERGEABLE" and c.passed for c in self.conditions)

    @property
    def cr_approved(self) -> bool:
        return any(c.name == "CodeRabbit approved" and c.passed for c in self.conditions)

    @property
    def no_blocking_comments(self) -> bool:
        return any(c.name == "No blocking comments" and c.passed for c in self.conditions)

    @property
    def evidence_passed(self) -> bool:
        return any(c.name == "Evidence review" and c.passed for c in self.conditions)

    @property
    def openclaw_approved(self) -> bool:
        return any(c.name == "OpenClaw LLM review" and c.passed for c in self.conditions)


def run_gh(*args: str) -> tuple[int, str, str]:
    """Run a gh command and return (returncode, stdout, stderr).

    Handles TimeoutExpired and FileNotFoundError explicitly so callers
    get a consistent (non-zero rc, "", error_message) tuple instead of
    an unhandled exception crashing the merge action.
    """
    try:
        result = subprocess.run(
            ["gh"] + list(args),
            capture_output=True,
            timeout=60,
        )
        stdout = result.stdout.decode("utf-8", errors="replace")
        stderr = result.stderr.decode("utf-8", errors="replace")
        return result.returncode, stdout, stderr
    except subprocess.TimeoutExpired:
        return 1, "", "gh command timed out after 30s"
    except FileNotFoundError:
        return 1, "", "gh binary not found in PATH"


def check_ci_status(owner: str, repo: str, pr_number: int) -> ConditionResult:
    """Check condition 1: CI green — uses check-runs API, not table parsing."""
    # Get the PR's head SHA
    rc, stdout, _ = run_gh("api", f"repos/{owner}/{repo}/pulls/{pr_number}", "--jq", ".head.sha")
    if rc != 0 or not stdout.strip():
        return ConditionResult(
            name="CI green",
            passed=False,
            details="Failed to get PR head SHA",
            blocked=True,
        )

    head_sha = stdout.strip()

    # Fetch check runs for the commit
    rc, stdout, stderr = run_gh(
        "api", f"repos/{owner}/{repo}/commits/{head_sha}/check-runs",
        "--jq", "[.check_runs[] | {name, status, conclusion}]",
    )
    if rc != 0:
        return ConditionResult(
            name="CI green",
            passed=False,
            details=f"Failed to get check runs: {stderr}",
            blocked=True,
        )

    try:
        checks = json.loads(stdout) if stdout.strip() else []
    except json.JSONDecodeError:
        return ConditionResult(
            name="CI green",
            passed=False,
            details="Failed to parse check runs",
            blocked=True,
        )

    if not checks:
        return ConditionResult(
            name="CI green",
            passed=False,
            details="No check runs found",
            blocked=True,
        )

    pending = [c for c in checks if c.get("status") in ("in_progress", "queued")]
    completed = [c for c in checks if c.get("status") == "completed"]
    
    # Acceptable conclusions for completed checks
    acceptable_conclusions = {"success", "neutral", "skipped"}
    failed = [c for c in completed if c.get("conclusion") not in acceptable_conclusions]

    if pending:
        names = ", ".join(c["name"] for c in pending[:3])
        return ConditionResult(
            name="CI green",
            passed=False,
            details=f"Pending checks: {names}",
            blocked=True,
        )

    if failed:
        names_with_conclusions = ", ".join(
            f"{c['name']} ({c.get('conclusion', 'unknown')})" for c in failed[:3]
        )
        return ConditionResult(
            name="CI green",
            passed=False,
            details=f"Failed/non-success checks: {names_with_conclusions}",
            blocked=True,
        )

    return ConditionResult(
        name="CI green",
        passed=True,
        details=f"All {len(checks)} CI checks passed",
    )


def check_mergeable(owner: str, repo: str, pr_number: int) -> ConditionResult:
    """Check condition 2: PR is MERGEABLE (not CONFLICTING, not null)."""
    rc, stdout, stderr = run_gh(
        "api", f"repos/{owner}/{repo}/pulls/{pr_number}",
        "--jq", "{mergeable: .mergeable, state: .mergeable_state}",
    )

    if rc != 0:
        return ConditionResult(
            name="MERGEABLE",
            passed=False,
            details=f"Failed to get mergeable status: {stderr}",
            blocked=True,
        )

    try:
        data = json.loads(stdout.strip()) if stdout.strip() else {}
    except json.JSONDecodeError:
        return ConditionResult(
            name="MERGEABLE",
            passed=False,
            details="Failed to parse mergeable response",
            blocked=True,
        )

    mergeable = data.get("mergeable")
    state = data.get("state", "unknown")

    # null means GitHub hasn't computed it yet — fail-closed to prevent
    # accidental merge-through when can_merge checks `c.blocked`
    if mergeable is None:
        return ConditionResult(
            name="MERGEABLE",
            passed=False,
            details="Mergeable state not yet computed (null) — retry next cycle",
            blocked=True,  # fail-closed: block until GitHub computes state
        )

    if mergeable is True:
        return ConditionResult(
            name="MERGEABLE",
            passed=True,
            details=f"PR is mergeable (state={state})",
        )

    return ConditionResult(
        name="MERGEABLE",
        passed=False,
        details=f"PR has merge conflicts (mergeable={mergeable}, state={state})",
        blocked=True,
    )


# Matches structured Critical/Major severity markers used by CodeRabbit and Cursor/Bugbot.
# Uses specific formatting patterns to avoid false positives like "No major issues found."
# NOTE: do NOT use re.VERBOSE here — '#' in VERBOSE mode starts a comment, which would
# turn patterns like r'###\s*critical' into empty matches (hitting every position).
_CRITICAL_MAJOR_PATTERNS = re.compile(
    r"🔴\s*critical"
    r"|🟠\s*major"
    r"|\*\*high\s+severity\*\*"
    r"|\*\*critical\*\*"
    r"|\*\*major\*\*"
    r"|\#\#\#\s*critical"
    r"|\#\#\#\s*major"
    r"|severity[:\s]+critical"
    r"|severity[:\s]+major"
    r"|_🔴\s*critical_"
    r"|_🟠\s*major_",
    re.IGNORECASE,
)


def _has_critical_or_major(body: str) -> bool:
    """Return True if body contains a structured Critical/Major severity marker.

    Uses specific formatting patterns (emojis, bold, headers) rather than bare
    substrings to avoid false positives like "No major issues found."
    """
    return bool(_CRITICAL_MAJOR_PATTERNS.search(body))



def check_coderabbit(owner: str, repo: str, pr_number: int) -> ConditionResult:
    """Check condition 3: CodeRabbit approved.

    Fail-closed: requires CR to post explicit APPROVED state.
    COMMENTED alone does not pass — configure .coderabbit.yaml with
    reviews.auto_review.approve=true so CR posts APPROVED when satisfied.
    Verifies the latest CR review covers the current HEAD SHA.
    """
    # Fetch HEAD SHA so we can verify CR reviewed the latest commit
    rc_sha, head_stdout, _ = run_gh("api", f"repos/{owner}/{repo}/pulls/{pr_number}", "--jq", ".head.sha")
    head_sha = head_stdout.strip() if rc_sha == 0 else ""

    rc, stdout, stderr = run_gh("api", f"repos/{owner}/{repo}/pulls/{pr_number}/reviews", "--jq", ".")

    if rc != 0:
        return ConditionResult(
            name="CodeRabbit approved",
            passed=False,
            details=f"Failed to get reviews: {stderr}",
            blocked=True,
        )

    try:
        reviews = json.loads(stdout) if stdout.strip() else []
    except json.JSONDecodeError:
        return ConditionResult(
            name="CodeRabbit approved",
            passed=False,
            details="Failed to parse reviews",
            blocked=True,
        )

    # Find CR reviews — fail-closed if none exist
    cr_reviews = [r for r in reviews if "coderabbit" in r.get("user", {}).get("login", "").lower()]

    if not cr_reviews:
        return ConditionResult(
            name="CodeRabbit approved",
            passed=False,
            details="No CodeRabbit review yet — post '@coderabbitai all good?' to trigger",
            blocked=True,
        )

    # Use the LATEST CR review (GitHub returns oldest-first)
    latest = cr_reviews[-1]
    state = latest.get("state", "").upper()
    body = latest.get("body", "")

    if state == "APPROVED":
        review_sha = latest.get("commit_id", "")
        if not head_sha or not review_sha:
            return ConditionResult(
                name="CodeRabbit approved",
                passed=False,
                details="Could not verify CodeRabbit APPROVED the current HEAD",
                blocked=True,
            )
        if review_sha != head_sha:
            return ConditionResult(
                name="CodeRabbit approved",
                passed=False,
                details=f"CodeRabbit APPROVED is stale (reviewed {review_sha[:8]}, HEAD is {head_sha[:8]})",
                blocked=True,
            )
        return ConditionResult(
            name="CodeRabbit approved",
            passed=True,
            details="CodeRabbit APPROVED",
        )

    if state == "CHANGES_REQUESTED":
        return ConditionResult(
            name="CodeRabbit approved",
            passed=False,
            details="CodeRabbit requested changes",
            blocked=True,
        )

    if state == "COMMENTED":
        # COMMENTED is not sufficient — CR must explicitly APPROVED.
        # Check if CR is paused (provides a more specific error message).
        if _is_cr_review_paused(owner, repo, pr_number):
            return ConditionResult(
                name="CodeRabbit approved",
                passed=False,
                details="CodeRabbit reviews paused (too many commits) — post '@coderabbitai review' to trigger",
                blocked=True,
            )
        # CR posted COMMENTED, not APPROVED — block.
        # If CR found issues, include that context; otherwise explain the config fix.
        if _has_critical_or_major(body):
            return ConditionResult(
                name="CodeRabbit approved",
                passed=False,
                details="CodeRabbit found Critical/Major issues (state=COMMENTED, not APPROVED)",
                blocked=True,
            )
        return ConditionResult(
            name="CodeRabbit approved",
            passed=False,
            details=(
                "CodeRabbit posted COMMENTED, not APPROVED — "
                "add .coderabbit.yaml with reviews.auto_review.approve=true"
            ),
            blocked=True,
        )

    return ConditionResult(
        name="CodeRabbit approved",
        passed=False,
        details=f"Unknown CR state: {state}",
        blocked=True,
    )


def classify_inline_comments(comments: list[dict]) -> tuple[list[dict], list[dict]]:
    """Classify inline comments into blocking vs informational.

    Blocking: CR/Bugbot Critical/Major, human comments
    Informational: CR/Bugbot Low/Medium, Copilot, Codex, chatgpt-codex-connector
    """
    blocking = []
    informational = []

    for comment in comments:
        author = comment.get("user", {}).get("login", "").lower()
        is_review_comment = comment.get("position") is not None or comment.get("line") is not None

        if not is_review_comment:
            continue

        if "coderabbit" in author or "cursor" in author or "bugbot" in author:
            # Only structured Critical/Major markers block from automated reviewers.
            # Bare substring "critical"/"major" would false-positive on phrases like
            # "No major issues found" — use pattern matching instead.
            if _has_critical_or_major(comment.get("body", "")):
                blocking.append(comment)
            else:
                informational.append(comment)
        elif "copilot" in author or "codex" in author or "chatgpt" in author:
            informational.append(comment)
        else:
            # Human comments always block
            blocking.append(comment)

    return blocking, informational


def _is_cr_review_paused(owner: str, repo: str, pr_number: int) -> bool:
    """Check if CodeRabbit paused its review via an issue comment.

    CR posts a comment containing "Reviews paused" and "auto_pause" when too
    many commits land in quick succession.  A subsequent "@coderabbitai review"
    or "@coderabbitai resume" comment from a human cancels the pause.

    Returns True if the most recent CR control comment is a pause notice.
    """
    rc, stdout, _ = run_gh(
        "api", f"repos/{owner}/{repo}/issues/{pr_number}/comments",
        "--jq", '[.[] | select(.user.login == "coderabbitai[bot]") | {body: .body, created_at: .created_at}]',
    )
    if rc != 0 or not stdout.strip():
        return False  # fail-open: can't fetch comments, don't block on this

    try:
        cr_comments = json.loads(stdout) if stdout.strip() else []
    except json.JSONDecodeError:
        return False

    if not cr_comments:
        return False

    # Check the latest CR comment for pause markers
    latest_body = cr_comments[-1].get("body", "")
    if "reviews paused" in latest_body.lower() or "auto_pause" in latest_body.lower():
        # Check if a human posted "@coderabbitai review" or "resume" AFTER the pause
        rc2, stdout2, _ = run_gh(
            "api", f"repos/{owner}/{repo}/issues/{pr_number}/comments",
            "--jq", '[.[] | select(.user.login != "coderabbitai[bot]") | {body: .body, created_at: .created_at}]',
        )
        if rc2 == 0 and stdout2.strip():
            try:
                human_comments = json.loads(stdout2)
                pause_time = cr_comments[-1].get("created_at", "")
                for hc in reversed(human_comments):
                    if hc.get("created_at", "") > pause_time:
                        body_lower = hc.get("body", "").lower()
                        if "@coderabbitai review" in body_lower or "@coderabbitai resume" in body_lower:
                            return False  # human triggered re-review after pause
            except json.JSONDecodeError:
                pass
        return True

    return False



def _get_unresolved_comment_ids(owner: str, repo: str, pr_number: int) -> set[str] | None:
    """Get node IDs of comments on unresolved threads via GraphQL.

    Returns a set of comment node IDs, or None if the GraphQL call fails.
    """
    query = """query($owner: String!, $name: String!, $pr: Int!, $after: String = null) {
      repository(owner: $owner, name: $name) {
        pullRequest(number: $pr) {
          reviewThreads(first: 100, after: $after) {
            pageInfo {
              hasNextPage
              endCursor
            }
            nodes {
              isResolved
              comments(first: 50) {
                nodes {
                  id
                }
              }
            }
          }
        }
      }
    }"""

    ids: set[str] = set()
    after: str | None = None

    while True:
        args = [
            "api", "graphql",
            "-F", f"owner={owner}",
            "-F", f"name={repo}",
            "-F", f"pr={pr_number}",
            "-f", f"query={query}",
        ]
        if after:
            args.extend(["-F", f"after={after}"])

        rc, stdout, _ = run_gh(*args)
        if rc != 0:
            return None

        try:
            data = json.loads(stdout)
            thread_data = data["data"]["repository"]["pullRequest"]["reviewThreads"]
            threads = thread_data.get("nodes", [])
        except (json.JSONDecodeError, KeyError, TypeError):
            return None

        for thread in threads:
            if not thread.get("isResolved", True):
                for comment in thread.get("comments", {}).get("nodes", []):
                    comment_id = comment.get("id")
                    if comment_id:
                        ids.add(comment_id)

        page_info = thread_data.get("pageInfo", {})
        if not page_info.get("hasNextPage", False):
            break
        after = page_info.get("endCursor")
        if not isinstance(after, str) or not after:
            break

    return ids


def check_blocking_comments(owner: str, repo: str, pr_number: int) -> ConditionResult:
    """Check condition 4+5: No blocking inline comments on unresolved threads.

    Uses GraphQL to identify unresolved thread comment IDs, then filters
    REST comments to only those on unresolved threads. Falls back to
    counting all comments if GraphQL fails.
    """
    # Step 1: Get unresolved comment IDs via GraphQL (fail-closed if unavailable)
    unresolved_ids = _get_unresolved_comment_ids(owner, repo, pr_number)
    if unresolved_ids is None:
        return ConditionResult(
            name="No blocking comments",
            passed=False,
            details="GraphQL unavailable — could not verify thread resolution; blocking until resolvable",
            blocked=True,
        )

    # Step 2: Get all comments via REST
    rc, stdout, stderr = run_gh("api", f"repos/{owner}/{repo}/pulls/{pr_number}/comments", "--jq", ".")

    if rc != 0:
        return ConditionResult(
            name="No blocking comments",
            passed=False,
            details=f"Failed to get comments: {stderr}",
            blocked=True,
        )

    try:
        comments = json.loads(stdout) if stdout.strip() else []
    except json.JSONDecodeError:
        return ConditionResult(
            name="No blocking comments",
            passed=False,
            details="Failed to parse comments",
            blocked=True,
        )

    # Step 3: Filter to only unresolved thread comments
    comments = [c for c in comments if c.get("node_id", "") in unresolved_ids]
    blocking, informational = classify_inline_comments(comments)

    if blocking:
        return ConditionResult(
            name="No blocking comments",
            passed=False,
            details=f"{len(blocking)} blocking comment(s), {len(informational)} informational",
            blocked=True,
        )

    return ConditionResult(
        name="No blocking comments",
        passed=True,
        details=f"No blocking comments ({len(informational)} informational)",
    )

def _check_reviewer_agent_approved(owner: str, repo: str, pr_number: int, head_sha: str) -> ConditionResult | None:
    """Check reviewer verdict marker comments for current HEAD.

    Accepts only verdict markers posted by reviewer-agent identities:
        <!-- reviewer-verdict: APPROVE sha:<HEAD_SHA> -->

    Returns:
      - PASS ConditionResult for APPROVE on current HEAD from reviewer agent
      - BLOCKING fail ConditionResult for non-APPROVE on current HEAD from reviewer agent
      - None if no relevant reviewer-agent verdict found
    """
    import json
    import re

    rc, stdout, _ = run_gh(
        "api", f"repos/{owner}/{repo}/issues/{pr_number}/comments",
        "--paginate",
        "--jq", '.[] | select((.body // "") | test("reviewer-verdict:")) | {body, created_at: .created_at, user: {login: .user.login}}',
    )
    if rc != 0:
        # Fail closed: treat fetch error as blocking so a past PASS verdict.json
        # cannot satisfy evidence in place of an unverifiable current-head marker.
        return ConditionResult(
            name="Evidence review",
            passed=False,
            details="Failed to fetch reviewer verdict comments — cannot verify evidence",
            blocked=True,
        )

    if not stdout.strip():
        return None

    try:
        # Parse newline-delimited JSON objects from paginated results
        comments = [json.loads(line) for line in stdout.strip().split('\n') if line.strip()]
    except json.JSONDecodeError:
        logger.warning("Failed to parse reviewer verdict comments JSON")
        return ConditionResult(
            name="Evidence review",
            passed=False,
            details="Failed to parse reviewer verdict comments — cannot verify evidence",
            blocked=True,
        )
    
    if not comments:
        return None

    marker_re = re.compile(r"<!-- reviewer-verdict: (\w+) sha:(\w+) -->")
    for comment in reversed(comments):
        match = marker_re.search(comment.get("body", ""))
        if not match:
            continue

        verdict, sha = match.group(1), match.group(2)
        if sha != head_sha:
            continue

        reviewer_login = ((comment.get("user") or {}).get("login") or "").lower()
        # Exact allowlist — substring checks like "reviewer" in login are spoofable.
        _AUTHORIZED_REVIEWERS_ENV = os.environ.get("SMARTCLAW_AUTHORIZED_REVIEWERS", "reviewer-agent[bot]")
        _AUTHORIZED_REVIEWERS = set(_AUTHORIZED_REVIEWERS_ENV.split(","))
        if reviewer_login not in _AUTHORIZED_REVIEWERS:
            # Ignore markers from unauthorized identities.
            continue

        if verdict == "APPROVE":
            return ConditionResult(
                name="Evidence review",
                passed=True,
                details=f"Reviewer agent APPROVE at {comment.get('created_at', 'unknown')}",
            )

        return ConditionResult(
            name="Evidence review",
            passed=False,
            details=f"Reviewer agent verdict {verdict} for current HEAD",
            blocked=True,
        )

    return None


def check_evidence_pass(owner: str, repo: str, pr_number: int) -> ConditionResult:
    """Check condition 6: Evidence review passed.

    Three-tier check (in runtime order):
    0. If PR has no code files → skip immediately (docs-only PRs don't need evidence)
    1. Reviewer agent posted APPROVE verdict comment (marker in issue comments)
    2. If PR has verdict.json in docs/evidence/ → read it (requires stage2 PASS)
    """
    # Get PR head SHA for ref-aware API calls (used for verdict.json fetch)
    rc, sha_stdout, _ = run_gh(
        "api", f"repos/{owner}/{repo}/pulls/{pr_number}",
        "--jq", ".head.sha",
    )
    if rc != 0 or not sha_stdout.strip():
        return ConditionResult(
            name="Evidence review",
            passed=False,
            details="Could not resolve PR head SHA for verdict lookup",
            blocked=True,
        )
    pr_head_sha = sha_stdout.strip()

    rc, stdout, _ = run_gh("api", f"repos/{owner}/{repo}/pulls/{pr_number}/files", "--paginate", "--jq", ".[].filename")

    if rc != 0:
        return ConditionResult(
            name="Evidence review",
            passed=False,
            details="Could not check PR files — failing closed for safety",
            blocked=True,
        )

    # --paginate + .[].filename produces newline-delimited filenames
    files = [line for line in stdout.strip().split("\n") if line] if stdout.strip() else []

    # Check if PR touches code paths that require evidence
    code_files = [f for f in files if is_code_path(f)]
    if not code_files:
        return ConditionResult(
            name="Evidence review",
            passed=True,
            details="No code files changed (skipped)",
        )

    # Tier 1: Check for reviewer agent APPROVED review (preferred — replaces evidence pipeline)
    reviewer_result = _check_reviewer_agent_approved(owner, repo, pr_number, pr_head_sha)
    if reviewer_result is not None:
        return reviewer_result

    # Tier 2: Check for verdict.json in docs/evidence/{repo}/PR-{pr_number}/
    # Scope to THIS repo/PR only — prevents stale/unrelated verdicts
    pr_evidence_prefix = f"docs/evidence/{repo}/PR-{pr_number}/"
    verdict_files = [f for f in files if f.startswith(pr_evidence_prefix) and f.endswith("verdict.json")]
    if verdict_files:
        latest = sorted(verdict_files, reverse=True)[0]
        # Use ref param to fetch from PR head, not default branch
        rc, stdout, _ = run_gh(
            "api", f"repos/{owner}/{repo}/contents/{latest}?ref={pr_head_sha}",
            "--jq", ".content",
            "-H", "Accept: application/vnd.github.v3+json",
        )
        if rc == 0 and stdout.strip():
            try:
                content = base64.b64decode(stdout.strip()).decode("utf-8")
                verdict = json.loads(content)
                overall = verdict.get("overall", "PENDING")
                stage2 = verdict.get("stage2", {})
                if overall == "PASS" and stage2.get("status") == "PASS":
                    independence_verified = stage2.get("independence_verified", False)
                    model_differs = stage2.get("model_family_differs_from_stage1", False)
                    if independence_verified and model_differs:
                        return ConditionResult(
                            name="Evidence review",
                            passed=True,
                            details="Evidence verdict.json PASS (stage2 independent review + model family verified)",
                        )
                    return ConditionResult(
                        name="Evidence review",
                        passed=False,
                        details=f"verdict.json PASS but independence_verified={independence_verified}, model_family_differs={model_differs}",
                        blocked=True,
                    )
                if overall == "PASS" and stage2.get("status") == "PENDING":
                    return ConditionResult(
                        name="Evidence review",
                        passed=False,
                        details="Stage 1 PASS, awaiting stage 2 independent review",
                        blocked=True,
                    )
                if overall == "FAIL":
                    stage1 = verdict.get("stage1", {})
                    return ConditionResult(
                        name="Evidence review",
                        passed=False,
                        details=f"Evidence verdict FAIL — stage1: {stage1.get('findings', [])}",
                        blocked=True,
                    )
                # Explicit stage1 check — fail-closed if stage1 not PASS
                stage1 = verdict.get("stage1", {})
                if stage1.get("status") != "PASS":
                    return ConditionResult(
                        name="Evidence review",
                        passed=False,
                        details=f"Evidence stage1 not PASS (status={stage1.get('status')}): {stage1.get('findings', [])}",
                        blocked=True,
                    )
                # PENDING with stage1 PASS — stage 2 not run yet
                return ConditionResult(
                    name="Evidence review",
                    passed=False,
                    details="Stage 1 PASS but stage 2 not started — run stage2_reviewer",
                    blocked=True,
                )
            except (json.JSONDecodeError, Exception):
                return ConditionResult(
                    name="Evidence review",
                    passed=False,
                    details="Failed to parse evidence verdict from docs/evidence",
                    blocked=True,
                )

    return ConditionResult(
        name="Evidence review",
        passed=False,
        details="Code PR requires evidence bundle — run: python -m orchestration.evidence_bundle --stage2",
        blocked=True,
    )


def check_openclaw_review(owner: str, repo: str, pr_number: int) -> ConditionResult:
    """Check condition 7: OpenClaw LLM review approved.

    First checks GitHub PR reviews from openclaw[bot].
    If no review exists, triggers pr_review_decision.review_pr() to perform LLM review.
    """
    # First: Check for existing PR review from openclaw[bot]
    rc, stdout, _ = run_gh(
        "api", f"repos/{owner}/{repo}/pulls/{pr_number}/reviews",
        "--jq", '[.[] | select(.user.login == "openclaw[bot]")] | last // null',
    )
    
    if rc == 0 and stdout.strip() and stdout.strip() != "null":
        try:
            review = json.loads(stdout)
            if isinstance(review, dict):
                state = review.get("state", "")
                if state == "APPROVED":
                    return ConditionResult(
                        name="OpenClaw LLM review",
                        passed=True,
                        details="OpenClaw LLM review approved (PR review)",
                    )
                elif state == "CHANGES_REQUESTED":
                    body = review.get("body", "")[:200]
                    return ConditionResult(
                        name="OpenClaw LLM review",
                        passed=False,
                        details=f"OpenClaw requested changes: {body}",
                        blocked=True,
                    )
            # If COMMENTED or PENDING, continue to check jsonl
        except (json.JSONDecodeError, TypeError):
            pass

    # Second: Check jsonl file for recent review
    from pathlib import Path
    review_path = Path(os.path.expanduser("~/.openclaw/state/openclaw_pr_reviews.jsonl"))

    if review_path.exists():
        try:
            latest_review = None
            with open(review_path) as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        review = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if review.get("repo") == f"{owner}/{repo}" and review.get("pr_number") == pr_number:
                        latest_review = review

            if latest_review is not None:
                decision = latest_review.get("decision", "")
                if decision == "approve":
                    return ConditionResult(
                        name="OpenClaw LLM review",
                        passed=True,
                        details="OpenClaw LLM review approved",
                    )
                return ConditionResult(
                    name="OpenClaw LLM review",
                    passed=False,
                    details=f"OpenClaw review: {decision}",
                    blocked=True,
                )
        except Exception as e:
            logger.warning(f"Failed to read OpenClaw review state: {e}")

    # Third: No existing review found — fail-closed, require review before merge.
    # Do NOT auto-trigger LLM review from the gate check (the reviewer should run
    # as a separate step, not as a side-effect of checking merge readiness).
    return ConditionResult(
        name="OpenClaw LLM review",
        passed=False,
        details="No OpenClaw LLM review found — trigger review before merge",
        blocked=True,
    )


def check_merge_ready(owner: str, repo: str, pr_number: int) -> MergeVerdict:
    """Check all 6 merge conditions for a PR.

    Note: OpenClaw LLM review (condition 7) is disabled until the auto-trigger
    mechanism is built (orch-j9e0.4). The evidence pipeline (stage 1 self-review
    + future stage 2 independent reviewer) replaces it.  check_openclaw_review()
    remains available for manual use.
    """
    pr_url = f"https://github.com/{owner}/{repo}/pull/{pr_number}"

    conditions = [
        check_ci_status(owner, repo, pr_number),
        check_mergeable(owner, repo, pr_number),
        check_coderabbit(owner, repo, pr_number),
        check_blocking_comments(owner, repo, pr_number),
        check_evidence_pass(owner, repo, pr_number),
        # check_openclaw_review disabled — no auto-trigger exists yet (orch-j9e0.4)
    ]

    can_merge = not any(c.blocked for c in conditions)
    blocked_reasons = [c.details for c in conditions if c.blocked]

    return MergeVerdict(
        pr_url=pr_url,
        can_merge=can_merge,
        conditions=conditions,
        blocked_reasons=blocked_reasons,
    )


def main() -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Check if PR is ready to merge")
    parser.add_argument("owner", help="Repository owner")
    parser.add_argument("repo", help="Repository name")
    parser.add_argument("pr_number", type=int, help="PR number")
    parser.add_argument("--json", action="store_true", help="Output JSON")

    args = parser.parse_args()

    verdict = check_merge_ready(args.owner, args.repo, args.pr_number)

    if args.json:
        output = {
            "can_merge": verdict.can_merge,
            "pr_url": verdict.pr_url,
            "conditions": [
                {"name": c.name, "passed": c.passed, "details": c.details, "blocked": c.blocked}
                for c in verdict.conditions
            ],
            "blocked_reasons": verdict.blocked_reasons,
        }
        print(json.dumps(output, indent=2))
    else:
        print(f"PR: {verdict.pr_url}")
        print(f"Can merge: {verdict.can_merge}")
        print("\nConditions:")
        for c in verdict.conditions:
            status = "✅" if c.passed else "❌"
            blocked = " [BLOCKING]" if c.blocked else ""
            print(f"  {status} {c.name}: {c.details}{blocked}")

        if verdict.blocked_reasons:
            print("\nBlocked reasons:")
            for reason in verdict.blocked_reasons:
                print(f"  - {reason}")

    return 0 if verdict.can_merge else 1


if __name__ == "__main__":
    sys.exit(main())
