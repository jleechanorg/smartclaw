"""PR Reviewer Agent — independent review via merge_gate + MCP mail.

Dispatched by AO when a coder agent signals "PR ready for review".
Checks all merge conditions, reviews the diff, and posts a GitHub
PR review (APPROVE or REQUEST_CHANGES). Communicates findings to the
coder via MCP mail for a fix loop.

This replaces the evidence pipeline (evidence_bundle.py + stage2_reviewer.py).
Instead of generating verdict.json files, the reviewer posts a real GitHub
PR review that merge_gate can read via the standard reviews API.

Usage (standalone):
    python -m orchestration.reviewer_agent <owner> <repo> <pr_number>

Usage (via AO dispatch):
    Triggered by MCP mail message with subject matching "PR #N ready"
"""

from __future__ import annotations

import logging
import re
import sys
from dataclasses import dataclass
from enum import StrEnum

logger = logging.getLogger(__name__)


class FindingSeverity(StrEnum):
    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"
    INFO = "info"


class ReviewVerdict(StrEnum):
    APPROVE = "APPROVE"
    REQUEST_CHANGES = "REQUEST_CHANGES"


@dataclass
class ReviewFinding:
    """A single finding from the diff review."""

    severity: FindingSeverity
    file: str
    description: str


@dataclass
class ReviewResult:
    """Complete review result."""

    gate_passed: bool
    gate_details: dict  # condition name → {passed, details, blocked}
    findings: list[ReviewFinding]
    verdict: ReviewVerdict
    summary: str
    github_posted: bool = False
    actual_event: str = ""  # Event actually posted: APPROVE, REQUEST_CHANGES, or COMMENT (when fallback)
    verdict_comment_posted: bool = False


from orchestration.merge_gate import run_gh as _run_gh


def check_gate(owner: str, repo: str, pr_number: int) -> dict:
    """Run merge_gate and return condition results as a dict.

    Returns dict of {condition_name: {passed, details, blocked}}.
    Imports merge_gate lazily to avoid circular imports when used standalone.
    """
    from orchestration.merge_gate import check_merge_ready

    verdict = check_merge_ready(owner, repo, pr_number)
    return {
        c.name: {"passed": c.passed, "details": c.details, "blocked": c.blocked}
        for c in verdict.conditions
    }


def get_pr_diff(owner: str, repo: str, pr_number: int) -> str:
    """Fetch PR diff via gh CLI. Returns diff text, truncated at 50k chars."""
    rc, stdout, stderr = _run_gh(
        "pr", "diff", str(pr_number), "--repo", f"{owner}/{repo}",
    )
    if rc != 0:
        logger.warning("Failed to get PR diff: %s", stderr)
        return ""
    # Truncate large diffs
    if len(stdout) > 50_000:
        return stdout[:50_000] + f"\n\n... (truncated, {len(stdout):,} total chars)"
    return stdout



def _is_test_path(path: str) -> bool:
    """Check if a file path is a test file. Avoids false positives on paths like contest/."""
    p = path.lower().replace("\\", "/")
    basename = p.split("/")[-1] if "/" in p else p
    # Check directory components and file naming conventions
    # Also exempt src/tests/ directory (common in monorepos)
    return (
        "/tests/" in p or "/test/" in p
        or p.startswith("tests/") or p.startswith("test/")
        or basename in {"test.py", "tests.py"}
        or basename.startswith("test_") or basename.endswith("_test.py")
    )


def review_diff(diff: str) -> list[ReviewFinding]:
    """Analyze diff for common issues. Returns findings.

    This is a basic structural review — checks for obvious problems.
    The real value is that a different model family reviews the code,
    catching things the coder's model might miss.
    """
    findings: list[ReviewFinding] = []

    if not diff:
        findings.append(ReviewFinding(
            severity=FindingSeverity.CRITICAL,
            file="(all)",
            description="Could not retrieve PR diff — cannot review",
        ))
        return findings

    # Check for common issues in diff
    lines = diff.split("\n")
    current_file = ""

    for i, line in enumerate(lines, start=1):
        # Track current file
        if line.startswith("+++ b/"):
            current_file = line[6:]
            continue

        # Only check added lines
        if not line.startswith("+") or line.startswith("+++"):
            continue

        content = line[1:]  # strip leading +

        # Hardcoded secrets/tokens (skip test files — they use fake values)
        is_test = _is_test_path(current_file)
        if not is_test and re.search(r'(api_key|secret|token|password)\s*=\s*["\'][^"\']{8,}', content, re.IGNORECASE):
            findings.append(ReviewFinding(
                severity=FindingSeverity.CRITICAL,
                file=current_file,
                description=f"Possible hardcoded secret on line ~{i}",
            ))

        # Debug statements in production code (skip tests, allow print in __main__ CLIs)
        if current_file.endswith(".py") and not is_test:
            if re.match(r'\s*(breakpoint|pdb\.set_trace)\(', content):
                findings.append(ReviewFinding(
                    severity=FindingSeverity.MAJOR,
                    file=current_file,
                    description=f"Debug statement (breakpoint/pdb) on line ~{i}",
                ))

        # TODO/FIXME/HACK
        if re.search(r'\b(TODO|FIXME|HACK|XXX)\b', content):
            findings.append(ReviewFinding(
                severity=FindingSeverity.INFO,
                file=current_file,
                description=f"TODO/FIXME marker on line ~{i}: {content.strip()[:80]}",
            ))

    return findings


