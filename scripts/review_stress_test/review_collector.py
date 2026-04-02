#!/usr/bin/env python3
"""AI Review Collector - waits for and collects AI reviewer comments."""
from __future__ import annotations

import json
import time
import subprocess
from dataclasses import dataclass
from typing import Optional
from datetime import datetime, timezone


AI_REVIEWERS = {
    "coderabbitai[bot]": "CodeRabbit",
    "cursor[bot]": "Cursor Bugbot",
    "copilot[bot]": "Copilot",
    "chatgpt-codex-connector[bot]": "Codex",
}


@dataclass
class ReviewComment:
    reviewer: str
    severity: str  # Critical, Major, Medium, Minor, None
    path: str
    line: Optional[int]
    body: str
    is_inline: bool
    created_at: datetime
    state: str = "COMMENTED"  # "COMMENTED" or "RESOLVED" (review comments only)


def _parse_gh_jq_output(output: str) -> list[dict]:
    """Parse gh --jq output which returns newline-separated JSON objects."""
    results: list[dict] = []
    for line in output.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            results.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return results


class ReviewCollector:
    """Collects AI review comments from PRs."""

    def __init__(self, owner: str, repo: str, pr: int) -> None:
        self.owner = owner
        self.repo = repo
        self.pr = pr

    def _run(self, cmd: list[str]) -> str:
        """Run gh command."""
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            return ""
        return result.stdout

    def get_comments(self, created_after: Optional[datetime] = None) -> list[ReviewComment]:
        """Get all AI review comments from PR, optionally filtered by creation time."""
        comments: list[ReviewComment] = []

        # Get review comments (inline)
        output = self._run([
            "gh", "api",
            f"repos/{self.owner}/{self.repo}/pulls/{self.pr}/comments",
            "--jq", '.[] | {body:.body,path:.path,line:.line,user:.user.login,created_at:.created_at,state:.state}'
        ])

        for item in _parse_gh_jq_output(output):
            reviewer = item.get("user", "")
            if reviewer in AI_REVIEWERS:
                state = item.get("state", "COMMENTED")
                # Skip resolved comments
                if state == "RESOLVED":
                    continue
                body = item.get("body", "")
                severity = self._extract_severity(body)
                created_at_str = item.get("created_at", "")
                try:
                    created_at = datetime.fromisoformat(created_at_str.replace('Z', '+00:00'))
                except (ValueError, AttributeError):
                    created_at = datetime.now(timezone.utc)

                # Skip comments created before the cutoff
                if created_after and created_at <= created_after:
                    continue

                comments.append(ReviewComment(
                    reviewer=AI_REVIEWERS[reviewer],
                    severity=severity,
                    path=item.get("path", ""),
                    line=item.get("line"),
                    body=body,
                    is_inline=True,
                    created_at=created_at,
                    state=state
                ))

        # Get issue comments (general)
        output = self._run([
            "gh", "api",
            f"repos/{self.owner}/{self.repo}/issues/{self.pr}/comments",
            "--jq", '.[] | {body:.body,user:.user.login,created_at:.created_at}'
        ])

        for item in _parse_gh_jq_output(output):
            reviewer = item.get("user", "")
            if reviewer in AI_REVIEWERS:
                body = item.get("body", "")
                severity = self._extract_severity(body)
                created_at_str = item.get("created_at", "")
                try:
                    created_at = datetime.fromisoformat(created_at_str.replace('Z', '+00:00'))
                except (ValueError, AttributeError):
                    created_at = datetime.now(timezone.utc)
                
                # Skip comments created before the cutoff
                if created_after and created_at <= created_after:
                    continue
                
                comments.append(ReviewComment(
                    reviewer=AI_REVIEWERS[reviewer],
                    severity=severity,
                    path="",
                    line=None,
                    body=body,
                    is_inline=False,
                    created_at=created_at,
                    state="COMMENTED"
                ))

        return comments

    def _extract_severity(self, body: str) -> str:
        """Extract severity from comment body."""
        body_lower = body.lower()
        if "critical" in body_lower or "blocker" in body_lower:
            return "Critical"
        if "major" in body_lower:
            return "Major"
        if "minor" in body_lower:
            return "Minor"
        if "suggestion" in body_lower or "nit" in body_lower:
            return "Minor"
        return "Medium"

    def wait_for_reviews(
        self, timeout_min: int = 30, poll_interval_sec: int = 60
    ) -> list[ReviewComment]:
        """Poll until AI reviewers have commented or timeout."""
        start = time.time()
        timeout_sec = timeout_min * 60

        while time.time() - start < timeout_sec:
            comments = self.get_comments()
            ai_comments = [c for c in comments if c.reviewer in AI_REVIEWERS.values()]

            if ai_comments:
                print(f"Got {len(ai_comments)} AI review comments")
                return ai_comments

            print(f"No AI reviews yet, waiting... ({int((time.time() - start)/60)}m)")
            time.sleep(poll_interval_sec)

        # Return what we have at timeout
        return self.get_comments()

    def check_unresolved_comments(self, created_after: Optional[datetime] = None) -> list[ReviewComment]:
        """Check for unresolved AI review comments (re-check after fix push).
        
        Args:
            created_after: Only return comments created after this timestamp.
                          This filters out old comments that were already addressed.
        """
        return self.get_comments(created_after=created_after)

    def summarize_comments(self, comments: list[ReviewComment]) -> dict:
        """Summarize comments by reviewer and severity."""
        summary: dict = {
            "total": len(comments),
            "by_reviewer": {},
            "by_severity": {"Critical": 0, "Major": 0, "Medium": 0, "Minor": 0}
        }

        for c in comments:
            if c.reviewer not in summary["by_reviewer"]:
                summary["by_reviewer"][c.reviewer] = 0
            summary["by_reviewer"][c.reviewer] += 1

            if c.severity in summary["by_severity"]:
                summary["by_severity"][c.severity] += 1

        return summary

    def format_comments_for_agent(self, comments: list[ReviewComment]) -> str:
        """Format comments into a prompt-friendly summary for the fixing agent."""
        lines: list[str] = []
        for i, c in enumerate(comments, 1):
            location = f"{c.path}:{c.line}" if c.path and c.line else (c.path or "general")
            lines.append(f"{i}. [{c.severity}] {c.reviewer} @ {location}")
            # Truncate very long comment bodies
            body = c.body[:500] + "..." if len(c.body) > 500 else c.body
            lines.append(f"   {body}")
            lines.append("")
        return "\n".join(lines)


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        print("Usage: review_collector.py <owner/repo> <pr_number>")
        sys.exit(1)

    owner, repo = sys.argv[1].split("/")
    pr = int(sys.argv[2])

    collector = ReviewCollector(owner, repo, pr)
    comments = collector.wait_for_reviews(timeout_min=5)

    print(f"\nFound {len(comments)} comments:")
    for c in comments:
        print(f"  [{c.severity}] {c.reviewer}: {c.path}:{c.line}")

    print("\nSummary:", collector.summarize_comments(comments))
