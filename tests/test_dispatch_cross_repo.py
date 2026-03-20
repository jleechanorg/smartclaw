"""Tests for dispatch_task cross-repo and worktree functions."""
from __future__ import annotations

import pytest

from orchestration.dispatch_task import (
    _is_cross_repo_task,
    _CROSS_REPO_CONTEXT,
    _extract_repo_name_hint,
)


class TestIsCrossRepoTask:
    """Tests for _is_cross_repo_task function."""

    @pytest.mark.parametrize(
        "task,expected",
        [
            # Cross-repo indicators (matched by _extract_repo_name_hint)
            ("make a PR against mctrl_test", True),
            ("create a PR in mctrl_test repo", True),
            ("fix in worldarchitect repository", True),
            ("work in https://github.com/jleechanorg/mctrl_test", True),
            # Not matched — 'PR to' is not an _extract_repo_name_hint pattern
            ("fix comments worldai mcp PR to worldarchitect.ai", False),
            # Blocklist filters common non-repo words
            ("fix the bug in this repo", False),
            ("add tests for the new feature", False),
            ("", False),
        ],
    )
    def test_cross_repo_detection(self, task: str, expected: bool):
        """Test cross-repo task detection."""
        assert _is_cross_repo_task(task) is expected

    def test_cross_repo_context_not_empty(self):
        """Test that CROSS_REPO_CONTEXT is defined and non-empty."""
        assert _CROSS_REPO_CONTEXT
        assert "worktree" in _CROSS_REPO_CONTEXT.lower()
        assert "pr" in _CROSS_REPO_CONTEXT.lower()


class TestExtractRepoNameHint:
    """Tests for _extract_repo_name_hint function."""

    @pytest.mark.parametrize(
        "task,expected",
        [
            # Basic patterns
            ("fix in mctrl_test repo", "mctrl_test"),
            ("do something generic", ""),
            # GitHub URL extraction
            ("work in https://github.com/jleechanorg/mctrl_test", "mctrl_test"),
            ("check https://github.com/org/my-repo.git changes", "my-repo.git"),
            # Backtick-wrapped repo names
            ("fix in `mctrl_test` repo", "mctrl_test"),
            ("make PR against `worldarchitect`", "worldarchitect"),
            # Against patterns
            ("make a PR against mctrl_test", "mctrl_test"),
            ("deploy against staging-env", "staging-env"),
            # Repository keyword
            ("fix in worldarchitect repository", "worldarchitect"),
            # Blocklist — common words should NOT be extracted
            ("fix the bug in this repo", ""),
            ("add code in the repository", ""),
            ("work in a repo", ""),
            # Edge: trailing dots stripped
            ("fix in mctrl_test. repo", "mctrl_test"),
        ],
    )
    def test_repo_name_extraction(self, task: str, expected: str):
        """Test repo name hint extraction."""
        assert _extract_repo_name_hint(task) == expected

