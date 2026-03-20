"""Tests for pr_reviewer: assembling review context from memory + project rules.

These tests verify that the PR review context builder correctly assembles all
available context sources for the LLM to make review decisions:
- PR diff, commit messages, CI status via gh CLI
- CLAUDE.md rules (repo-level + global)
- OpenClaw memory (project memories, feedback memories, user preferences)
- Prior review patterns from action_log.jsonl
- Handle missing memory gracefully (no memory → still review)
- Large diffs truncated with summary note

These tests will fail until pr_reviewer.py is implemented (TDD).
"""

from __future__ import annotations

import json
import os
import pytest
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open
from dataclasses import dataclass


# These imports will fail until pr_reviewer.py is implemented (TDD)
from orchestration.pr_reviewer import (
    build_review_context,
    ReviewContext,
    fetch_pr_diff,
    fetch_pr_commits,
    fetch_ci_status,
    load_claude_md_rules,
    load_openclaw_memory,
    load_prior_patterns,
    truncate_diff,
    GHPullRequestError,
    MemoryLoadError,
)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@dataclass
class MockPRDiff:
    """Mock PR diff for testing."""

    @staticmethod
    def small_diff() -> str:
        """Small diff that won't be truncated."""
        return """diff --git a/src/orchestration/test_file.py b/src/orchestration/test_file.py
index 1234567..89abcdef 100644
--- a/src/orchestration/test_file.py
+++ b/src/orchestration/test_file.py
@@ -1,5 +1,6 @@
+import pytest
 from __future__ import annotations

 def hello():
     print("Hello, world!")
+
"""

    @staticmethod
    def large_diff() -> str:
        """Large diff that exceeds truncation threshold."""
        # Generate a large diff (1000+ lines)
        lines = ["+added line " + str(i) for i in range(500)]
        lines.extend(["-removed line " + str(i) for i in range(500)])
        return "\n".join(lines)

    @staticmethod
    def truncated_result() -> str:
        """Expected truncated diff with summary note."""
        return """[DIFF TRUNCATED - 1000 lines total, showing first 300 lines]

diff --git a/src/orchestration/test_file.py b/src/orchestration/test_file.py
index 1234567..89abcdef 100644
--- a/src/orchestration/test_file.py
+++ b/src/orchestration/test_file.py
@@ -1,5 +1,6 @@
+import pytest
... (293 more lines)

================================================================================
NOTE: This diff was truncated from 1000 to 300 lines. The LLM should flag this
PR as potentially needing human review due to size. Review the summary above
and consider requesting changes or escalating to Jeffrey if the changes are
complex or touch sensitive areas.
================================================================================
"""


@dataclass
class MockPRCommits:
    """Mock PR commit messages."""

    @staticmethod
    def commits_json() -> str:
        return json.dumps([
            {
                "sha": "abc123",
                "commit": {
                    "message": "feat: add auth middleware",
                    "author": {"name": "Claude", "date": "2026-03-14T10:00:00Z"}
                }
            },
            {
                "sha": "def456",
                "commit": {
                    "message": "fix: resolve auth middleware tests",
                    "author": {"name": "Claude", "date": "2026-03-14T11:00:00Z"}
                }
            },
        ])


@dataclass
class MockCIStatus:
    """Mock CI status responses."""

    @staticmethod
    def success_json() -> str:
        return json.dumps({
            "state": "success",
            "statuses": [
                {"context": "github-actions", "state": "success"},
                {"context": "codecov", "state": "success"},
            ]
        })

    @staticmethod
    def failure_json() -> str:
        return json.dumps({
            "state": "failure",
            "statuses": [
                {"context": "github-actions", "state": "failure"},
                {"context": "codecov", "state": "success"},
            ]
        })

    @staticmethod
    def pending_json() -> str:
        return json.dumps({
            "state": "pending",
            "statuses": [
                {"context": "github-actions", "state": "pending"},
            ]
        })


