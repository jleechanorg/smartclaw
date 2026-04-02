#!/usr/bin/env python3
"""Test Repo Manager - syncs and manages the test repo."""
from __future__ import annotations

import subprocess
from pathlib import Path
from datetime import date


class TestRepoManager:
    """Manages the test repo for AI review stress testing."""

    TEST_REPO_OWNER = "jleechanorg"
    TEST_REPO_NAME = "smartclaw-review-test"
    TEST_REPO_PATH = Path.home() / "projects_reference" / TEST_REPO_NAME

    def __init__(self, original_repo_path: str) -> None:
        self.original_repo_path = Path(original_repo_path)
        self._ensure_test_repo()

    def _ensure_test_repo(self) -> None:
        """Ensure test repo exists and is accessible."""
        if not self.TEST_REPO_PATH.exists():
            print(f"Cloning test repo to {self.TEST_REPO_PATH}...")
            self._run(["git", "clone",
                      f"git@github.com:{self.TEST_REPO_OWNER}/{self.TEST_REPO_NAME}.git",
                      str(self.TEST_REPO_PATH)], cwd=self.TEST_REPO_PATH.parent)

    def _run(self, cmd: list[str], cwd: str | Path | None = None, check: bool = True) -> str:
        """Run shell command."""
        result = subprocess.run(
            cmd, cwd=cwd or self.TEST_REPO_PATH,
            capture_output=True, text=True
        )
        if check and result.returncode != 0:
            raise RuntimeError(f"Command failed: {' '.join(cmd)}\n{result.stderr}")
        return result.stdout.strip()

    def sync_from_origin(self) -> None:
        """Ensure test repo is up to date with origin."""
        print("Syncing test repo from origin...")
        self._run(["git", "fetch", "origin"])
        self._run(["git", "reset", "--hard", "origin/main"])

    def create_review_branch(self, slice_name: str) -> str:
        """Create branch review-YYYY-MM-DD-<slice>."""
        branch = f"review-{date.today()}-{slice_name[:30]}"

        # Check if branch exists and delete
        try:
            self._run(["git", "checkout", "main"])
            self._run(["git", "branch", "-D", branch], check=False)
        except RuntimeError:
            pass

        self._run(["git", "checkout", "-b", branch])
        return branch

    def copy_code(self, slice_: object, target_dir: str = "src/test_slice") -> None:
        """Copy slice files to test repo."""
        # Copy files preserving original directory structure
        # NOTE: slice_.files contains paths relative to the repo root
        # (e.g. "scripts/review_stress_test/run.py"), so use them directly.
        for file_rel in slice_.files:  # type: ignore[attr-defined]
            src = self.original_repo_path / file_rel
            if src.exists():
                dst = self.TEST_REPO_PATH / file_rel
                dst.parent.mkdir(parents=True, exist_ok=True)

                # Copy file
                with open(src) as f:
                    content = f.read()
                with open(dst, 'w') as f:
                    f.write(content)

        print(f"Copied {len(slice_.files)} files preserving structure")  # type: ignore[attr-defined]

    def commit_and_push(self, message: str) -> None:
        """Commit and push changes."""
        self._run(["git", "add", "."])
        self._run(["git", "commit", "-m", message])
        self._run(["git", "push", "-u", "origin", "HEAD"])

    def create_pr(self, title: str, body: str = "") -> int:
        """Create PR, return PR number."""
        result = self._run([
            "gh", "pr", "create",
            "--title", title,
            "--body", body or f"AI Reviewer Stress Test: {title}",
            "--base", "main"
        ])
        # Extract PR number from URL
        if "pull/" in result:
            return int(result.split("pull/")[-1].split("/")[0])
        return 0

    def close_pr(self) -> None:
        """Close current PR and delete branch."""
        try:
            # Get current branch
            branch = self._run(["git", "branch", "--show-current"])
            self._run(["gh", "pr", "close"], check=False)
            self._run(["git", "checkout", "main"])
            self._run(["git", "branch", "-D", branch], check=False)
            self._run(["git", "push", "origin", "--delete", branch], check=False)
        except Exception as e:
            print(f"Cleanup error (non-fatal): {e}")


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: test_repo_manager.py <original_repo_path>")
        sys.exit(1)

    manager = TestRepoManager(sys.argv[1])
    manager.sync_from_origin()
    print("Test repo ready!")
