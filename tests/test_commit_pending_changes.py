"""
Unit tests for commit-pending-changes.sh logic via subprocess smoke test.

Tests the script in isolation using a temp git repo to avoid touching
the live ~/.smartclaw directory.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time
from pathlib import Path

import pytest


def make_git_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo with one tracked file and one untracked file."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=repo, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=repo, check=True, capture_output=True,
    )
    # Create tracked file and commit it
    (repo / "tracked.txt").write_text("initial\n")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=repo, check=True, capture_output=True,
    )
    # Add an untracked file
    (repo / "untracked.txt").write_text("untracked\n")
    return repo


class TestCommitPendingChanges:
    """Smoke tests for commit-pending-changes.sh in an isolated temp repo."""

    @pytest.fixture
    def script(self) -> str:
        return str(
            Path(__file__).parent.parent
            / "scripts"
            / "commit-pending-changes.sh"
        )

    def test_script_is_executable(self, script: str) -> None:
        """Script must be executable."""
        assert os.access(script, os.X_OK)

    def test_no_changes_skips_commit(self, script: str, tmp_path: Path) -> None:
        """When no tracked files are modified, script should exit 0 with no commit."""
        repo = make_git_repo(tmp_path)
        # remove untracked.txt so repo has truly no changes
        (repo / "untracked.txt").unlink()

        state = tmp_path / "state.json"
        lock = tmp_path / "lock"
        env = {
            "HOME": str(tmp_path),
            "CPC_LOCK_DIR": str(lock),
            "CPC_STATE_FILE": str(state),
            "CPC_REPO": str(repo),
            "CPC_USE_AO_FALLBACK": "0",
            "CPC_SKIP_GH_AUTH": "1",
            "PATH": os.environ["PATH"],
        }

        result = subprocess.run(
            ["bash", script],
            cwd=str(repo),
            capture_output=True,
            text=True,
            env=env,
        )
        # Should skip: nothing to commit
        assert "No changes detected" in result.stdout or "SKIP" in result.stdout
        # returncode 0=success with tracked work, 2=no-work (early return) — both are ok
        assert result.returncode in (0, 2), f"Unexpected returncode: {result.returncode}"

    def test_tracked_change_commits_and_updates_pr(
        self, script: str, tmp_path: Path
    ) -> None:
        """When a tracked file is modified, script commits and attempts PR update."""
        repo = make_git_repo(tmp_path)

        # Add a remote so gh commands don't fail immediately
        subprocess.run(
            ["git", "remote", "add", "origin", str(repo)],
            cwd=repo, check=True, capture_output=True,
        )

        state = tmp_path / "state.json"
        lock = tmp_path / "lock"
        log = tmp_path / "commit-pending.log"

        # Record commit count before
        before = int(
            subprocess.run(
                ["git", "rev-list", "--count", "HEAD"],
                cwd=repo, capture_output=True, text=True,
            ).stdout.strip()
        )

        # Modify tracked file
        (repo / "tracked.txt").write_text("modified\n")

        env = {
            "HOME": str(tmp_path),
            "CPC_LOCK_DIR": str(lock),
            "CPC_STATE_FILE": str(state),
            "CPC_REPO": str(repo),
            "CPC_COMMIT_LOG": str(log),
            "CPC_USE_AO_FALLBACK": "0",
            "CPC_SKIP_GH_AUTH": "1",
            "GH_TOKEN": "test",  # propagate via env dict, not monkeypatch
            "PATH": os.environ["PATH"],
        }

        result = subprocess.run(
            ["bash", script],
            cwd=str(repo),
            capture_output=True,
            text=True,
            env=env,
        )
        # Script should exit 0 and NOT say "nothing to commit"
        assert result.returncode == 0, f"Script failed: {result.stdout}"

        # Verify a commit was actually created (not just exit 0)
        after = int(
            subprocess.run(
                ["git", "rev-list", "--count", "HEAD"],
                cwd=repo, capture_output=True, text=True,
            ).stdout.strip()
        )
        assert after == before + 1, (
            f"Expected 1 new commit, got {after - before}. "
            f"Output: {result.stdout}"
        )
        # Verify a commit was actually created on the branch
        log_result = subprocess.run(
            ["git", "log", "--oneline"],
            cwd=str(repo),
            capture_output=True,
            text=True,
        )
        assert len(log_result.stdout.strip().splitlines()) >= 2, \
            "Expected at least 2 commits (initial + auto-commit)"

    def test_untracked_files_trigger_warning(
        self, script: str, tmp_path: Path
    ) -> None:
        """Untracked-only changes send warning and skip commit."""
        repo = make_git_repo(tmp_path)

        state = tmp_path / "state.json"
        lock = tmp_path / "lock"
        env = {
            "HOME": str(tmp_path),
            "CPC_LOCK_DIR": str(lock),
            "CPC_STATE_FILE": str(state),
            "CPC_REPO": str(repo),
            "CPC_USE_AO_FALLBACK": "0",
            "CPC_SKIP_GH_AUTH": "1",
            "CPC_DISABLE_SLACK": "1",  # exercise warn path without Slack credentials
            "PATH": os.environ["PATH"],
        }

        result = subprocess.run(
            ["bash", script],
            cwd=str(repo),
            capture_output=True,
            text=True,
            env=env,
        )
        # Must assert on the explicit "untracked files" warning log — not the loose
        # "WARN" fallback — so the test fails meaningfully if the warning is broken.
        assert "untracked files" in result.stdout.lower(), (
            f"Expected 'untracked files' in stdout, got: {result.stdout!r}"
        )
        assert "NOT auto-committed" in result.stdout
        # returncode 0=success with tracked work, 2=no-work (early return) — both are ok
        assert result.returncode in (0, 2), f"Unexpected returncode: {result.returncode}"

    def test_overlap_lock_prevents_concurrent_runs(
        self, script: str, tmp_path: Path
    ) -> None:
        """Second concurrent run should skip (overlap lock)."""
        repo = make_git_repo(tmp_path)

        lock = tmp_path / "lock"
        state = tmp_path / "state.json"
        env = {
            "HOME": str(tmp_path),
            "CPC_LOCK_DIR": str(lock),
            "CPC_STATE_FILE": str(state),
            "CPC_REPO": str(repo),
            "CPC_USE_AO_FALLBACK": "0",
            "CPC_SKIP_GH_AUTH": "1",
            "PATH": os.environ["PATH"],
        }

        # Start first run in background; it will hold the lock while running.
        # We start a trivial background bash that holds the lock dir open for 5s
        # while we launch the second test run concurrently.
        r1 = subprocess.Popen(
            ["bash", "-c", f"mkdir {lock!s} && sleep 5 && rmdir {lock!s}"],
            cwd=str(repo),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        try:
            # Wait for r1 to acquire the lock
            for _ in range(50):
                if lock.exists():
                    break
                time.sleep(0.1)
            assert lock.exists(), (
                f"Lock dir {lock} was not created by background process"
            )

            # Second run should find the held lock and skip
            r2 = subprocess.run(
                ["bash", script],
                cwd=str(repo),
                capture_output=True,
                text=True,
                env=env,
            )
            assert "SKIP" in r2.stdout, (
                f"Expected 'SKIP' (overlap lock) in output, got: {r2.stdout!r}"
            )
            assert r2.returncode == 0
        finally:
            # Clean up background process if still running
            if r1.poll() is None:
                r1.terminate()
                r1.wait()
            # Remove lock/state so subsequent test runs aren't affected
            if lock.exists():
                lock.rmdir()
            if state.exists():
                state.unlink()