# ---------------------------------------------------------------------------
# fetch_pr_diff tests
# ---------------------------------------------------------------------------


@patch("orchestration.pr_reviewer.subprocess.run")
def test_fetch_pr_diff_success(mock_run: MagicMock) -> None:
    """fetch_pr_diff should return diff content from gh CLI."""
    # Arrange
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = MockPRDiff.small_diff()
    mock_result.stderr = ""
    mock_run.return_value = mock_result

    # Act
    result = fetch_pr_diff("jleechanorg", "claw", 42)

    # Assert
    mock_run.assert_called_once()
    call_args = mock_run.call_args
    assert "pr" in call_args[0][0]
    assert "42" in call_args[0][0]
    assert "diff" in call_args[0][0]
    assert "jleechanorg/claw" in call_args[0][0]
    assert "1234567" in result  # The diff content should be returned


@patch("orchestration.pr_reviewer.subprocess.run")
def test_fetch_pr_diff_not_found(mock_run: MagicMock) -> None:
    """fetch_pr_diff should raise error when PR doesn't exist."""
    # Arrange
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = ""
    mock_result.stderr = "HTTP 404: Not Found"
    mock_run.return_value = mock_result

    # Act & Assert
    with pytest.raises(GHPullRequestError) as exc_info:
        fetch_pr_diff("jleechanorg", "nonexistent", 999)

    assert "404" in str(exc_info.value)


@patch("orchestration.pr_reviewer.subprocess.run")
def test_fetch_pr_diff_uses_github_api(mock_run: MagicMock) -> None:
    """fetch_pr_diff should use gh pr view --json diff."""
    # Arrange
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = MockPRDiff.small_diff()
    mock_result.stderr = ""
    mock_run.return_value = mock_result

    # Act
    fetch_pr_diff("jleechanorg", "claw", 42)

    # Assert - verify gh command structure
    call_args = mock_run.call_args[0][0]
    assert "gh" in call_args
    assert "pr" in call_args
    assert "diff" in call_args


# ---------------------------------------------------------------------------
# fetch_pr_commits tests
# ---------------------------------------------------------------------------


@patch("orchestration.pr_reviewer.subprocess.run")
def test_fetch_pr_commits_success(mock_run: MagicMock) -> None:
    """fetch_pr_commits should return parsed commit list."""
    # Arrange
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = MockPRCommits.commits_json()
    mock_result.stderr = ""
    mock_run.return_value = mock_result

    # Act
    commits = fetch_pr_commits("jleechanorg", "claw", 42)

    # Assert
    assert len(commits) == 2
    assert commits[0]["sha"] == "abc123"
    assert "feat: add auth middleware" in commits[0]["message"]


@patch("orchestration.pr_reviewer.subprocess.run")
def test_fetch_pr_commits_empty(mock_run: MagicMock) -> None:
    """fetch_pr_commits should return empty list for PR with no commits."""
    # Arrange
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "[]"
    mock_result.stderr = ""
    mock_run.return_value = mock_result

    # Act
    commits = fetch_pr_commits("jleechanorg", "claw", 42)

    # Assert
    assert commits == []


# ---------------------------------------------------------------------------
# fetch_ci_status tests
# ---------------------------------------------------------------------------


@patch("orchestration.pr_reviewer.subprocess.run")
def test_fetch_ci_status_success(mock_run: MagicMock) -> None:
    """fetch_ci_status should return CI status from gh API."""
    # Arrange
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = MockCIStatus.success_json()
    mock_result.stderr = ""
    mock_run.return_value = mock_result

    # Act
    status = fetch_ci_status("jleechanorg", "claw", 42)

    # Assert
    assert status["state"] == "success"
    assert len(status["statuses"]) == 2