def build_review_body(
    gate_results: dict,
    findings: list[ReviewFinding],
) -> tuple[ReviewVerdict, str]:
    """Build GitHub PR review body. Returns (verdict, body_markdown)."""
    # Determine verdict
    blocking_gate = [name for name, r in gate_results.items() if r["blocked"]]
    critical_findings = [f for f in findings if f.severity in {FindingSeverity.CRITICAL, FindingSeverity.MAJOR}]

    if blocking_gate or critical_findings:
        verdict = ReviewVerdict.REQUEST_CHANGES
    else:
        verdict = ReviewVerdict.APPROVE

    lines = ["## Independent PR Review\n"]

    # Gate status
    lines.append(f"### Merge Gate ({len(gate_results)} conditions)\n")
    lines.append("| Condition | Status | Details |")
    lines.append("|-----------|--------|---------|")
    for name, r in gate_results.items():
        icon = "PASS" if r["passed"] else "BLOCKED" if r["blocked"] else "WARN"
        details_safe = r["details"].replace("\n", " ")[:80]
        lines.append(f"| {name} | {icon} | {details_safe} |")

    # Findings
    if findings:
        lines.append("\n### Findings\n")
        for f in findings:
            severity_tag = f"**{f.severity.upper()}**"
            lines.append(f"- {severity_tag} `{f.file}`: {f.description}")
    else:
        lines.append("\n### Findings\n\nNo issues found.")

    # Verdict
    lines.append(f"\n### Verdict: **{verdict}**\n")
    if verdict == ReviewVerdict.APPROVE:
        lines.append("All merge conditions met, no blocking findings. Approved.")
    else:
        if blocking_gate:
            lines.append(f"Blocked by gate conditions: {', '.join(blocking_gate)}")
        if critical_findings:
            lines.append(f"{len(critical_findings)} critical/major finding(s) require attention.")

    return verdict, "\n".join(lines)


def post_github_review(
    owner: str,
    repo: str,
    pr_number: int,
    verdict: str,
    body: str,
) -> tuple[bool, str]:
    """Post a GitHub PR review (APPROVE or REQUEST_CHANGES).

    Falls back to COMMENT if APPROVE/REQUEST_CHANGES fails (e.g., can't
    approve your own PR). The verdict is always in the body text.

    Returns (success, actual_event) — actual_event is APPROVE, REQUEST_CHANGES,
    or COMMENT when fallback occurred, so callers know what was really posted.
    """
    # GitHub API event values: APPROVE, REQUEST_CHANGES, COMMENT
    event = verdict  # already "APPROVE" or "REQUEST_CHANGES"

    rc, stdout, stderr = _run_gh(
        "api", f"repos/{owner}/{repo}/pulls/{pr_number}/reviews",
        "--method", "POST",
        "--field", f"event={event}",
        "--field", f"body={body}",
    )

    if rc != 0:
        # Can't approve own PR or other permission issues — fall back to COMMENT
        logger.warning("Could not post %s review (likely self-review), falling back to COMMENT", event)
        rc2, _, stderr2 = _run_gh(
            "api", f"repos/{owner}/{repo}/pulls/{pr_number}/reviews",
            "--method", "POST",
            "--field", "event=COMMENT",
            "--field", f"body={body}",
        )
        if rc2 != 0:
            logger.error("Failed to post review even as COMMENT: %s", stderr2)
            return False, ""
        logger.info("Posted %s verdict as COMMENT on PR #%d (%s not permitted)", verdict, pr_number, event)
        return True, "COMMENT"

    logger.info("Posted %s review on PR #%d", verdict, pr_number)
    return True, event


