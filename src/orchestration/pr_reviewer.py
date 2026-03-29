"""PR review context builder — assemble full review context from memory + project rules.

This module builds a complete context for LLM-powered PR reviews by gathering:
- PR diff, commit messages, CI status via gh CLI
- CLAUDE.md rules (repo-level + global)
- OpenClaw memory (project memories, feedback memories)
- Prior review patterns from action_log.jsonl

No filtering, no pre-screening — everything goes into context for the LLM to read.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Helper functions for file operations (easier to mock in tests)
# ---------------------------------------------------------------------------


def _path_exists(path: Path) -> bool:
    """Check if a path exists. Wrapped for testability.

    Uses class method call to allow proper mocking in tests.
    """
    # Convert to string for mock compatibility (tests check string in path)
    return Path.exists(Path(str(path)))


def _read_file(path: Path) -> str:
    """Read file content. Wrapped for testability."""
    return path.read_text(encoding="utf-8")

logger = logging.getLogger(__name__)

# Constants
OPENCLAW_HOME = Path.home() / ".openclaw"
OPENCLAW_MEMORY_DIR = OPENCLAW_HOME / "memory"
OPENCLAW_STATE_DIR = OPENCLAW_HOME / "state"
ACTION_LOG_PATH = OPENCLAW_STATE_DIR / "action_log.jsonl"
GLOBAL_CLAUDE_MD = Path.home() / ".claude" / "CLAUDE.md"

# Default truncation threshold for large diffs
DEFAULT_MAX_DIFF_LINES = 300


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class GHPullRequestError(Exception):
    """Raised when PR data cannot be fetched from GitHub."""

    pass


class MemoryLoadError(Exception):
    """Raised when memory files cannot be loaded."""

    pass


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ReviewContext:
    """Complete context for LLM-powered PR review.

    Attributes:
        diff: The PR diff content (may be truncated for large PRs).
        commits: List of commits in the PR with sha and message.
        ci_status: CI status from GitHub (state + statuses).
        claude_md_rules: Combined CLAUDE.md rules (repo + global).
        memories: OpenClaw memories for this project.
        prior_patterns: Prior review decisions on similar PRs from action_log.
    """

    diff: str
    commits: list[dict]
    ci_status: dict
    claude_md_rules: str
    memories: str
    prior_patterns: str

    def __str__(self) -> str:
        """Serialize context for LLM consumption."""
        parts = [
            f"## PR Diff\n{self.diff}",
            f"## Commits\n{json.dumps(self.commits, indent=2)}",
            f"## CI Status\n{json.dumps(self.ci_status, indent=2)}",
            f"## CLAUDE.md Rules\n{self.claude_md_rules}",
            f"## OpenClaw Memories\n{self.memories}",
            f"## Prior Review Patterns\n{self.prior_patterns}",
        ]
        return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# GitHub PR data fetching
# ---------------------------------------------------------------------------


def _run_gh(args: list[str], timeout: int = 30) -> str:
    """Run a gh CLI command and return stdout.

    Raises:
        GHPullRequestError: If the command fails or gh is not found.
    """
    try:
        result = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            stderr = result.stderr or ""
            if "HTTP 404" in stderr or "Not Found" in stderr or result.returncode == 1:
                raise GHPullRequestError(f"PR not found: {stderr}")
            raise GHPullRequestError(f"gh command failed: {stderr}")
        return result.stdout.strip()
    except subprocess.TimeoutExpired as exc:
        raise GHPullRequestError(f"gh command timed out after {timeout}s") from exc
    except FileNotFoundError as exc:
        raise GHPullRequestError("gh CLI not found") from exc


def fetch_pr_diff(owner: str, repo: str, pr_number: int) -> str:
    """Fetch the diff for a PR.

    Args:
        owner: Repository owner (e.g., "jleechanorg").
        repo: Repository name (e.g., "claw").
        pr_number: PR number (e.g., 42).

    Returns:
        The diff content as a string.
    """
    args = [
        "pr", "diff", str(pr_number),
        "--repo", f"{owner}/{repo}",
    ]
    return _run_gh(args)


def fetch_pr_commits(owner: str, repo: str, pr_number: int) -> list[dict]:
    """Fetch commit messages for a PR.

    Args:
        owner: Repository owner.
        repo: Repository name.
        pr_number: PR number.

    Returns:
        List of commits with sha, message, author, and date.
    """
    args = [
        "pr", "view", str(pr_number),
        "--repo", f"{owner}/{repo}",
        "--json", "commits",
    ]
    raw = _run_gh(args)
    data = json.loads(raw)

    commits = []
    # Handle both dict format {"commits": [...]} and direct array [...]
    commit_list = data.get("commits", []) if isinstance(data, dict) else data if isinstance(data, list) else []

    for c in commit_list:
        commits.append({
            "sha": c.get("sha", ""),
            "message": c.get("commit", {}).get("message", ""),
            "author": c.get("commit", {}).get("author", {}).get("name", ""),
            "date": c.get("commit", {}).get("author", {}).get("date", ""),
        })
    return commits


def fetch_ci_status(owner: str, repo: str, pr_number: int) -> dict:
    """Fetch CI status for a PR.

    Args:
        owner: Repository owner.
        repo: Repository name.
        pr_number: PR number.

    Returns:
        Dict with 'state' and 'statuses' keys.
    """
    args = [
        "pr", "checks", str(pr_number),
        "--repo", f"{owner}/{repo}",
        "--json", "state,statuses",
    ]
    raw = _run_gh(args)
    data = json.loads(raw)

    # Handle empty response
    if not data:
        return {"state": "unknown", "statuses": []}

    # gh pr checks returns array directly
    if isinstance(data, list):
        state = "success"
        for check in data:
            check_state = (check.get("state") or "").upper()
            if check_state in ("FAILURE", "TIMED_OUT", "CANCELLED"):
                state = "failure"
                break
            elif check_state in ("PENDING", "QUEUED", "IN_PROGRESS"):
                state = "pending"
        return {
            "state": state,
            "statuses": data,
        }

    return {
        "state": data.get("state", "unknown"),
        "statuses": data.get("statuses", []),
    }


# ---------------------------------------------------------------------------
# CLAUDE.md loading
# ---------------------------------------------------------------------------


def load_claude_md_rules(repo_path: str) -> str:
    """Load CLAUDE.md rules from repo root and global config.

    Args:
        repo_path: Path to the repository root.

    Returns:
        Combined CLAUDE.md content, or empty string if neither exists.
    """
    repo_claude_md = Path(repo_path) / "CLAUDE.md"
    rules_parts: list[str] = []

    # Load repo-level CLAUDE.md if it exists
    if repo_claude_md.exists():
        try:
            with open(repo_claude_md, encoding="utf-8") as f:
                content = f.read()
            rules_parts.append(f"# Repo CLAUDE.md ({repo_path})\n{content}")
        except Exception as e:
            logger.warning(f"Failed to read repo CLAUDE.md: {e}")

    # Load global CLAUDE.md if it exists
    if GLOBAL_CLAUDE_MD.exists():
        try:
            with open(GLOBAL_CLAUDE_MD, encoding="utf-8") as f:
                content = f.read()
            rules_parts.append(f"# Global CLAUDE.md (~/.claude/CLAUDE.md)\n{content}")
        except Exception as e:
            logger.warning(f"Failed to read global CLAUDE.md: {e}")

    return "\n\n".join(rules_parts)


# ---------------------------------------------------------------------------
# OpenClaw memory loading
# ---------------------------------------------------------------------------


def load_openclaw_memory(owner: str, repo: str) -> str:
    """Load OpenClaw memories for a repository.

    Loads from ~/.openclaw/memory/ directory:
    - {owner}_{repo}_project.jsonl - project-specific memories
    - {owner}_{repo}_feedback.jsonl - feedback memories

    Args:
        owner: Repository owner.
        repo: Repository name.

    Returns:
        Combined memory content, or empty string if no memory files exist.
    """
    memory_parts: list[str] = []
    repo_key = f"{owner}_{repo}"

    # Look for memory files
    memory_files = [
        OPENCLAW_MEMORY_DIR / f"{repo_key}_project.jsonl",
        OPENCLAW_MEMORY_DIR / f"{repo_key}_feedback.jsonl",
    ]

    for memory_file in memory_files:
        if not memory_file.exists():
            continue

        try:
            with open(memory_file, encoding="utf-8") as f:
                content = f.read()
            if not content.strip():
                continue

            # Parse memory content - try JSONL first (one object per line),
            # then try JSON array format for compatibility
            memories = []
            lines = content.strip().split("\n")

            # Check if it's a JSON array (single line containing array)
            if len(lines) == 1 and lines[0].strip().startswith("["):
                try:
                    entries = json.loads(lines[0])
                    if isinstance(entries, list):
                        for entry in entries:
                            if isinstance(entry, dict):
                                memory_type = entry.get("type", "unknown")
                                memory_content = entry.get("content", "")
                                if memory_content:
                                    memories.append(f"[{memory_type}] {memory_content}")
                except (json.JSONDecodeError, TypeError):
                    pass
            else:
                # Standard JSONL format
                for line in lines:
                    if not line.strip():
                        continue
                    try:
                        entry = json.loads(line)
                        if isinstance(entry, dict):
                            memory_type = entry.get("type", "unknown")
                            memory_content = entry.get("content", "")
                            if memory_content:
                                memories.append(f"[{memory_type}] {memory_content}")
                    except json.JSONDecodeError:
                        continue

            if memories:
                memory_type_label = memory_file.stem.replace(f"{repo_key}_", "")
                memory_parts.append(f"## {memory_type_label.upper()} ({repo_key})\n" + "\n".join(memories))

        except Exception as e:
            logger.warning(f"Failed to read memory file {memory_file}: {e}")

    return "\n\n".join(memory_parts)


# ---------------------------------------------------------------------------
# Prior patterns loading
# ---------------------------------------------------------------------------


def load_prior_patterns(owner: str, repo: str) -> str:
    """Load prior review decisions for a repository from action log.

    Scans ~/.openclaw/state/action_log.jsonl for past review decisions
    on the same repo to provide historical context.

    Args:
        owner: Repository owner.
        repo: Repository name.

    Returns:
        Summary of prior review patterns, or empty string if no log exists.
    """
    if not ACTION_LOG_PATH.exists():
        return ""

    repo_key = f"{owner}/{repo}"
    review_entries: list[dict] = []

    try:
        with open(ACTION_LOG_PATH, encoding="utf-8") as f:
            content = f.read()
        for line in content.strip().split("\n"):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                # Filter to review-related actions for this repo
                action_type = entry.get("action_type", "")
                entry_repo = entry.get("repo", "")

                if entry_repo == repo_key and "review" in action_type:
                    review_entries.append(entry)
            except json.JSONDecodeError:
                continue
    except Exception as e:
        logger.warning(f"Failed to read action log: {e}")
        return ""

    if not review_entries:
        return ""

    # Summarize the patterns
    pattern_parts = [f"## Prior Review Patterns for {repo_key}"]

    # Group by action type
    action_counts: dict[str, int] = {}
    for entry in review_entries:
        action = entry.get("action_type", "unknown")
        action_counts[action] = action_counts.get(action, 0) + 1

    pattern_parts.append("\n### Action Counts")
    for action, count in sorted(action_counts.items()):
        pattern_parts.append(f"- {action}: {count}")

    # Include recent entries (last 5)
    recent = review_entries[-5:]
    pattern_parts.append("\n### Recent Reviews")
    for entry in recent:
        action = entry.get("action_type", "unknown")
        timestamp = entry.get("timestamp", "unknown")
        summary = entry.get("summary", "")[:100]
        pattern_parts.append(f"- {timestamp}: {action} - {summary}")

    return "\n".join(pattern_parts)


# ---------------------------------------------------------------------------
# Diff truncation
# ---------------------------------------------------------------------------


def truncate_diff(diff: str, max_lines: int = DEFAULT_MAX_DIFF_LINES) -> str:
    """Truncate large diffs with a summary note.

    Args:
        diff: The diff content.
        max_lines: Maximum number of lines to keep (default: 300).

    Returns:
        The diff, truncated if it exceeds max_lines, with a summary note.
    """
    lines = diff.split("\n")
    if len(lines) <= max_lines:
        return diff

    # Truncate with summary
    kept_lines = lines[:max_lines]
    truncated_diff = "\n".join(kept_lines)

    truncation_note = f"""\