@patch("orchestration.pr_reviewer.subprocess.run")
def test_fetch_ci_status_failure(mock_run: MagicMock) -> None:
    """fetch_ci_status should detect CI failure."""
    # Arrange
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = MockCIStatus.failure_json()
    mock_result.stderr = ""
    mock_run.return_value = mock_result

    # Act
    status = fetch_ci_status("jleechanorg", "claw", 42)

    # Assert
    assert status["state"] == "failure"


@patch("orchestration.pr_reviewer.subprocess.run")
def test_fetch_ci_status_pending(mock_run: MagicMock) -> None:
    """fetch_ci_status should handle pending status."""
    # Arrange
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = MockCIStatus.pending_json()
    mock_result.stderr = ""
    mock_run.return_value = mock_result

    # Act
    status = fetch_ci_status("jleechanorg", "claw", 42)

    # Assert
    assert status["state"] == "pending"


# ---------------------------------------------------------------------------
# load_claude_md_rules tests
# ---------------------------------------------------------------------------


@patch("orchestration.pr_reviewer.Path.exists")
@patch("builtins.open", new_callable=mock_open)
def test_load_claude_md_rules_repo_only(mock_file: MagicMock, mock_exists: MagicMock) -> None:
    """load_claude_md_rules should load repo-level CLAUDE.md when global doesn't exist."""
    # Arrange - repo CLAUDE.md exists, global doesn't
    # repo CLAUDE.md exists, global doesn't
    mock_exists.side_effect = [True, False]
    mock_file.return_value.read.return_value = "# Repo Rules\n\nFollow test-driven development."

    # Act
    rules = load_claude_md_rules("/tmp/fake-repo")

    # Assert
    assert "Repo Rules" in rules
    assert "Follow test-driven development" in rules


@patch("orchestration.pr_reviewer.Path.exists")
@patch("builtins.open", new_callable=mock_open)
def test_load_claude_md_rules_combined(mock_file: MagicMock, mock_exists: MagicMock) -> None:
    """load_claude_md_rules should combine repo and global rules."""
    # Arrange - both exist
    mock_exists.return_value = True

    call_count = 0
    def read_side_effect():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return "# Repo Rules\n\nRepo specific rules."
        return "# Global Rules\n\nGlobal specific rules."

    mock_file.return_value.read.side_effect = read_side_effect

    # Act
    rules = load_claude_md_rules("/tmp/fake-repo")

    # Assert
    assert "Repo Rules" in rules
    assert "Global Rules" in rules


@patch("orchestration.pr_reviewer.Path.exists")
def test_load_claude_md_rules_none_found(mock_exists: MagicMock) -> None:
    """load_claude_md_rules should handle missing files gracefully."""
    # Arrange - neither exists
    mock_exists.return_value = False

    # Act
    rules = load_claude_md_rules("/tmp/fake-repo")

    # Assert - should return empty string, not crash
    assert rules == ""


# ---------------------------------------------------------------------------
# load_openclaw_memory tests
# ---------------------------------------------------------------------------


@patch("orchestration.pr_reviewer.Path.exists")
@patch("builtins.open", new_callable=mock_open)
def test_load_openclaw_memory_project_memory(mock_file: MagicMock, mock_exists: MagicMock) -> None:
    """load_openclaw_memory should load project-type memories."""
    # Arrange
    # project memory exists, feedback doesn't
    mock_exists.side_effect = [True, False]
    mock_file.return_value.read.return_value = json.dumps([
        {"type": "project", "content": "This project uses pytest."},
        {"type": "project", "content": "Auth goes in src/auth/."},
    ])

    # Act
    memory = load_openclaw_memory("jleechanorg", "claw")

    # Assert
    assert "pytest" in memory
    assert "src/auth/" in memory


@patch("orchestration.pr_reviewer.Path.exists")
@patch("builtins.open", new_callable=mock_open)
def test_load_openclaw_memory_feedback_memory(mock_file: MagicMock, mock_exists: MagicMock) -> None:
    """load_openclaw_memory should load feedback-type memories."""
    # Arrange
    # project memory doesn't exist, feedback does
    mock_exists.side_effect = [False, True]
    mock_file.return_value.read.return_value = json.dumps([
        {"type": "feedback", "content": "Don't use print statements."},
    ])

    # Act
    memory = load_openclaw_memory("jleechanorg", "claw")

    # Assert
    assert "print" in memory.lower()