# Marker format used by merge_gate to detect reviewer verdicts in PR comments
VERDICT_MARKER = "<!-- reviewer-verdict: {verdict} sha:{sha} -->"


def _post_verdict_comment(
    owner: str, repo: str, pr_number: int, verdict: str, reviewed_sha: str,
) -> bool:
    """Post a PR comment with a machine-readable verdict marker.

    Uses the reviewed commit SHA and refuses to post if PR HEAD moved since
    review started, preventing accidental approval of unreviewed commits.
    """
    rc, sha_out, _ = _run_gh(
        "api", f"repos/{owner}/{repo}/pulls/{pr_number}",
        "--jq", ".head.sha",
    )
    if rc != 0 or not sha_out.strip():
        logger.warning("Could not fetch HEAD SHA for verdict comment")
        return False

    current_head = sha_out.strip()
    if current_head != reviewed_sha:
        logger.warning(
            "Refusing verdict comment: PR HEAD moved from reviewed %s to %s",
            reviewed_sha[:8],
            current_head[:8],
        )
        return False

    marker = VERDICT_MARKER.format(verdict=verdict, sha=reviewed_sha)
    comment_body = f"{marker}\n**Reviewer verdict: {verdict}** for commit `{reviewed_sha[:8]}`"

    rc2, _, stderr = _run_gh(
        "api", f"repos/{owner}/{repo}/issues/{pr_number}/comments",
        "--method", "POST",
        "--field", f"body={comment_body}",
    )
    if rc2 != 0:
        logger.warning("Failed to post verdict comment: %s", stderr)
        return False
    logger.info("Posted verdict comment on PR #%d: %s at %s", pr_number, verdict, reviewed_sha[:8])
    return True


def format_mail_findings(
    gate_results: dict,
    findings: list[ReviewFinding],
    pr_number: int,
) -> str:
    """Format findings as MCP mail body for the coder agent."""
    lines = [f"# PR #{pr_number} Review Findings\n"]

    # Gate blockers
    blocked = {name: r for name, r in gate_results.items() if r["blocked"]}
    if blocked:
        lines.append("## Gate Blockers\n")
        for name, r in blocked.items():
            lines.append(f"- **{name}**: {r['details']}")
        lines.append("")

    # Code findings
    critical = [f for f in findings if f.severity in {FindingSeverity.CRITICAL, FindingSeverity.MAJOR}]
    if critical:
        lines.append("## Code Issues (must fix)\n")
        for f in critical:
            lines.append(f"- **{f.severity.upper()}** `{f.file}`: {f.description}")
        lines.append("")

    minor = [f for f in findings if f.severity not in {FindingSeverity.CRITICAL, FindingSeverity.MAJOR}]
    if minor:
        lines.append("## Minor / Info\n")
        for f in minor:
            lines.append(f"- {f.severity} `{f.file}`: {f.description}")

    if not blocked and not findings:
        lines.append("No issues found. PR looks good!")

    return "\n".join(lines)


