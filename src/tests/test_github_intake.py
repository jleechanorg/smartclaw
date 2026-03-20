"""Tests for GitHub intake daemon classification and dispatch logic.

TDD: These tests define the expected behavior of github-intake.sh.
The shell script sources lib/github-intake-lib.sh which contains the
testable classification functions.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LIB_PATH = REPO_ROOT / "lib" / "github-intake-lib.sh"
SCRIPT_PATH = REPO_ROOT / "scripts" / "github-intake.sh"


def run_bash_function(
    func_name: str,
    *args: str,
    env: dict[str, str] | None = None,
    stdin_data: str | None = None,
) -> subprocess.CompletedProcess:
    """Source the library and call a specific bash function."""
    # Use positional params via set -- to avoid shell quoting issues with JSON
    set_args = ""
    if args:
        escaped = []
        for a in args:
            # Write args to a heredoc-safe format
            escaped.append(a)
        # Pass args via env var and read with mapfile
        pass
    arg_str = " ".join(f'"${{args[{i}]}}"' for i in range(len(args)))
    # Build script that sets args array from env
    parts = []
    for i, a in enumerate(args):
        parts.append(f'args[{i}]={json.dumps(a)}')  # json.dumps handles quoting
    array_setup = "\n".join(parts)
    cmd = f'{array_setup}\nsource "{LIB_PATH}" && {func_name} {arg_str}'
    run_env = {**os.environ, **(env or {})}
    return subprocess.run(
        ["bash", "-c", cmd],
        capture_output=True,
        text=True,
        env=run_env,
        timeout=10,
        input=stdin_data,
    )


# ---------------------------------------------------------------------------
# Classification tests
# ---------------------------------------------------------------------------

class TestClassifyNotification:
    """Test notification classification logic."""

    def test_ci_failure_returns_auto_dispatch(self) -> None:
        notif = json.dumps({
            "reason": "ci_activity",
            "subject": {
                "title": "fix: resolve auth middleware",
                "type": "PullRequest",
                "url": "https://api.github.com/repos/jleechanorg/worldarchitect.ai/pulls/5960",
            },
            "repository": {"full_name": "jleechanorg/worldarchitect.ai"},
        })
        result = run_bash_function("classify_notification", notif)
        assert result.returncode == 0
        output = json.loads(result.stdout.strip())
        assert output["action"] == "auto-dispatch"
        assert output["reason"] == "ci_activity"

    def test_review_comment_returns_auto_dispatch(self) -> None:
        notif = json.dumps({
            "reason": "review_requested",
            "subject": {
                "title": "feat: add caching layer",
                "type": "PullRequest",
                "url": "https://api.github.com/repos/jleechanorg/worldarchitect.ai/pulls/5961",
            },
            "repository": {"full_name": "jleechanorg/worldarchitect.ai"},
        })
        result = run_bash_function("classify_notification", notif)
        assert result.returncode == 0
        output = json.loads(result.stdout.strip())
        assert output["action"] == "auto-dispatch"

    def test_bug_issue_returns_auto_dispatch(self) -> None:
        notif = json.dumps({
            "reason": "assign",
            "subject": {
                "title": "[bug] Login fails on mobile",
                "type": "Issue",
                "url": "https://api.github.com/repos/jleechanorg/worldarchitect.ai/issues/600",
            },
            "repository": {"full_name": "jleechanorg/worldarchitect.ai"},
        })
        result = run_bash_function("classify_notification", notif)
        assert result.returncode == 0
        output = json.loads(result.stdout.strip())
        assert output["action"] == "auto-dispatch"

    def test_subscribed_noise_returns_skip(self) -> None:
        notif = json.dumps({
            "reason": "subscribed",
            "subject": {
                "title": "Update README.md",
                "type": "PullRequest",
                "url": "https://api.github.com/repos/jleechanorg/worldarchitect.ai/pulls/100",
            },
            "repository": {"full_name": "jleechanorg/worldarchitect.ai"},
        })
        result = run_bash_function("classify_notification", notif)
        assert result.returncode == 0
        output = json.loads(result.stdout.strip())
        assert output["action"] == "skip"

    def test_ci_checksuite_on_main_skipped(self) -> None:
        notif = json.dumps({
            "reason": "ci_activity",
            "subject": {
                "title": "Auto-Deploy Dev workflow run failed for main branch",
                "type": "CheckSuite",
                "url": "https://api.github.com/repos/jleechanorg/worldarchitect.ai/check-suites/123",
            },
            "repository": {"full_name": "jleechanorg/worldarchitect.ai"},
        })
        result = run_bash_function("classify_notification", notif)
        assert result.returncode == 0
        output = json.loads(result.stdout.strip())
        assert output["action"] == "skip"
        assert output["reason"] == "ci_checksuite_noise"

    def test_ci_on_pull_request_dispatched(self) -> None:
        notif = json.dumps({
            "reason": "ci_activity",
            "subject": {
                "title": "fix: auth middleware",
                "type": "PullRequest",
                "url": "https://api.github.com/repos/jleechanorg/worldarchitect.ai/pulls/5960",
            },
            "repository": {"full_name": "jleechanorg/worldarchitect.ai"},
        })
        result = run_bash_function("classify_notification", notif)
        assert result.returncode == 0
        output = json.loads(result.stdout.strip())
        assert output["action"] == "auto-dispatch"
        assert output["reason"] == "ci_activity"

    def test_feature_request_returns_escalate(self) -> None:
        notif = json.dumps({
            "reason": "mention",
            "subject": {
                "title": "[feature] Add dark mode support",
                "type": "Issue",
                "url": "https://api.github.com/repos/jleechanorg/worldarchitect.ai/issues/601",
            },
            "repository": {"full_name": "jleechanorg/worldarchitect.ai"},
        })
        result = run_bash_function("classify_notification", notif)
        assert result.returncode == 0
        output = json.loads(result.stdout.strip())
        assert output["action"] == "escalate"

    def test_dependabot_pr_returns_auto_dispatch(self) -> None:
        notif = json.dumps({
            "reason": "security_alert",
            "subject": {
                "title": "Bump lodash from 4.17.20 to 4.17.21",
                "type": "PullRequest",
                "url": "https://api.github.com/repos/jleechanorg/worldarchitect.ai/pulls/5962",
            },
            "repository": {"full_name": "jleechanorg/worldarchitect.ai"},
        })
        result = run_bash_function("classify_notification", notif)
        assert result.returncode == 0
        output = json.loads(result.stdout.strip())
        assert output["action"] == "auto-dispatch"
        assert output.get("conservative") is True

    def test_author_pr_notification_returns_auto_dispatch(self) -> None:
        """Test that author notifications on PRs (CI results, review comments) are auto-dispatched."""
        notif = json.dumps({
            "reason": "author",
            "subject": {
                "title": "CI: test-unit failed on main branch",
                "type": "PullRequest",
                "url": "https://api.github.com/repos/jleechanorg/worldarchitect.ai/pulls/5963",
            },
            "repository": {"full_name": "jleechanorg/worldarchitect.ai"},
        })
        result = run_bash_function("classify_notification", notif)
        assert result.returncode == 0
        output = json.loads(result.stdout.strip())
        assert output["action"] == "auto-dispatch"
        assert output["reason"] == "author_pr"

    def test_author_notification_on_non_pr_is_escalated(self) -> None:
        """Test that author notifications on non-PRs are escalated (not auto-dispatched)."""
        notif = json.dumps({
            "reason": "author",
            "subject": {
                "title": "New comment on your issue",
                "type": "Issue",
                "url": "https://api.github.com/repos/jleechanorg/worldarchitect.ai/issues/600",
            },
            "repository": {"full_name": "jleechanorg/worldarchitect.ai"},
        })
        result = run_bash_function("classify_notification", notif)
        assert result.returncode == 0
        output = json.loads(result.stdout.strip())
        # Should escalate since it's not a PR - the code checks subject_type == "PullRequest"
        assert output["action"] == "escalate"


# ---------------------------------------------------------------------------
# Agent selection tests
# ---------------------------------------------------------------------------

class TestSelectAgent:
    """Test agent CLI selection heuristics."""

    def test_backend_repo_selects_codex(self) -> None:
        result = run_bash_function(
            "select_agent", "jleechanorg/worldarchitect.ai", "fix: server auth crash"
        )
        assert result.returncode == 0
        assert result.stdout.strip() == "codex"

    def test_frontend_title_selects_claude(self) -> None:
        result = run_bash_function(
            "select_agent", "jleechanorg/ai_universe_frontend", "fix: React component render"
        )
        assert result.returncode == 0
        assert result.stdout.strip() == "claude"

    def test_default_selects_claude(self) -> None:
        result = run_bash_function(
            "select_agent", "jleechanorg/beads", "update docs"
        )
        assert result.returncode == 0
        assert result.stdout.strip() == "claude"


# ---------------------------------------------------------------------------
# Cooldown / dedup tests
# ---------------------------------------------------------------------------

class TestCooldown:
    """Test cooldown and dedup logic."""

    def test_pr_within_cooldown_is_skipped(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            # PR 5960 dispatched 10 minutes ago (within 1h cooldown)
            import time
            state = {
                "dispatched_prs": {"5960": int(time.time()) - 600},
            }
            json.dump(state, f)
            f.flush()
            result = run_bash_function(
                "check_cooldown", "5960", "3600",
                env={"INTAKE_STATE_FILE": f.name},
            )
            os.unlink(f.name)
        assert result.returncode == 0
        assert result.stdout.strip() == "cooldown"

    def test_pr_outside_cooldown_is_allowed(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            import time
            state = {
                "dispatched_prs": {"5960": int(time.time()) - 7200},
            }
            json.dump(state, f)
            f.flush()
            result = run_bash_function(
                "check_cooldown", "5960", "3600",
                env={"INTAKE_STATE_FILE": f.name},
            )
            os.unlink(f.name)
        assert result.returncode == 0
        assert result.stdout.strip() == "ok"

    def test_unknown_pr_is_allowed(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            state = {"dispatched_prs": {}}
            json.dump(state, f)
            f.flush()
            result = run_bash_function(
                "check_cooldown", "9999", "3600",
                env={"INTAKE_STATE_FILE": f.name},
            )
            os.unlink(f.name)
        assert result.returncode == 0
        assert result.stdout.strip() == "ok"


# ---------------------------------------------------------------------------
# Rate limit tests
# ---------------------------------------------------------------------------

class TestRateLimit:
    """Test per-run dispatch rate limiting."""

    def test_under_limit_allowed(self) -> None:
        result = run_bash_function("check_rate_limit", "2", "3")
        assert result.returncode == 0
        assert result.stdout.strip() == "ok"

    def test_at_limit_blocked(self) -> None:
        result = run_bash_function("check_rate_limit", "3", "3")
        assert result.returncode == 0
        assert result.stdout.strip() == "limited"

    def test_over_limit_blocked(self) -> None:
        result = run_bash_function("check_rate_limit", "5", "3")
        assert result.returncode == 0
        assert result.stdout.strip() == "limited"


# ---------------------------------------------------------------------------
# Digest formatting tests
# ---------------------------------------------------------------------------

class TestDigest:
    """Test Slack digest message formatting."""

    def test_digest_with_all_categories(self) -> None:
        result = run_bash_function("format_digest", "3", "1", "12")
        assert result.returncode == 0
        msg = result.stdout.strip()
        assert "dispatched 3" in msg
        assert "escalated 1" in msg
        assert "skipped 12" in msg

    def test_digest_with_zero_dispatched(self) -> None:
        result = run_bash_function("format_digest", "0", "0", "5")
        assert result.returncode == 0
        msg = result.stdout.strip()
        assert "dispatched 0" in msg


# ---------------------------------------------------------------------------
# Extract PR number tests
# ---------------------------------------------------------------------------

class TestExtractPrNumber:
    """Test PR number extraction from GitHub API URLs."""

    def test_extracts_pr_number_from_pulls_url(self) -> None:
        url = "https://api.github.com/repos/jleechanorg/worldarchitect.ai/pulls/5960"
        result = run_bash_function("extract_pr_number", url)
        assert result.returncode == 0
        assert result.stdout.strip() == "5960"

    def test_extracts_issue_number(self) -> None:
        url = "https://api.github.com/repos/jleechanorg/worldarchitect.ai/issues/600"
        result = run_bash_function("extract_pr_number", url)
        assert result.returncode == 0
        assert result.stdout.strip() == "600"

    def test_empty_on_invalid_url(self) -> None:
        result = run_bash_function("extract_pr_number", "not-a-url")
        assert result.returncode == 0
        assert result.stdout.strip() == ""


# ---------------------------------------------------------------------------
# Repo to AO project mapping tests
# ---------------------------------------------------------------------------

class TestRepoToAoProject:
    """Test GitHub repo → AO project ID mapping."""

    def test_worldarchitect_maps_correctly(self) -> None:
        result = run_bash_function("repo_to_ao_project", "jleechanorg/worldarchitect.ai")
        assert result.returncode == 0
        assert result.stdout.strip() == "worldarchitect"

    def test_jleechanclaw_maps_correctly(self) -> None:
        result = run_bash_function("repo_to_ao_project", "jleechanorg/jleechanclaw")
        assert result.returncode == 0
        assert result.stdout.strip() == "jleechanclaw"

    def test_worldai_claw_maps_correctly(self) -> None:
        result = run_bash_function("repo_to_ao_project", "jleechanorg/worldai_claw")
        assert result.returncode == 0
        assert result.stdout.strip() == "worldai-claw"

    def test_unknown_repo_returns_empty(self) -> None:
        result = run_bash_function("repo_to_ao_project", "someorg/unknown-repo")
        assert result.returncode == 0
        assert result.stdout.strip() == ""
