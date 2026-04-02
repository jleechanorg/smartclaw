#!/usr/bin/env python3
"""
AI Reviewer Stress Test - Main Runner

Runs every 4h to:
1. Select ~5000 lines of code from the repo
2. Copy to test repo, create PR
3. Wait for AI reviewers (CR, Cursor, Copilot, Codex)
4. Use agento to fix comments until green
5. Create real PR in original repo for human review
6. Close the test PR
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Add script dir to path
sys.path.insert(0, str(Path(__file__).parent))

from code_selector import CodeSelector
from test_repo_manager import TestRepoManager
from review_collector import ReviewCollector, ReviewComment
from outcome_ledger import OutcomeLedger, StressTestResult


# Config
ORIGINAL_REPO = "jleechanorg/smartclaw"
ORIGINAL_REPO_PATH = Path.home() / "projects_reference" / "smartclaw"
TEST_REPO_OWNER = "jleechanorg"
TEST_REPO_NAME = "smartclaw-review-test"
TARGET_LINES = 5000
MAX_AGENTO_RETRIES = 3
REVIEW_WAIT_MIN = 30
RE_REVIEW_WAIT_MIN = 15


def _find_fix_tool() -> str:
    """Find available agent CLI: ao only."""
    if shutil.which("ao"):
        return "ao"
    raise RuntimeError("ao CLI not found — ao spawn is required")


def _run_agent_fix(
    tool: str,
    comment_summary: str,
    repo_path: str,
    branch: str,
) -> bool:
    """Invoke ao spawn to fix AI reviewer comments. Returns True on success."""
    task = (
        f"Fix these AI reviewer comments on the code in this repo. "
        f"Make minimal, targeted changes to address each comment.\n\n{comment_summary}"
    )

    cmd = [
        "ao", "spawn",
        "--agent", "minimax",
        "--task", task,
    ]

    print(f"  Running ao spawn (minimax) to fix comments...")
    try:
        result = subprocess.run(
            cmd,
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=600,  # 10 min max per attempt
        )
    except subprocess.TimeoutExpired:
        print(f"  {tool} timed out after 10 minutes")
        return False

    if result.returncode != 0:
        print(f"  {tool} exited with code {result.returncode}")
        if result.stderr:
            print(f"  stderr: {result.stderr[:500]}")
        return False

    print(f"  {tool} completed successfully")
    return True


def _commit_and_push_fixes(repo_path: str, attempt: int) -> bool:
    """Stage, commit, and push any fixes the agent made."""
    # Check if there are changes
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo_path, capture_output=True, text=True
    )
    if not status.stdout.strip():
        print("  No changes made by agent")
        return False

    try:
        subprocess.run(["git", "add", "-A"], cwd=repo_path, check=True)
        subprocess.run(
            ["git", "commit", "-m", f"fix: address AI reviewer comments (attempt {attempt})"],
            cwd=repo_path, capture_output=True, text=True, check=True
        )
        subprocess.run(
            ["git", "push"],
            cwd=repo_path, capture_output=True, text=True, check=True
        )
    except subprocess.CalledProcessError as e:
        print(f"  Git operation failed (attempt {attempt}): {e}")
        return False
    except OSError as e:
        print(f"  Git not found or unavailable: {e}")
        return False

    print(f"  Pushed fixes (attempt {attempt})")
    return True


def _get_fix_diff(repo_path: str, start_sha: str) -> str:
    """Get the cumulative diff of all fixes since start_sha."""
    result = subprocess.run(
        ["git", "diff", start_sha, "HEAD"],
        cwd=repo_path, capture_output=True, text=True
    )
    return result.stdout


def _apply_fixes_to_original(
    diff: str,
    slice_name: str,
    original_repo_path: str,
) -> int | None:
    """Apply the fix diff to the original repo and create a PR. Returns PR number."""
    branch = f"ai-review-fixes-{slice_name[:30]}-{datetime.now().strftime('%Y%m%d')}"

    # Create branch in original repo
    subprocess.run(
        ["git", "fetch", "origin"],
        cwd=original_repo_path, capture_output=True, text=True
    )
    subprocess.run(
        ["git", "checkout", "-b", branch, "origin/main"],
        cwd=original_repo_path, capture_output=True, text=True
    )

    # Apply the diff
    apply_result = subprocess.run(
        ["git", "apply", "--allow-empty", "-"],
        input=diff,
        cwd=original_repo_path,
        capture_output=True, text=True
    )

    if apply_result.returncode != 0:
        print(f"  Failed to apply diff: {apply_result.stderr[:300]}")
        # Clean up branch
        subprocess.run(
            ["git", "checkout", "main"],
            cwd=original_repo_path, capture_output=True, text=True
        )
        subprocess.run(
            ["git", "branch", "-D", branch],
            cwd=original_repo_path, capture_output=True, text=True
        )
        return None

    # Check if there are actual changes
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=original_repo_path, capture_output=True, text=True
    )
    if not status.stdout.strip():
        print("  No applicable changes for original repo")
        subprocess.run(
            ["git", "checkout", "main"],
            cwd=original_repo_path, capture_output=True, text=True
        )
        subprocess.run(
            ["git", "branch", "-D", branch],
            cwd=original_repo_path, capture_output=True, text=True
        )
        return None

    # Commit
    subprocess.run(["git", "add", "-A"], cwd=original_repo_path, check=True)
    subprocess.run(
        ["git", "commit", "-m",
         f"fix: AI reviewer stress test findings - {slice_name}"],
        cwd=original_repo_path, capture_output=True, text=True, check=True
    )
    subprocess.run(
        ["git", "push", "-u", "origin", branch],
        cwd=original_repo_path, capture_output=True, text=True, check=True
    )

    # Create PR via REST API (avoids GraphQL rate limits)
    pr_body = (
        f"## AI Reviewer Stress Test Findings\n\n"
        f"Automated fixes discovered by AI reviewers for `{slice_name}`.\n\n"
        f"**Source:** stress test PR in `{TEST_REPO_OWNER}/{TEST_REPO_NAME}`\n"
        f"**Reviewers involved:** CodeRabbit, Cursor Bugbot, Copilot, Codex\n\n"
        f"Please review these changes carefully before merging."
    )

    pr_result = subprocess.run(
        ["gh", "api", f"repos/{ORIGINAL_REPO}/pulls",
         "--method", "POST",
         "-f", f"title=fix: AI reviewer stress test findings - {slice_name}",
         "-f", f"head={branch}",
         "-f", "base=main",
         "-f", f"body={pr_body}"],
        capture_output=True, text=True
    )

    if pr_result.returncode != 0:
        print(f"  Failed to create PR: {pr_result.stderr[:300]}")
        return None

    try:
        pr_data = json.loads(pr_result.stdout)
        pr_number = pr_data.get("number", 0)
        print(f"  Created PR #{pr_number} in {ORIGINAL_REPO}")
        return pr_number
    except json.JSONDecodeError:
        print(f"  Failed to parse PR response")
        return None
    finally:
        # Return to main branch
        subprocess.run(
            ["git", "checkout", "main"],
            cwd=original_repo_path, capture_output=True, text=True
        )


def run_agento_fix_loop(
    collector: ReviewCollector,
    comments: list[ReviewComment],
    test_repo_path: str,
    branch: str,
) -> tuple[bool, int]:
    """Run the fix loop: agent fix -> push -> re-review -> check.

    Returns (success, attempts).
    """
    try:
        tool = _find_fix_tool()
    except RuntimeError as e:
        print(f"  ERROR: {e}")
        return False, 0

    # Track when we last pushed a fix to filter out old comments
    last_push_time = None

    for attempt in range(1, MAX_AGENTO_RETRIES + 1):
        print(f"\n  === Fix attempt {attempt}/{MAX_AGENTO_RETRIES} ===")

        comment_summary = collector.format_comments_for_agent(comments)

        success = _run_agent_fix(tool, comment_summary, test_repo_path, branch)
        if not success:
            print(f"  Agent fix failed on attempt {attempt}")
            continue

        pushed = _commit_and_push_fixes(test_repo_path, attempt)
        if not pushed:
            print(f"  No changes to push on attempt {attempt} — agent may have made no changes")
            continue

        # Record push time to filter out old comments
        last_push_time = datetime.now(timezone.utc)

        # Wait for re-review
        print(f"  Waiting {RE_REVIEW_WAIT_MIN}min for re-review...")
        time.sleep(RE_REVIEW_WAIT_MIN * 60)

        # Check for remaining comments (only those created after our fix)
        new_comments = collector.check_unresolved_comments(created_after=last_push_time)
        actionable = [c for c in new_comments if c.severity in ("Critical", "Major")]

        if not actionable:
            print(f"  All critical/major comments resolved after attempt {attempt}")
            return True, attempt

        print(f"  {len(actionable)} critical/major comments remain")
        comments = new_comments  # Use fresh comments for next iteration

    print(f"  Exhausted {MAX_AGENTO_RETRIES} fix attempts")
    return False, MAX_AGENTO_RETRIES


def run_stress_test() -> StressTestResult:
    """Run one iteration of the stress test."""
    run_id = f"run-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    start_time = time.time()

    result = StressTestResult(
        run_id=run_id,
        timestamp=datetime.now().isoformat(),
        slice="",
        lines_reviewed=0,
        test_pr=0,
        original_pr=None,
        ai_reviewers_responded=[],
        total_comments=0,
        comments_by_severity={},
        agento_attempts=0,
        fix_success=False,
        time_to_green_minutes=0,
        fixes_applied_to_original=False
    )

    try:
        # 1. Select code slice
        print(f"[{run_id}] Step 1: Selecting code slice...")
        selector = CodeSelector(str(ORIGINAL_REPO_PATH), TARGET_LINES)
        slice_ = selector.select_next_slice()

        result.slice = slice_.path
        result.lines_reviewed = slice_.line_count

        print(f"[{run_id}] Selected: {slice_.path} ({slice_.line_count} lines)")

        # 2. Copy to test repo
        print(f"[{run_id}] Step 2: Setting up test repo...")
        manager = TestRepoManager(str(ORIGINAL_REPO_PATH))
        manager.sync_from_origin()

        branch = manager.create_review_branch(slice_.path)
        manager.copy_code(slice_)
        manager.commit_and_push(f"Stress test: {slice_.path}")

        # Record start SHA for diff later
        start_sha_result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(manager.TEST_REPO_PATH),
            capture_output=True, text=True
        )
        start_sha = start_sha_result.stdout.strip()

        # 3. Create PR in test repo
        print(f"[{run_id}] Step 3: Creating test PR...")
        pr = manager.create_pr(
            title=f"Stress test: {slice_.path}",
            body=f"AI Reviewer Stress Test - {slice_.line_count} lines from {slice_.path}"
        )
        result.test_pr = pr
        print(f"[{run_id}] Test PR: https://github.com/{TEST_REPO_OWNER}/{TEST_REPO_NAME}/pull/{pr}")

        if pr == 0:
            print(f"[{run_id}] Failed to create test PR - cannot proceed")
            result.error = "PR creation failed"
            result.time_to_green_minutes = int((time.time() - start_time) / 60)
            manager.close_pr()
            return result

        # 4. Wait for AI reviews
        print(f"[{run_id}] Step 4: Waiting for AI reviews (max {REVIEW_WAIT_MIN} min)...")
        collector = ReviewCollector(TEST_REPO_OWNER, TEST_REPO_NAME, pr)
        comments = collector.wait_for_reviews(timeout_min=REVIEW_WAIT_MIN)

        result.total_comments = len(comments)
        result.ai_reviewers_responded = list(set(c.reviewer for c in comments))
        result.comments_by_severity = collector.summarize_comments(comments).get("by_severity", {})

        print(f"[{run_id}] Got {len(comments)} comments from {result.ai_reviewers_responded}")

        if not comments:
            print(f"[{run_id}] No AI reviews received - closing test PR")
            result.fix_success = True
            result.time_to_green_minutes = int((time.time() - start_time) / 60)
            manager.close_pr()
            return result

        # 5. Run agento fix loop
        print(f"[{run_id}] Step 5: Running agent fix loop for {len(comments)} comments...")
        fix_success = False
        attempts = 0
        fix_loop_error: str | None = None
        try:
            fix_success, attempts = run_agento_fix_loop(
                collector=collector,
                comments=comments,
                test_repo_path=str(manager.TEST_REPO_PATH),
                branch=branch,
            )
        except Exception as e:
            fix_loop_error = str(e)
            print(f"[{run_id}] Fix loop error: {e}")

        result.agento_attempts = attempts
        result.fix_success = fix_success
        result.time_to_green_minutes = int((time.time() - start_time) / 60)

        # 6. If fixes were made, create PR in original repo
        if fix_success and attempts > 0:
            diff = _get_fix_diff(str(manager.TEST_REPO_PATH), start_sha)
            if diff.strip():
                print(f"[{run_id}] Step 6: Creating PR in original repo with fixes...")
                try:
                    original_pr = _apply_fixes_to_original(
                        diff=diff,
                        slice_name=slice_.path,
                        original_repo_path=str(ORIGINAL_REPO_PATH),
                    )
                    if original_pr:
                        result.original_pr = original_pr
                        result.fixes_applied_to_original = True
                        print(f"[{run_id}] Original PR: https://github.com/{ORIGINAL_REPO}/pull/{original_pr}")
                except Exception as e:
                    print(f"[{run_id}] Warning: could not apply fixes to original repo: {e}")
            else:
                print(f"[{run_id}] No diff to apply to original repo")

        # 7. Close test PR — always runs
        print(f"[{run_id}] Step 7: Closing test PR...")
        try:
            manager.close_pr()
        except Exception as e:
            print(f"[{run_id}] Warning: could not close test PR: {e}")

        if fix_loop_error:
            result.error = fix_loop_error

    except Exception as e:
        print(f"[{run_id}] Error: {e}")
        result.error = str(e)

    return result


def main() -> None:
    print("=" * 60)
    print("AI Reviewer Stress Test")
    print("=" * 60)

    # Run the stress test
    result = run_stress_test()

    # Log result
    ledger = OutcomeLedger()
    ledger.log(result)

    print("\n" + "=" * 60)
    print("RESULT SUMMARY")
    print("=" * 60)
    print(f"Slice: {result.slice}")
    print(f"Lines: {result.lines_reviewed}")
    print(f"Test PR: {result.test_pr}")
    print(f"Original PR: {result.original_pr or 'N/A'}")
    print(f"Comments: {result.total_comments}")
    print(f"Reviewers: {result.ai_reviewers_responded}")
    print(f"Agento attempts: {result.agento_attempts}")
    print(f"Success: {result.fix_success}")
    print(f"Fixes applied to original: {result.fixes_applied_to_original}")
    print(f"Time: {result.time_to_green_minutes} min")

    # Weekly summary
    print("\n" + "=" * 60)
    print("WEEKLY SUMMARY")
    print("=" * 60)
    summary = ledger.weekly_summary()
    for k, v in summary.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
