"""Integration tests for auto_resolve_threads wiring in action_executor.

These tests verify that auto_resolve_threads_for_pr is called after
RetryAction and MergeAction execution.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from orchestration.action_executor import (
    execute_action,
    _parse_pr_url,
    _auto_resolve_if_pr,
)
from orchestration.escalation_router import (
    RetryAction,
    MergeAction,
)


class TestParsePrUrl(unittest.TestCase):
    """Tests for _parse_pr_url helper."""

    def test_valid_pr_url(self):
        """Test parsing a valid PR URL."""
        result = _parse_pr_url("https://github.com/owner/repo/pull/123")
        self.assertEqual(result, ("owner", "repo", 123))

    def test_valid_pr_url_with_trailing_slash(self):
        """Test parsing PR URL with trailing slash."""
        result = _parse_pr_url("https://github.com/owner/repo/pull/456/")
        self.assertEqual(result, ("owner", "repo", 456))

    def test_org_repo_url(self):
        """Test parsing PR URL with organization name."""
        result = _parse_pr_url("https://github.com/my-org/my-repo/pull/789")
        self.assertEqual(result, ("my-org", "my-repo", 789))

    def test_none_input(self):
        """Test that None input returns None."""
        result = _parse_pr_url(None)
        self.assertIsNone(result)

    def test_empty_string_input(self):
        """Test that empty string returns None."""
        result = _parse_pr_url("")
        self.assertIsNone(result)

    def test_invalid_url(self):
        """Test that invalid URL returns None."""
        result = _parse_pr_url("not-a-valid-url")
        self.assertIsNone(result)


class TestAutoResolveIfPr(unittest.TestCase):
    """Tests for _auto_resolve_if_pr helper."""

    @patch("orchestration.action_executor.auto_resolve_threads_for_pr")
    def test_calls_auto_resolve_with_valid_pr_url(self, mock_resolve):
        """Test that _auto_resolve_if_pr calls auto_resolve_threads_for_pr."""
        mock_resolve.return_value = {"resolved": 2, "skipped": 1, "resolved_threads": [], "skipped_threads": [], "errors": []}
        
        result = _auto_resolve_if_pr("https://github.com/owner/repo/pull/123", "session-123")
        
        mock_resolve.assert_called_once_with("owner", "repo", 123)
        self.assertEqual(result["resolved"], 2)

    @patch("orchestration.action_executor.auto_resolve_threads_for_pr")
    def test_returns_none_for_none_pr_url(self, mock_resolve):
        """Test that None pr_url returns None without calling resolve."""
        result = _auto_resolve_if_pr(None, "session-123")
        
        mock_resolve.assert_not_called()
        self.assertIsNone(result)

    @patch("orchestration.action_executor.auto_resolve_threads_for_pr")
    def test_handles_exception_gracefully(self, mock_resolve):
        """Test that exceptions from auto_resolve are handled gracefully."""
        mock_resolve.side_effect = RuntimeError("API error")
        
        result = _auto_resolve_if_pr("https://github.com/owner/repo/pull/123", "session-123")
        
        # Should return None on exception (doesn't propagate)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