@patch("orchestration.pr_reviewer.Path.exists")
def test_load_openclaw_memory_no_memory(mock_exists: MagicMock) -> None:
    """load_openclaw_memory should handle missing memory gracefully."""
    # Arrange - no memory files exist
    mock_exists.return_value = False

    # Act
    memory = load_openclaw_memory("jleechanorg", "nonexistent")

    # Assert - should return empty string, not crash
    assert memory == ""


# ---------------------------------------------------------------------------
# load_prior_patterns tests
# ---------------------------------------------------------------------------


@patch("orchestration.pr_reviewer.Path.exists")
@patch("builtins.open", new_callable=mock_open)
def test_load_prior_patterns_from_action_log(mock_file: MagicMock, mock_exists: MagicMock) -> None:
    """load_prior_patterns should scan action_log.jsonl for past review decisions."""
    # Arrange
    mock_exists.return_value = True
    mock_file.return_value.read.return_value = json.dumps({
        "action_type": "review_approved",
        "repo": "jleechanorg/claw",
    }) + "\n" + json.dumps({
        "action_type": "review_escalated",
        "repo": "jleechanorg/claw",
    })

    # Act
    patterns = load_prior_patterns("jleechanorg", "claw")

    # Assert
    assert "approved" in patterns
    assert "escalated" in patterns


@patch("orchestration.pr_reviewer.Path.exists")
def test_load_prior_patterns_no_log(mock_exists: MagicMock) -> None:
    """load_prior_patterns should handle missing action log gracefully."""
    # Arrange
    mock_exists.return_value = False

    # Act
    patterns = load_prior_patterns("jleechanorg", "nonexistent")

    # Assert
    assert patterns == ""


# ---------------------------------------------------------------------------
# truncate_diff tests
# ---------------------------------------------------------------------------


def test_truncate_diff_small() -> None:
    """Small diffs should not be truncated."""
    diff = MockPRDiff.small_diff()
    result = truncate_diff(diff, max_lines=300)

    # Result should be unchanged (or minimally modified)
    assert "import pytest" in result
    assert "TRUNCATED" not in result


def test_truncate_diff_large() -> None:
    """Large diffs should be truncated with summary note."""
    diff = MockPRDiff.large_diff()
    result = truncate_diff(diff, max_lines=300)

    # Should contain truncation notice
    assert "TRUNCATED" in result
    assert "1000" in result
    assert "300" in result
    assert "NOTE:" in result


def test_truncate_diff_exact_boundary() -> None:
    """Diff exactly at boundary should not be truncated."""
    lines = ["+line " + str(i) for i in range(300)]
    diff = "\n".join(lines)
    result = truncate_diff(diff, max_lines=300)

    # Exactly at boundary - may or may not truncate depending on impl
    # But should not add NOTE if unchanged
    assert result.count("+line") == 300


# ---------------------------------------------------------------------------
# build_review_context integration tests
# ---------------------------------------------------------------------------


