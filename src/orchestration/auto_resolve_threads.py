"""Auto-resolve review threads after fix push.

This module provides functionality to automatically resolve GitHub review threads
when their location was modified in the latest push. This helps unblock PRs with
mergeStateStatus=UNSTABLE by resolving outdated review comments.

Usage:
    from orchestration.auto_resolve_threads import auto_resolve_threads_for_pr
    
    result = auto_resolve_threads_for_pr("owner", "repo", 123)
    print(f"Resolved {result['resolved']} threads")
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Optional


# Re-export from gh_integration for convenience
from orchestration.gh_integration import gh, BOT_AUTHORS  # noqa: F401


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ReviewThread:
    """Represents an unresolved review thread on a PR."""
    id: str
    author: str
    body: str
    path: Optional[str]
    line: Optional[int]
    url: str
    is_resolved: bool
    created_at: str


# ---------------------------------------------------------------------------
# Get review threads
# ---------------------------------------------------------------------------


def get_review_threads(owner: str, repo: str, pr_number: int) -> list[dict]:
    """Get all unresolved, non-bot review threads for a PR.

    Uses GraphQL to fetch review threads, filtering out:
    - Already resolved threads
    - Threads where all comments are from bots

    Args:
        owner: Repository owner (e.g., "jleechanorg")
        repo: Repository name (e.g., "smartclaw")
        pr_number: PR number

    Returns:
        List of thread dictionaries with id, author, body, path, line, url, is_resolved, created_at
    """
    raw = gh([
        "api", "graphql",
        "-f", (
            "query=query($owner: String!, $name: String!, $number: Int!) {"
            "  repository(owner: $owner, name: $name) {"
            "    pullRequest(number: $number) {"
            "      reviewThreads(first: 100) {"
            "        totalCount"
            "        nodes {"
            "          id"
            "          isResolved"
            "          comments(first: 50) {"
            "            totalCount"
            "            nodes {"
            "              id"
            "              author { login }"
            "              body"
            "              path"
            "              line"
            "              url"
            "              createdAt"
            "            }"
            "          }"
            "        }"
            "      }"
            "    }"
            "  }"
            "}"
        ),
        "-f", f"owner={owner}",
        "-f", f"name={repo}",
        "-F", f"number={pr_number}",
    ])

    try:
        data = json.loads(raw)
        thread_data = data["data"]["repository"]["pullRequest"]["reviewThreads"]
        threads = thread_data["nodes"]
    except Exception as exc:
        raise RuntimeError("Unable to load reviewThreads from GitHub GraphQL") from exc

    # Validate completeness
    if "totalCount" not in thread_data:
        raise RuntimeError(
            "GraphQL response missing totalCount for reviewThreads — "
            "cannot verify all threads were fetched"
        )
    total_count = thread_data["totalCount"]
    if total_count > len(threads):
        raise RuntimeError(
            f"PR has {total_count} review threads but only {len(threads)} "
            f"were fetched — cannot guarantee all unresolved threads are visible"
        )

    # Filter to unresolved non-bot threads
    result = []
    for t in threads:
        if t["isResolved"]:
            continue

        comment_data = t["comments"]
        comments = comment_data["nodes"]
        comment_total = comment_data.get("totalCount", len(comments))

        if not comments:
            continue

        # Find first non-bot comment in thread
        c = None
        for candidate in comments:
            author = (candidate.get("author") or {}).get("login", "unknown")
            if author not in BOT_AUTHORS:
                c = candidate
                break

        if c is None:
            # All comments are from bots - skip this thread
            continue

        author = (c.get("author") or {}).get("login", "unknown")
        result.append({
            "id": t["id"],
            "author": author,
            "body": c["body"],
            "path": c.get("path") or None,
            "line": c.get("line"),
            "is_resolved": t["isResolved"],
            "created_at": c.get("createdAt"),
            "url": c.get("url"),
        })

    return result


# ---------------------------------------------------------------------------
# Get files changed in push
# ---------------------------------------------------------------------------


def get_files_changed_in_push(owner: str, repo: str, branch: str) -> list[str]:
    """Get list of files changed in the latest push to a branch.

    Uses `gh pr diff` to get the files that were modified in the most recent push.

    Args:
        owner: Repository owner
        repo: Repository name
        branch: Branch name (PR head branch)

    Returns:
        List of file paths that were changed
    """
    # Get the comparison between the branch and its base
    # For a PR, we can use the PR number or compare with the base branch
    try:
        # Try using the compare API to get files changed in the latest push
        raw = gh([
            "api", "repos", f"{owner}/{repo}",
            "--jq", ".compare"
        ])
    except Exception:
        # Fallback: use pr diff which shows all changes in the PR
        raw = gh([
            "pr", "diff",
            "--repo", f"{owner}/{repo}",
            "--name-only",
        ])
        # pr diff returns file names directly, one per line
        return [line.strip() for line in raw.strip().split("\n") if line.strip()]

    # Use the commits API to get the latest commit
    try:
        raw = gh([
            "api",
            f"repos/{owner}/{repo}/commits/{branch}",
            "--jq", ".[].files[].filename"
        ])
    except Exception:
        pass

    # Fallback: get files from the PR itself
    try:
        raw = gh([
            "api",
            "graphql",
            "-f", (
                "query=query($owner: String!, $name: String!, $branch: String!) {"
                "  repository(owner: $owner, name: $name) {"
                "    ref(qualifiedName: $branch) {"
                "      target {"
                "        ... on Commit {"
                "          changedFiles(first: 100) {"
                "            nodes {"
                "              path"
                "            }"
                "          }"
                "        }"
                "      }"
                "    }"
                "  }"
                "}"
            ),
            "-f", f"owner={owner}",
            "-f", f"name={repo}",
            "-f", f"branch=refs/heads/{branch}",
        ])
        data = json.loads(raw)
        files = data.get("data", {}).get("repository", {}).get("ref", {}).get("target", {})
        if files:
            changed_files = files.get("changedFiles", {}).get("nodes", [])
            return [f["path"] for f in changed_files]
    except Exception:
        pass

    # Final fallback: empty list
    return []


def _get_pr_files_gh(owner: str, repo: str, pr_number: int) -> list[str]:
    """Get files changed in a PR using gh CLI."""
    raw = gh([
        "api",
        f"repos/{owner}/{repo}/pulls/{pr_number}/files",
        "--jq", ".[].filename"
    ])
    if not raw.strip():
        return []
    return [line.strip() for line in raw.strip().split("\n") if line.strip()]


# ---------------------------------------------------------------------------
# Resolve review thread
# ---------------------------------------------------------------------------


def resolve_review_thread(thread_id: str) -> bool:
    """Resolve a review thread via GraphQL mutation.

    Args:
        thread_id: The GitHub node ID of the thread comment

    Returns:
        True if resolution succeeded, False otherwise
    """
    try:
        gh([
            "api", "graphql",
            "-f", (
                "query=mutation($id: ID!) {"
                "  resolveReviewThread(input: {threadId: $id}) {"
                "    thread {"
                "      isResolved"
                "    }"
                "  }"
                "}"
            ),
            "-f", f"id={thread_id}",
        ])
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Main auto-resolve function
# ---------------------------------------------------------------------------


def auto_resolve_threads_for_pr(
    owner: str,
    repo: str,
    pr_number: int,
    branch: Optional[str] = None,
) -> dict:
    """Auto-resolve review threads whose locations were modified in the latest push.

    This is the main entry point. It:
    1. Gets all unresolved, non-bot review threads for the PR
    2. Gets the list of files changed in the PR (via the branch or PR number)
    3. For each thread, checks if its file was modified
    4. Resolves threads where the file was changed

    Args:
        owner: Repository owner
        repo: Repository name
        pr_number: PR number
        branch: Branch name (optional, will be inferred from PR if not provided)

    Returns:
        Dictionary with:
        - resolved: Number of threads resolved
        - skipped: Number of threads skipped (unchanged files)
        - resolved_threads: List of resolved thread IDs
        - skipped_threads: List of skipped thread IDs
        - errors: List of error messages (if any)
    """
    result = {
        "resolved": 0,
        "skipped": 0,
        "resolved_threads": [],
        "skipped_threads": [],
        "errors": [],
    }

    # Step 1: Get unresolved threads
    try:
        threads = get_review_threads(owner, repo, pr_number)
    except Exception as e:
        result["errors"].append(f"Failed to get review threads: {e}")
        return result

    if not threads:
        return result

    # Step 2: Get files changed in the PR
    try:
        if branch:
            # Get files from branch comparison (for latest push only)
            files = get_files_changed_in_push(owner, repo, branch)
        else:
            files = _get_pr_files_gh(owner, repo, pr_number)
    except Exception as e:
        result["errors"].append(f"Failed to get changed files: {e}")
        # Continue with empty file list - will skip all threads
        files = []

    # Convert to set for O(1) lookup
    changed_files = set(files)

    # Step 3: Resolve threads whose file was modified
    for thread in threads:
        thread_path = thread.get("path")
        thread_id = thread["id"]

        # If thread has no file path (general comment), skip it
        if not thread_path:
            result["skipped"] += 1
            result["skipped_threads"].append(thread_id)
            continue

        # Check if the file was modified in the push
        if thread_path in changed_files:
            # Try to resolve the thread
            if resolve_review_thread(thread_id):
                result["resolved"] += 1
                result["resolved_threads"].append(thread_id)
            else:
                result["skipped"] += 1
                result["skipped_threads"].append(thread_id)
                result["errors"].append(f"Failed to resolve thread {thread_id}")
        else:
            # File was not modified - skip this thread
            result["skipped"] += 1
            result["skipped_threads"].append(thread_id)

    return result


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main():
    """CLI entry point for the script."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Auto-resolve GitHub review threads after fix push"
    )
    parser.add_argument("owner", help="Repository owner")
    parser.add_argument("repo", help="Repository name")
    parser.add_argument("pr_number", type=int, help="PR number")
    parser.add_argument(
        "--branch", "-b",
        help="Branch name (optional, will be inferred from PR if not provided)"
    )
    parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="Show what would be resolved without actually resolving"
    )

    args = parser.parse_args()

    if args.dry_run:
        # Just show threads without resolving
        threads = get_review_threads(args.owner, args.repo, args.pr_number)
        files = _get_pr_files_gh(args.owner, args.repo, args.pr_number)
        changed_files = set(files)

        print(f"PR: {args.owner}/{args.repo}#{args.pr_number}")
        print(f"Changed files: {', '.join(files) or '(none)'}")
        print(f"\nUnresolved threads:")
        for t in threads:
            status = "WOULD RESOLVE" if t.get("path") in changed_files else "unchanged"
            print(f"  - {t['path']}:{t.get('line')} ({t['author']}): {t['body'][:50]}... [{status}]")
    else:
        result = auto_resolve_threads_for_pr(
            args.owner, args.repo, args.pr_number, args.branch
        )
        print(f"Resolved: {result['resolved']}")
        print(f"Skipped: {result['skipped']}")
        if result["errors"]:
            print(f"Errors: {result['errors']}")


if __name__ == "__main__":
    main()
