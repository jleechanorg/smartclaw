"""Tests for orchestration.auto_resolve_threads — auto-resolve review threads after fix push."""

import json
import subprocess
from unittest.mock import patch, MagicMock

import pytest

from orchestration.auto_resolve_threads import (
    get_review_threads,
    get_files_changed_in_push,
    resolve_review_thread,
    auto_resolve_threads_for_pr,
    _get_pr_files_gh,
)


# ---------------------------------------------------------------------------
# Helper: mock gh CLI output
# ---------------------------------------------------------------------------

def mock_gh_result(stdout: str):
    """Return just the stdout string (gh() returns .strip() of stdout)."""
    return stdout


# ---------------------------------------------------------------------------
# Tests for get_review_threads
# ---------------------------------------------------------------------------


def test_get_review_threads_returns_unresolved_threads():
    """Should return unresolved, non-outdated review threads with location info."""
    mock_response = {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "totalCount": 2,
                        "nodes": [
                            {
                                "isResolved": False,
                                "comments": {
                                    "totalCount": 1,
                                    "nodes": [
                                        {
                                            "id": "thread-1-id",
                                            "author": {"login": "human-user"},
                                            "body": "Fix this bug",
                                            "path": "src/main.py",
                                            "line": 42,
                                            "url": "https://github.com/org/repo/pull/1#discussion_thread_1",
                                            "createdAt": "2024-01-15T10:00:00Z"
                                        }
                                    ]
                                }
                            },
                            {
                                "isResolved": True,  # Already resolved - should be filtered
                                "comments": {
                                    "totalCount": 1,
                                    "nodes": [
                                        {
                                            "id": "thread-2-id",
                                            "author": {"login": "human-user"},
                                            "body": "LGTM",
                                            "path": "src/main.py",
                                            "line": 50,
                                            "url": "https://github.com/org/repo/pull/1#discussion_thread_2",
                                            "createdAt": "2024-01-15T11:00:00Z"
                                        }
                                    ]
                                }
                            }
                        ]
                    }
                }
            }
        }
    }

    with patch("orchestration.auto_resolve_threads.gh") as mock_gh:
        mock_gh.return_value = json.dumps(mock_response)
        threads = get_review_threads("owner", "repo", 1)

    assert len(threads) == 1
    assert threads[0]["id"] == "thread-1-id"
    assert threads[0]["path"] == "src/main.py"
    assert threads[0]["line"] == 42


def test_get_review_threads_filters_bot_threads():
    """Should filter out threads where all comments are from bots."""
    mock_response = {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "totalCount": 1,
                        "nodes": [
                            {
                                "isResolved": False,
                                "comments": {
                                    "totalCount": 1,
                                    "nodes": [
                                        {
                                            "id": "bot-thread-id",
                                            "author": {"login": "codecov[bot]"},
                                            "body": "Coverage unchanged",
                                            "path": "src/main.py",
                                            "line": 42,
                                            "url": "https://github.com/org/repo/pull/1#discussion_bot",
                                            "createdAt": "2024-01-15T10:00:00Z"
                                        }
                                    ]
                                }
                            }
                        ]
                    }
                }
            }
        }
    }

    with patch("orchestration.auto_resolve_threads.gh") as mock_gh:
        mock_gh.return_value = json.dumps(mock_response)
        threads = get_review_threads("owner", "repo", 1)

    # Bot-only threads should be filtered out
    assert len(threads) == 0


# ---------------------------------------------------------------------------
# Tests for get_files_changed_in_push
# ---------------------------------------------------------------------------


def test_get_files_changed_in_push():
    """Should return list of files changed in the latest push (via _get_pr_files_gh)."""
    # The main function uses _get_pr_files_gh which calls the files API
    from orchestration.auto_resolve_threads import _get_pr_files_gh
    
    with patch("orchestration.auto_resolve_threads.gh") as mock_gh:
        # Raw output from --jq .[].filename
        mock_gh.return_value = "src/main.py\nsrc/utils.py\nREADME.md"
        files = _get_pr_files_gh("owner", "repo", 1)

    assert len(files) == 3
    assert "src/main.py" in files
    assert "src/utils.py" in files
    assert "README.md" in files


def test_get_files_changed_in_push_empty():
    """Should return empty list when no files changed."""
    from orchestration.auto_resolve_threads import _get_pr_files_gh
    
    with patch("orchestration.auto_resolve_threads.gh") as mock_gh:
        mock_gh.return_value = ""
        files = _get_pr_files_gh("owner", "repo", 1)

    assert len(files) == 0


# ---------------------------------------------------------------------------
# Tests for resolve_review_thread
# ---------------------------------------------------------------------------