================================================================================
NOTE: This diff was truncated from {len(lines)} to {max_lines} lines. The LLM should flag this
PR as potentially needing human review due to size. Review the summary above
and consider requesting changes or escalating to Jeffrey if the changes are
complex or touch sensitive areas.
================================================================================
"""

    header = f"[DIFF TRUNCATED - {len(lines)} lines total, showing first {max_lines} lines]\n\n"

    # Add continuation marker
    if kept_lines and not kept_lines[-1].endswith("..."):
        truncated_diff += f"\n... ({len(lines) - max_lines} more lines)"

    return header + truncated_diff + "\n" + truncation_note


# ---------------------------------------------------------------------------
# Main context builder
# ---------------------------------------------------------------------------


def build_review_context(owner: str, repo: str, pr_number: int) -> ReviewContext:
    """Build complete review context for a PR.

    Assembles all available context sources:
    - PR diff (may be truncated for large PRs)
    - PR commits
    - CI status
    - CLAUDE.md rules
    - OpenClaw memories
    - Prior review patterns

    Args:
        owner: Repository owner (e.g., "jleechanorg").
        repo: Repository name (e.g., "claw").
        pr_number: PR number (e.g., 42).

    Returns:
        ReviewContext with all assembled data.
    """
    # Fetch PR data
    diff = ""
    commits: list[dict] = []
    ci_status: dict = {"state": "unknown", "statuses": []}

    try:
        diff = fetch_pr_diff(owner, repo, pr_number)
    except GHPullRequestError as e:
        logger.warning(f"Failed to fetch PR diff: {e}")
        diff = f"[ERROR: Could not fetch diff - {e}]"

    try:
        commits = fetch_pr_commits(owner, repo, pr_number)
    except GHPullRequestError as e:
        logger.warning(f"Failed to fetch PR commits: {e}")

    try:
        ci_status = fetch_ci_status(owner, repo, pr_number)
    except GHPullRequestError as e:
        logger.warning(f"Failed to fetch CI status: {e}")

    # Truncate diff if needed
    diff = truncate_diff(diff)

    # Load context from files
    # For CLAUDE.md, we need the repo path - try to find it
    claude_md_rules = ""
    try:
        # Try to find repo in common locations
        repo_path = _find_repo_path(owner, repo)
        if repo_path:
            claude_md_rules = load_claude_md_rules(repo_path)
    except Exception as e:
        logger.warning(f"Failed to load CLAUDE.md rules: {e}")

    # Load OpenClaw memory
    memories = ""
    try:
        memories = load_openclaw_memory(owner, repo)
    except Exception as e:
        logger.warning(f"Failed to load OpenClaw memory: {e}")

    # Load prior patterns
    prior_patterns = ""
    try:
        prior_patterns = load_prior_patterns(owner, repo)
    except Exception as e:
        logger.warning(f"Failed to load prior patterns: {e}")

    return ReviewContext(
        diff=diff,
        commits=commits,
        ci_status=ci_status,
        claude_md_rules=claude_md_rules,
        memories=memories,
        prior_patterns=prior_patterns,
    )


def _find_repo_path(owner: str, repo: str) -> Optional[str]:
    """Find the local path to a repository.

    Checks common locations where repositories might be cloned.

    Args:
        owner: Repository owner.
        repo: Repository name.

    Returns:
        Path to repo if found, None otherwise.
    """
    # Check in ~/projects/ (common location)
    projects_dir = Path.home() / "projects"
    if projects_dir.exists():
        repo_path = projects_dir / repo
        if repo_path.exists() and (repo_path / ".git").exists():
            return str(repo_path)

    # Check in ~/repos/
    repos_dir = Path.home() / "repos"
    if repos_dir.exists():
        repo_path = repos_dir / repo
        if repo_path.exists() and (repo_path / ".git").exists():
            return str(repo_path)

    # Check in ~/dev/
    dev_dir = Path.home() / "dev"
    if dev_dir.exists():
        repo_path = dev_dir / repo
        if repo_path.exists() and (repo_path / ".git").exists():
            return str(repo_path)

    # Check in current directory's parent (for worktrees)
    cwd = Path.cwd()
    if cwd.exists():
        # Check if current directory might be a worktree
        potential_base = cwd.parent
        repo_path = potential_base / repo
        if repo_path.exists() and (repo_path / ".git").exists():
            return str(repo_path)

    return None