@patch("orchestration.pr_reviewer._find_repo_path")
@patch("orchestration.pr_reviewer.load_claude_md_rules")
@patch("orchestration.pr_reviewer.load_openclaw_memory")
@patch("orchestration.pr_reviewer.load_prior_patterns")
@patch("orchestration.pr_reviewer.fetch_pr_commits")
@patch("orchestration.pr_reviewer.fetch_ci_status")
@patch("orchestration.pr_reviewer.fetch_pr_diff")
def test_build_review_context_all_sources(
    mock_diff: MagicMock,
    mock_ci: MagicMock,
    mock_commits: MagicMock,
    mock_patterns: MagicMock,
    mock_memory: MagicMock,
    mock_claude: MagicMock,
    mock_find_repo: MagicMock,
) -> None:
    """build_review_context should assemble all sources into ReviewContext."""
    # Arrange
    mock_diff.return_value = MockPRDiff.small_diff()
    mock_commits.return_value = json.loads(MockPRCommits.commits_json())
    mock_ci.return_value = json.loads(MockCIStatus.success_json())
    mock_claude.return_value = "# Test Rules\n\nBe nice."
    mock_memory.return_value = "Project uses pytest."
    mock_patterns.return_value = "Previously approved similar PRs."
    mock_find_repo.return_value = "/tmp/test_repo"

    # Act
    context = build_review_context("jleechanorg", "claw", 42)

    # Assert
    assert isinstance(context, ReviewContext)
    assert "import pytest" in context.diff
    assert len(context.commits) == 2
    assert context.ci_status["state"] == "success"
    assert "Test Rules" in context.claude_md_rules
    assert "pytest" in context.memories
    assert "Previously approved" in context.prior_patterns


@patch("orchestration.pr_reviewer.load_claude_md_rules")
@patch("orchestration.pr_reviewer.load_openclaw_memory")
@patch("orchestration.pr_reviewer.load_prior_patterns")
@patch("orchestration.pr_reviewer.fetch_pr_commits")
@patch("orchestration.pr_reviewer.fetch_ci_status")
@patch("orchestration.pr_reviewer.fetch_pr_diff")
def test_build_review_context_partial_sources(
    mock_diff: MagicMock,
    mock_ci: MagicMock,
    mock_commits: MagicMock,
    mock_patterns: MagicMock,
    mock_memory: MagicMock,
    mock_claude: MagicMock,
) -> None:
    """build_review_context should work with partial sources (graceful degradation)."""
    # Arrange - simulate missing sources by returning empty
    mock_diff.return_value = MockPRDiff.small_diff()
    mock_commits.return_value = []
    mock_ci.return_value = {"state": "unknown", "statuses": []}
    mock_claude.return_value = ""
    mock_memory.return_value = ""
    mock_patterns.return_value = ""

    # Act
    context = build_review_context("jleechanorg", "nonexistent", 999)

    # Assert - should still return valid ReviewContext
    assert isinstance(context, ReviewContext)
    assert context.diff is not None
    assert context.commits == []
    # Empty sources should be empty strings
    assert context.claude_md_rules == ""
    assert context.memories == ""
    assert context.prior_patterns == ""


# ---------------------------------------------------------------------------
# ReviewContext dataclass tests
# ---------------------------------------------------------------------------


def test_review_context_dataclass_fields() -> None:
    """ReviewContext should have expected fields for LLM consumption."""
    context = ReviewContext(
        diff="+import pytest",
        commits=[{"sha": "abc", "message": "feat: test"}],
        ci_status={"state": "success"},
        claude_md_rules="# Rules",
        memories="Project uses pytest.",
        prior_patterns="Approved before.",
    )

    assert context.diff == "+import pytest"
    assert len(context.commits) == 1
    assert context.ci_status["state"] == "success"
    assert context.claude_md_rules == "# Rules"
    assert context.memories == "Project uses pytest."
    assert context.prior_patterns == "Approved before."


def test_review_context_to_prompt() -> None:
    """ReviewContext should serialize into prompt for LLM."""
    context = ReviewContext(
        diff="+import pytest",
        commits=[{"sha": "abc", "message": "feat: test"}],
        ci_status={"state": "success"},
        claude_md_rules="# Rules",
        memories="Project uses pytest.",
        prior_patterns="Approved before.",
    )

    # Verify all fields are included in string representation
    context_str = str(context)
    assert "+import pytest" in context_str
    assert "feat: test" in context_str
    assert "success" in context_str
    assert "# Rules" in context_str
    assert "pytest" in context_str
    assert "Approved before." in context_str
