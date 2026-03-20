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
import re
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

        # If comment page is incomplete, we cannot safely classify as bot-only.
        # Fail closed: treat truncated threads as non-bot so they are not silently skipped.
        if comment_total > len(comments):
            raise RuntimeError(
                f"Thread has {comment_total} comments but only {len(comments)} were fetched "
                f"— cannot safely determine if thread is bot-only"
            )

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


def get_files_changed_in_push(
    owner: str,
    repo: str,
    branch: str,
    before_sha: Optional[str] = None,
    after_sha: Optional[str] = None,
) -> list[str]:
    """Get list of files changed in the latest push to a branch.

    When push SHAs are available, uses the compare endpoint for accurate results.
    Falls back to the PR files endpoint when SHAs are not provided.

    Args:
        owner: Repository owner
        repo: Repository name
        branch: Branch name (PR head branch, used for fallback only)
        before_sha: SHA of the commit before the push (from webhook payload)
        after_sha: SHA of the commit after the push (from webhook payload)

    Returns:
        List of file paths that were changed
    """
    first_exc: Optional[Exception] = None

    # Primary: compare the exact push SHAs when available
    if before_sha and after_sha:
        try:
            raw = gh([
                "api",
                f"repos/{owner}/{repo}/compare/{before_sha}...{after_sha}",
                "--jq", ".files[].filename",
            ])
            if raw.strip():
                return [line.strip() for line in raw.strip().split("\n") if line.strip()]
        except Exception as exc:
            first_exc = exc

    # Fallback: single commit files when only after_sha is known
    if after_sha:
        try:
            raw = gh([
                "api",
                f"repos/{owner}/{repo}/commits/{after_sha}",
                "--jq", ".files[].filename",
            ])
            if raw.strip():
                return [line.strip() for line in raw.strip().split("\n") if line.strip()]
        except Exception as exc:
            if first_exc is None:
                first_exc = exc

    # Propagate errors so callers can observe auth/rate-limit/transient failures
    if first_exc is not None:
        raise first_exc

    # No SHAs provided or both returned empty — caller falls back to PR-level file list
    return []


def _parse_hunk_ranges(patch: str) -> list[tuple[int, int]]:
    """Parse a unified diff patch string and return (line, line) pairs for each added line.

    Only lines that begin with ``+`` (i.e. lines that were actually added or
    modified in the new file) are recorded.  Context lines (lines with no
    prefix or with a space prefix) are explicitly excluded so that a review
    thread sitting on an unchanged context line inside a large hunk is NOT
    wrongly auto-resolved.

    Args:
        patch: Unified diff patch string (from GitHub API ``patch`` field)

    Returns:
        List of (line_number, line_number) tuples — one per added line in the
        new file.  Each tuple represents a single touched line; callers that
        treat tuples as (start, end) ranges will correctly match only exact
        line positions.
    """
    ranges: list[tuple[int, int]] = []
    current_line = 0
    for raw_line in patch.splitlines():
        m = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", raw_line)
        if m:
            # Reset the new-file line counter to the hunk start (minus 1; the
            # first non-header line will increment it before use).
            current_line = int(m.group(1)) - 1
        elif raw_line.startswith("+") and not raw_line.startswith("+++"):
            # Actual added line — advance counter and record exact position.
            current_line += 1
            ranges.append((current_line, current_line))
        elif raw_line.startswith("-"):
            pass  # Removed line: does not occupy a line number in the new file.
        else:
            # Context line (space prefix or bare line): advance counter but do
            # NOT record — threads on context lines should not be auto-resolved.
            current_line += 1
    return ranges


def _line_in_hunks(line: Optional[int], hunks: list[tuple[int, int]]) -> bool:
    """Return True if *line* falls within any of the hunk ranges."""
    if line is None:
        return False
    return any(start <= line <= end for start, end in hunks)