def run_review(
    owner: str,
    repo: str,
    pr_number: int,
    post_github: bool = True,
    coder_agent: str = "",
) -> ReviewResult:
    """Run a complete PR review.

    1. Check merge gate
    2. Fetch and review diff
    3. Build review body
    4. Post GitHub PR review

    Returns ReviewResult with all details.
    """
    logger.info("Starting review of %s/%s#%d", owner, repo, pr_number)

    # Step 1: Check merge gate
    gate_results = check_gate(owner, repo, pr_number)
    gate_passed = not any(r["blocked"] for r in gate_results.values())

    # Step 2: Fetch PR HEAD SHA (captured before diff so reviewed_sha is stable)
    rc_sha, sha_out, _ = _run_gh(
        "api", f"repos/{owner}/{repo}/pulls/{pr_number}", "--jq", ".head.sha",
    )
    reviewed_sha = sha_out.strip() if rc_sha == 0 else ""
    if not reviewed_sha:
        logger.warning("Could not fetch PR HEAD SHA — verdict comment will be skipped")

    # Step 3: Fetch diff
    diff = get_pr_diff(owner, repo, pr_number)

    # Step 4: Review diff
    findings = review_diff(diff)

    # Step 5: Build review
    verdict, body = build_review_body(gate_results, findings)

    # Step 6: Post GitHub review + verdict comment
    posted = False
    actual_event = ""
    verdict_posted = False
    if post_github:
        # Re-check HEAD SHA to close TOCTOU window (diff fetch may be slow).
        # Fail closed: if the API call fails, skip posting (don't assume SHA matches).
        if reviewed_sha:
            rc_recheck, sha_recheck, _ = _run_gh(
                "api", f"repos/{owner}/{repo}/pulls/{pr_number}", "--jq", ".head.sha",
            )
            current_head = sha_recheck.strip() if rc_recheck == 0 else ""
            if not current_head or current_head != reviewed_sha:
                reason = "API error" if not current_head else f"{reviewed_sha[:8]} → {current_head[:8]}"
                logger.warning(
                    "TOCTOU: PR HEAD check failed (%s) — skipping post",
                    reason,
                )
                return ReviewResult(
                    gate_passed=gate_passed,
                    gate_details=gate_results,
                    findings=findings,
                    verdict=verdict,
                    summary=body,
                )

        posted, actual_event = post_github_review(owner, repo, pr_number, verdict, body)
        if not posted:
            logger.warning("Failed to post GitHub review — continuing anyway")
        elif actual_event and actual_event != verdict:
            logger.warning("Posted as %s (requested %s not permitted)", actual_event, verdict)
        # Post machine-readable verdict comment (merge_gate reads this)
        # Skip APPROVE verdict marker when review was downgraded to COMMENT
        # (e.g. self-review fallback). REQUEST_CHANGES markers are still posted
        # even when downgraded, so the gate sees the blocking signal.
        downgraded = actual_event == "COMMENT" and verdict == ReviewVerdict.APPROVE
        if posted and reviewed_sha and not downgraded:
            verdict_posted = _post_verdict_comment(owner, repo, pr_number, verdict, reviewed_sha)
            if not verdict_posted:
                logger.warning("Failed to post verdict comment — gate signal may be lost")
        elif downgraded:
            logger.warning("Skipping verdict comment — review was downgraded to COMMENT (self-review?)")

    # Phase 2: send format_mail_findings(gate_results, findings, pr_number) to coder via MCP mail
    if coder_agent:
        try:
            from orchestration.mcp_mail import send_mail
            mail_body = format_mail_findings(gate_results, findings, pr_number)
            mail_sent = send_mail(
                to=coder_agent,
                subject=f"PR #{pr_number} Review Findings",
                body_md=mail_body,
            )
            if mail_sent:
                logger.info("Sent review findings to %s via MCP mail", coder_agent)
            else:
                logger.warning("Failed to send MCP mail to %s", coder_agent)
        except Exception as e:
            logger.warning("Failed to send MCP mail to %s: %s", coder_agent, e)

    return ReviewResult(
        gate_passed=gate_passed,
        gate_details=gate_results,
        findings=findings,
        verdict=verdict,
        summary=body,
        github_posted=posted,
        actual_event=actual_event if posted else "",
        verdict_comment_posted=verdict_posted,
    )


def main() -> int:
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Independent PR reviewer agent")
    parser.add_argument("owner", help="Repository owner")
    parser.add_argument("repo", help="Repository name")
    parser.add_argument("pr_number", type=int, help="PR number")
    parser.add_argument("--no-post", action="store_true", help="Don't post GitHub review (dry run)")
    parser.add_argument("--coder-agent", type=str, default="", help="Agent ID to send review findings via MCP mail (Phase 2)")

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    result = run_review(
        args.owner,
        args.repo,
        args.pr_number,
        post_github=not args.no_post,
        coder_agent=args.coder_agent,
    )

    print(f"\nVerdict: {result.verdict}")
    print(f"Gate passed: {result.gate_passed}")
    print(f"GitHub posted: {result.github_posted}")
    if result.actual_event and result.actual_event != result.verdict:
        print(f"Actual event posted: {result.actual_event} (requested {result.verdict} not permitted)")
    if result.findings:
        print(f"Findings: {len(result.findings)}")
        for f in result.findings:
            print(f"  [{f.severity}] {f.file}: {f.description}")

    # Fail if posting was requested but failed, or if verdict is not APPROVE
    if not args.no_post and not result.github_posted:
        return 1
    # Fail if we wanted APPROVE but could only post COMMENT (e.g. self-review)
    if not args.no_post and result.verdict == ReviewVerdict.APPROVE and result.actual_event == "COMMENT":
        return 1
    # Fail if verdict comment (machine-readable gate signal) could not be posted
    if not args.no_post and result.github_posted and not result.verdict_comment_posted:
        logger.warning("Verdict comment was not posted — merge gate will not see the signal")
        return 1
    return 0 if result.verdict == ReviewVerdict.APPROVE else 1


if __name__ == "__main__":
    sys.exit(main())