def test_resolve_review_thread_success():
    """Should resolve a review thread via GraphQL mutation."""
    mock_response = {
        "data": {
            "resolveReviewThread": {
                "thread": {
                    "isResolved": True
                }
            }
        }
    }

    with patch("orchestration.auto_resolve_threads.gh") as mock_gh:
        mock_gh.return_value = json.dumps(mock_response)
        result = resolve_review_thread("thread-id-123")

    assert result is True
    mock_gh.assert_called_once()
    call_args = mock_gh.call_args[0][0]
    # Check that mutation is in the query
    assert "mutation" in call_args[call_args.index("-f") + 1]
    # Check that the thread ID is passed as a variable (-f id=...)
    assert "-f" in call_args
    # The -f id=thread-id-123 is in the args list
    assert any("thread-id-123" in str(arg) for arg in call_args)


def test_resolve_review_thread_failure():
    """Should return False when resolution fails."""
    with patch("orchestration.auto_resolve_threads.gh") as mock_gh:
        mock_gh.side_effect = RuntimeError("GraphQL error")
        result = resolve_review_thread("thread-id-123")

    assert result is False


# ---------------------------------------------------------------------------
# Tests for auto_resolve_threads_for_pr
# ---------------------------------------------------------------------------


def test_auto_resolve_threads_matches_modified_files():
    """Should resolve threads whose file locations were modified in push."""
    # Setup: 2 unresolved threads, one on modified file, one on unchanged file
    threads_response = {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "totalCount": 2,
                        "nodes": [
                            {
                                "isResolved": False,
                                "comments": {
                                    "totalCount": 1,
                                    "nodes": [
                                        {
                                            "id": "thread-on-modified",
                                            "author": {"login": "reviewer"},
                                            "body": "Fix this",
                                            "path": "src/main.py",
                                            "line": 42,
                                            "url": "https://github.com/org/repo/pull/1#r1",
                                            "createdAt": "2024-01-15T10:00:00Z"
                                        }
                                    ]
                                }
                            },
                            {
                                "isResolved": False,
                                "comments": {
                                    "totalCount": 1,
                                    "nodes": [
                                        {
                                            "id": "thread-on-unchanged",
                                            "author": {"login": "reviewer"},
                                            "body": "Fix this too",
                                            "path": "src/other.py",
                                            "line": 10,
                                            "url": "https://github.com/org/repo/pull/1#r2",
                                            "createdAt": "2024-01-15T11:00:00Z"
                                        }
                                    ]
                                }
                            }
                        ]
                    }
                }
            }
        }
    }

    # Files returned as raw output (one per line) since we use --jq .[].filename
    files_response = "src/main.py"

    resolve_response = {
        "data": {
            "resolveReviewThread": {
                "thread": {"isResolved": True}
            }
        }
    }

    with patch("orchestration.auto_resolve_threads.gh") as mock_gh:
        # Return threads, then files, then resolve mutation
        mock_gh.side_effect = [
            mock_gh_result(json.dumps(threads_response)),
            mock_gh_result(files_response),
            mock_gh_result(json.dumps(resolve_response)),
        ]
        result = auto_resolve_threads_for_pr("owner", "repo", 1)

    # Only the thread on modified file should be resolved
    assert result["resolved"] == 1
    assert result["skipped"] == 1
    assert "thread-on-modified" in result["resolved_threads"]
    assert "thread-on-unchanged" in result["skipped_threads"]


def test_auto_resolve_threads_handles_no_threads():
    """Should handle PR with no unresolved threads gracefully."""
    threads_response = {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "totalCount": 0,
                        "nodes": []
                    }
                }
            }
        }
    }

    with patch("orchestration.auto_resolve_threads.gh") as mock_gh:
        mock_gh.return_value = json.dumps(threads_response)
        result = auto_resolve_threads_for_pr("owner", "repo", 1)

    assert result["resolved"] == 0
    assert result["skipped"] == 0


def test_auto_resolve_threads_handles_empty_push():
    """Should skip all threads when push has no file changes."""
    threads_response = {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "totalCount": 1,
                        "nodes": [
                            {
                                "isResolved": False,
                                "comments": {
                                    "totalCount": 1,
                                    "nodes": [
                                        {
                                            "id": "thread-1",
                                            "author": {"login": "reviewer"},
                                            "body": "Comment",
                                            "path": "src/main.py",
                                            "line": 42,
                                            "url": "https://github.com/org/repo/pull/1#r1",
                                            "createdAt": "2024-01-15T10:00:00Z"
                                        }
                                    ]
                                }
                            }
                        ]
                    }
                }
            }
        }
    }

    with patch("orchestration.auto_resolve_threads.gh") as mock_gh:
        mock_gh.side_effect = [
            mock_gh_result(json.dumps(threads_response)),
            mock_gh_result(""),  # No files changed - empty output from --jq
        ]
        result = auto_resolve_threads_for_pr("owner", "repo", 1)

    # No threads resolved since no files changed
    assert result["resolved"] == 0
    assert result["skipped"] == 1