def get_changed_file_hunks(
    owner: str,
    repo: str,
    before_sha: Optional[str],
    after_sha: Optional[str],
    pr_number: Optional[int],
    branch: Optional[str] = None,
) -> dict[str, list[tuple[int, int]]]:
    """Return a mapping of filename → list of (start, end) hunk ranges.

    Priority order:
    1. Push SHAs (before/after) — most precise
    2. PR files endpoint — when no SHAs
    3. Branch compare (branch...HEAD) — when only branch is provided
    Returns an empty dict if none of the above are available.
    """
    try:
        if before_sha and after_sha:
            raw = gh([
                "api",
                f"repos/{owner}/{repo}/compare/{before_sha}...{after_sha}",
                "--jq", "[.files[] | {filename, patch: (.patch // \"\")}]",
            ])
        elif pr_number is not None:
            raw = gh([
                "api",
                f"repos/{owner}/{repo}/pulls/{pr_number}/files?per_page=100",
                "--paginate",
                "--jq", "[.[] | {filename, patch: (.patch // \"\")}]",
            ])
        elif branch:
            # Compare branch tip against HEAD to surface hunk-level changes
            raw = gh([
                "api",
                f"repos/{owner}/{repo}/compare/{branch}...HEAD",
                "--jq", "[.files[] | {filename, patch: (.patch // \"\")}]",
            ])
        else:
            return {}
        files = json.loads(raw) if raw.strip() else []
    except Exception:
        return {}

    result: dict[str, list[tuple[int, int]]] = {}
    for f in files:
        patch = f.get("patch") or ""
        result[f["filename"]] = _parse_hunk_ranges(patch) if patch else []
    return result


def _get_pr_files_gh(owner: str, repo: str, pr_number: int) -> list[str]:
    """Get files changed in a PR using gh CLI (paginated)."""
    raw = gh([
        "api",
        f"repos/{owner}/{repo}/pulls/{pr_number}/files?per_page=100",
        "--paginate",
        "--jq", ".[].filename",
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
    before_sha: Optional[str] = None,
    after_sha: Optional[str] = None,
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

    # Step 2: Get hunk-level change map (filename → hunk ranges)
    file_hunks = get_changed_file_hunks(
        owner, repo,
        before_sha=before_sha,
        after_sha=after_sha,
        pr_number=pr_number,
        branch=branch,
    )

    # Step 3: Resolve threads whose line falls within a changed hunk
    for thread in threads:
        thread_path = thread.get("path")
        thread_id = thread["id"]
        thread_line = thread.get("line")

        # If thread has no file path (general comment), skip it
        if not thread_path:
            result["skipped"] += 1
            result["skipped_threads"].append(thread_id)
            continue

        # Skip if file not in the changed set at all
        if thread_path not in file_hunks:
            result["skipped"] += 1
            result["skipped_threads"].append(thread_id)
            continue

        hunks = file_hunks[thread_path]
        if not hunks:
            # Patch unavailable (e.g. binary file or large diff) — fail closed.
            # Do not auto-resolve threads in files without line-level hunk data.
            result["skipped"] += 1
            result["skipped_threads"].append(thread_id)
            continue
        in_hunk = _line_in_hunks(thread_line, hunks)

        if in_hunk:
            # Try to resolve the thread
            if resolve_review_thread(thread_id):
                result["resolved"] += 1
                result["resolved_threads"].append(thread_id)
            else:
                result["skipped"] += 1
                result["skipped_threads"].append(thread_id)
                result["errors"].append(f"Failed to resolve thread {thread_id}")
        else:
            # Thread line not in any changed hunk — skip
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
        "--before-sha",
        help="Commit SHA before the push (enables push-scoped diffing)"
    )
    parser.add_argument(
        "--after-sha",
        help="Commit SHA after the push (enables push-scoped diffing)"
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
        file_hunks = get_changed_file_hunks(
            args.owner, args.repo,
            before_sha=args.before_sha,
            after_sha=args.after_sha,
            pr_number=args.pr_number,
            branch=args.branch,
        )

        print(f"PR: {args.owner}/{args.repo}#{args.pr_number}")
        print(f"Changed files: {', '.join(file_hunks.keys()) or '(none)'}")
        print(f"\nUnresolved threads:")
        for t in threads:
            path = t.get("path")
            line = t.get("line")
            if path not in file_hunks:
                status = "unchanged"
            else:
                hunks = file_hunks[path]
                # Fail closed: skip if no patch data (same as live mode)
                status = "WOULD RESOLVE" if (hunks and _line_in_hunks(line, hunks)) else "unchanged"
            print(f"  - {path}:{line} ({t['author']}): {t['body'][:50]}... [{status}]")
    else:
        result = auto_resolve_threads_for_pr(
            args.owner, args.repo, args.pr_number, args.branch,
            before_sha=args.before_sha,
            after_sha=args.after_sha,
        )
        print(f"Resolved: {result['resolved']}")
        print(f"Skipped: {result['skipped']}")
        if result["errors"]:
            print(f"Errors: {result['errors']}")


if __name__ == "__main__":
    main()
