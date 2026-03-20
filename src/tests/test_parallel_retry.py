"""Tests for parallel_retry: speculative parallel CI fix attempts.

These tests verify:
- Parseable CI failure → generates 2-3 distinct fix strategies
- Each strategy gets different approach description injected into agent prompt
- Strategies are explicitly non-overlapping (no duplicate approaches)
- Unparseable CI failure → falls back to single enriched retry (graceful degradation)
- Budget with only 1 attempt remaining → single retry, not parallel
- Spawns N parallel AO sessions via ao_spawn, each on its own worktree
- First session with CI green → kills remaining sessions via ao_kill
- All sessions fail → records all strategies as failed, escalates
- Outcome recorded: {error_class, winning_strategy, losing_strategies}
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch

# These imports will fail until parallel_retry.py is implemented (TDD)
from orchestration.parallel_retry import (
    FixStrategy,
    ParallelRetryResult,
    generate_fix_strategies,
    execute_parallel_retry,
    ParallelRetryError,
    parse_ci_error,
    is_parseable_ci_failure,
    check_ci_status,
    _extract_repo_from_worktree,
    derive_error_class,
)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_ao_cli() -> MagicMock:
    """Create a mock AO CLI wrapper."""
    cli = MagicMock()
    cli.spawn.return_value = "ao-session-test"
    cli.kill.return_value = None
    return cli


@pytest.fixture
def sample_ci_failure_parseable() -> str:
    return """Error: Test failed in test_auth.py::test_login
FAILED test_auth.py::test_login - AssertionError: assert user.is_authenticated is True
Expected: True
Actual: False
"""


@pytest.fixture
def sample_ci_failure_unparseable() -> str:
    return """The build failed for some reason. Something went wrong.
Please check the logs and fix the issue.
"""


@pytest.fixture
def sample_diff() -> str:
    return """diff --git a/src/auth.py b/src/auth.py
-        return self._verify_credentials(username, password)
+        return self._verify_credentials(username, password) and self._update_session()
"""


# ---------------------------------------------------------------------------
# FixStrategy and ParallelRetryResult dataclasses
# ---------------------------------------------------------------------------


def test_fixstrategy_fields() -> None:
    strategy = FixStrategy(
        approach_id="approach-001",
        description="Fix authentication by updating session",
        prompt_injection="Consider using session.update() to fix the auth issue",
    )
    assert strategy.approach_id == "approach-001"
    assert strategy.description == "Fix authentication by updating session"
    assert "session.update()" in strategy.prompt_injection


def test_fixstrategy_equality() -> None:
    s1 = FixStrategy("app-1", "desc", "inj")
    s2 = FixStrategy("app-1", "different desc", "different inj")
    assert s1 == s2


def test_parallelretryresult_winner() -> None:
    winner = FixStrategy("win", "winner desc", "winner inj")
    result = ParallelRetryResult(winner=winner, sessions_spawned=3, sessions_killed=2)
    assert result.winner == winner
    assert result.sessions_spawned == 3
    assert result.sessions_killed == 2


def test_parallelretryresult_no_winner() -> None:
    result = ParallelRetryResult(winner=None, sessions_spawned=3, sessions_killed=3)
    assert result.winner is None


# ---------------------------------------------------------------------------
# CI error parsing
# ---------------------------------------------------------------------------


def test_parse_ci_error_extracts_test_name(sample_ci_failure_parseable: str) -> None:
    parsed = parse_ci_error(sample_ci_failure_parseable)
    assert parsed is not None
    assert "test_login" in parsed.get("test_name", "")


def test_parse_ci_error_returns_none_for_unparseable(sample_ci_failure_unparseable: str) -> None:
    parsed = parse_ci_error(sample_ci_failure_unparseable)
    assert parsed is None


def test_is_parseable_ci_failure_true(sample_ci_failure_parseable: str) -> None:
    assert is_parseable_ci_failure(sample_ci_failure_parseable) is True


def test_is_parseable_ci_failure_false(sample_ci_failure_unparseable: str) -> None:
    assert is_parseable_ci_failure(sample_ci_failure_unparseable) is False


# ---------------------------------------------------------------------------
# Strategy generation
# ---------------------------------------------------------------------------


def test_generate_fix_strategies_returns_list(sample_ci_failure_parseable: str, sample_diff: str) -> None:
    strategies = generate_fix_strategies(sample_ci_failure_parseable, sample_diff)
    assert isinstance(strategies, list)
    assert len(strategies) > 0


def test_generate_fix_strategies_max_count(sample_ci_failure_parseable: str, sample_diff: str) -> None:
    strategies = generate_fix_strategies(sample_ci_failure_parseable, sample_diff, max_strategies=2)
    assert len(strategies) <= 2


def test_generate_fix_strategies_distinct_approaches(sample_ci_failure_parseable: str, sample_diff: str) -> None:
    strategies = generate_fix_strategies(sample_ci_failure_parseable, sample_diff, max_strategies=3)
    approach_ids = [s.approach_id for s in strategies]
    assert len(approach_ids) == len(set(approach_ids)), "Duplicate approach_ids found"


def test_generate_fix_strategies_unique_descriptions(sample_ci_failure_parseable: str, sample_diff: str) -> None:
    """Strategy descriptions should be non-overlapping."""
    strategies = generate_fix_strategies(sample_ci_failure_parseable, sample_diff, max_strategies=3)
    for i, s1 in enumerate(strategies):
        for s2 in strategies[i + 1:]:
            words1 = set(s1.description.lower().split())
            words2 = set(s2.description.lower().split())
            overlap = len(words1 & words2) / max(len(words1 | words2), 1)
            assert overlap < 0.5, f"Overlapping: {s1.description} vs {s2.description}"


def test_generate_fix_strategies_prompt_injection(sample_ci_failure_parseable: str, sample_diff: str) -> None:
    strategies = generate_fix_strategies(sample_ci_failure_parseable, sample_diff)
    for strategy in strategies:
        assert strategy.prompt_injection is not None
        assert len(strategy.prompt_injection) > 0


def test_generate_fix_strategies_fallback_for_unparseable(sample_ci_failure_unparseable: str, sample_diff: str) -> None:
    """Unparseable CI failure should return single fallback strategy."""
    strategies = generate_fix_strategies(sample_ci_failure_unparseable, sample_diff)
    assert len(strategies) == 1
    assert "retry" in strategies[0].description.lower() or "enriched" in strategies[0].prompt_injection.lower()


# ---------------------------------------------------------------------------
# Parallel execution
# ---------------------------------------------------------------------------


def test_execute_parallel_retry_spawns_multiple_sessions(mock_ao_cli: MagicMock, sample_diff: str) -> None:
    strategies = [
        FixStrategy("app-1", "Fix 1", "Try approach 1"),
        FixStrategy("app-2", "Fix 2", "Try approach 2"),
        FixStrategy("app-3", "Fix 3", "Try approach 3"),
    ]
    mock_ao_cli.spawn.side_effect = ["session-1", "session-2", "session-3"]

    with patch("orchestration.parallel_retry.check_ci_status") as mock_ci:
        mock_ci.side_effect = [
            {"status": "green", "session_id": "session-1"},
            {"status": "red", "session_id": "session-2"},
            {"status": "red", "session_id": "session-3"},
        ]
        result = execute_parallel_retry(
            strategies=strategies,
            project="jleechanorg/test",
            issue="Fix auth bug",
            cli=mock_ao_cli,
        )
    assert mock_ao_cli.spawn.call_count == 3


def test_execute_parallel_retry_first_green_wins(mock_ao_cli: MagicMock, sample_diff: str) -> None:
    strategies = [
        FixStrategy("app-1", "Fix 1", "Try approach 1"),
        FixStrategy("app-2", "Fix 2", "Try approach 2"),
    ]
    mock_ao_cli.spawn.side_effect = ["session-1", "session-2"]

    with patch("orchestration.parallel_retry.check_ci_status") as mock_ci:
        mock_ci.side_effect = [
            {"status": "red", "session_id": "session-1"},
            {"status": "green", "session_id": "session-2"},
        ]
        result = execute_parallel_retry(
            strategies=strategies,
            project="jleechanorg/test",
            issue="Fix auth bug",
            cli=mock_ao_cli,
        )
    assert result.winner is not None
    assert result.winner.approach_id == "app-2"


def test_execute_parallel_retry_kills_losers_on_win(mock_ao_cli: MagicMock, sample_diff: str) -> None:
    strategies = [
        FixStrategy("app-1", "Fix 1", "Try approach 1"),
        FixStrategy("app-2", "Fix 2", "Try approach 2"),
        FixStrategy("app-3", "Fix 3", "Try approach 3"),
    ]
    mock_ao_cli.spawn.side_effect = ["session-1", "session-2", "session-3"]

    with patch("orchestration.parallel_retry.check_ci_status") as mock_ci:
        mock_ci.side_effect = [
            {"status": "green", "session_id": "session-1"},
            {"status": "pending", "session_id": "session-2"},
            {"status": "pending", "session_id": "session-3"},
        ]
        result = execute_parallel_retry(
            strategies=strategies,
            project="jleechanorg/test",
            issue="Fix auth bug",
            cli=mock_ao_cli,
        )
    assert result.sessions_killed == 2


def test_execute_parallel_retry_all_fail_escalates(mock_ao_cli: MagicMock, sample_diff: str) -> None:
    strategies = [
        FixStrategy("app-1", "Fix 1", "Try approach 1"),
        FixStrategy("app-2", "Fix 2", "Try approach 2"),
    ]
    mock_ao_cli.spawn.side_effect = ["session-1", "session-2"]

    with patch("orchestration.parallel_retry.check_ci_status") as mock_ci:
        mock_ci.side_effect = [
            {"status": "red", "session_id": "session-1"},
            {"status": "red", "session_id": "session-2"},
        ]
        result = execute_parallel_retry(
            strategies=strategies,
            project="jleechanorg/test",
            issue="Fix auth bug",
            cli=mock_ao_cli,
        )
    assert result.winner is None
    assert result.sessions_spawned == 2


def test_execute_parallel_retry_single_attempt_no_parallel(mock_ao_cli: MagicMock, sample_diff: str) -> None:
    """Budget with 1 attempt remaining should do single retry, not parallel."""
    strategies = [FixStrategy("app-1", "Fix 1", "Try approach 1")]
    result = execute_parallel_retry(
        strategies=strategies,
        project="jleechanorg/test",
        issue="Fix auth bug",
        cli=mock_ao_cli,
        max_parallel=1,
    )
    assert mock_ao_cli.spawn.call_count <= 1


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_parallel_retry_handles_spawn_failure(mock_ao_cli: MagicMock, sample_diff: str) -> None:
    from orchestration.ao_cli import AOCommandError
    strategies = [
        FixStrategy("app-1", "Fix 1", "Try approach 1"),
        FixStrategy("app-2", "Fix 2", "Try approach 2"),
    ]
    mock_ao_cli.spawn.side_effect = AOCommandError("Spawn failed", returncode=1, stderr="error")

    with pytest.raises(ParallelRetryError):
        execute_parallel_retry(
            strategies=strategies,
            project="jleechanorg/test",
            issue="Fix auth bug",
            cli=mock_ao_cli,
        )


def test_parallel_retry_handles_kill_failure_non_blocking(mock_ao_cli: MagicMock, sample_diff: str) -> None:
    """ao_kill failure should be non-blocking."""
    from orchestration.ao_cli import AOCommandError
    strategies = [FixStrategy("app-1", "Fix 1", "Try approach 1")]
    mock_ao_cli.spawn.return_value = "session-1"
    mock_ao_cli.kill.side_effect = AOCommandError("Kill failed", returncode=1, stderr="error")

    with patch("orchestration.parallel_retry.check_ci_status") as mock_ci:
        mock_ci.return_value = {"status": "green", "session_id": "session-1"}
        result = execute_parallel_retry(
            strategies=strategies,
            project="jleechanorg/test",
            issue="Fix auth bug",
            cli=mock_ao_cli,
        )
        assert result.winner is not None


def test_pending_session_repolled_until_green(mock_ao_cli: MagicMock) -> None:
    """A session initially pending must be re-polled each round and can go green later."""
    strategies = [
        FixStrategy("app-1", "Fix 1", "Try approach 1"),
        FixStrategy("app-2", "Fix 2", "Try approach 2"),
    ]
    mock_ao_cli.spawn.side_effect = ["session-1", "session-2"]

    with patch("orchestration.parallel_retry.check_ci_status") as mock_ci:
        with patch("orchestration.parallel_retry.time") as mock_time:
            mock_time.time.return_value = 0  # prevent timeout
            mock_ci.side_effect = [
                # Round 1: both pending
                {"status": "pending", "session_id": "session-1"},
                {"status": "pending", "session_id": "session-2"},
                # Round 2: session-1 goes green
                {"status": "green", "session_id": "session-1"},
            ]
            result = execute_parallel_retry(
                strategies=strategies,
                project="jleechanorg/test",
                issue="Fix auth bug",
                cli=mock_ao_cli,
                ci_timeout=9999,
                ci_check_interval=0,
            )

    assert result.winner is not None, "Pending session must be re-polled and found as winner"
    assert result.winner.approach_id == "app-1"


# ---------------------------------------------------------------------------
# CI Status Checking
# ---------------------------------------------------------------------------


def test_check_ci_status_green() -> None:
    """gh run list returns completed/success -> returns green."""
    mock_mapping = MagicMock()
    mock_mapping.branch = "fix/test-branch"
    mock_mapping.worktree_path = "/path/to/worktrees/jleechanorg-test/test-branch"

    with patch("orchestration.parallel_retry.get_mapping") as mock_get_mapping:
        mock_get_mapping.return_value = mock_mapping

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout='[{"status": "completed", "conclusion": "success"}]',
                stderr="",
            )
            result = check_ci_status("test-session-id", bead_id="test-bead-id")

    assert result == {"status": "green", "session_id": "test-session-id"}


def test_check_ci_status_red() -> None:
    """gh run list returns completed/failure -> returns red."""
    mock_mapping = MagicMock()
    mock_mapping.branch = "fix/test-branch"
    mock_mapping.worktree_path = "/path/to/worktrees/jleechanorg-test/test-branch"

    with patch("orchestration.parallel_retry.get_mapping") as mock_get_mapping:
        mock_get_mapping.return_value = mock_mapping

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout='[{"status": "completed", "conclusion": "failure"}]',
                stderr="",
            )
            result = check_ci_status("test-session-id", bead_id="test-bead-id")

    assert result == {"status": "red", "session_id": "test-session-id"}


def test_check_ci_status_pending_in_progress() -> None:
    """gh run list returns in_progress -> returns pending."""
    mock_mapping = MagicMock()
    mock_mapping.branch = "fix/test-branch"
    mock_mapping.worktree_path = "/path/to/worktrees/jleechanorg-test/test-branch"

    with patch("orchestration.parallel_retry.get_mapping") as mock_get_mapping:
        mock_get_mapping.return_value = mock_mapping

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout='[{"status": "in_progress", "conclusion": null}]',
                stderr="",
            )
            result = check_ci_status("test-session-id", bead_id="test-bead-id")

    assert result == {"status": "pending", "session_id": "test-session-id"}


def test_check_ci_status_pending_empty() -> None:
    """gh run list returns empty list -> returns pending."""
    mock_mapping = MagicMock()
    mock_mapping.branch = "fix/test-branch"
    mock_mapping.worktree_path = "/path/to/worktrees/jleechanorg-test/test-branch"

    with patch("orchestration.parallel_retry.get_mapping") as mock_get_mapping:
        mock_get_mapping.return_value = mock_mapping

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="[]",
                stderr="",
            )
            result = check_ci_status("test-session-id", bead_id="test-bead-id")

    assert result == {"status": "pending", "session_id": "test-session-id"}


def test_check_ci_status_pending_gh_fails() -> None:
    """gh exits non-zero -> returns pending (fail-open)."""
    mock_mapping = MagicMock()
    mock_mapping.branch = "fix/test-branch"
    mock_mapping.worktree_path = "/path/to/worktrees/jleechanorg-test/test-branch"

    with patch("orchestration.parallel_retry.get_mapping") as mock_get_mapping:
        mock_get_mapping.return_value = mock_mapping

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1,
                stdout="",
                stderr="gh: not logged in",
            )
            result = check_ci_status("test-session-id", bead_id="test-bead-id")

    assert result == {"status": "pending", "session_id": "test-session-id"}


def test_check_ci_status_no_mapping() -> None:
    """No bead mapping found -> returns pending without touching gh CLI."""
    with patch("orchestration.parallel_retry.get_mapping") as mock_get_mapping:
        mock_get_mapping.return_value = None

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
            result = check_ci_status("unknown-session-id", bead_id="unknown-bead-id")

    assert result == {"status": "pending", "session_id": "unknown-session-id"}


def test_check_ci_status_invalid_worktree_path() -> None:
    """Worktree path with no parseable repo -> returns pending."""
    mock_mapping = MagicMock()
    mock_mapping.branch = "fix/test-branch"
    mock_mapping.worktree_path = "/invalid/path/test-branch"

    with patch("orchestration.parallel_retry.get_mapping") as mock_get_mapping:
        mock_get_mapping.return_value = mock_mapping

        with patch("subprocess.run") as mock_run:
            # git remote returns nothing useful
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
            result = check_ci_status("test-session-id", bead_id="test-bead-id")

    assert result == {"status": "pending", "session_id": "test-session-id"}


# ---------------------------------------------------------------------------
# Repo Extraction
# ---------------------------------------------------------------------------


def test_extract_repo_from_worktree_valid() -> None:
    """Valid worktree path extracts correct repo."""
    result = _extract_repo_from_worktree("/path/to/worktrees/jleechanorg-test/fix/branch")
    assert result == "jleechanorg/test"


def test_extract_repo_from_worktree_no_worktrees() -> None:
    """Path without worktrees returns None."""
    result = _extract_repo_from_worktree("/some/other/path/branch")
    assert result is None


def test_extract_repo_from_worktree_deep_path() -> None:
    """Deep path: rpartition on last hyphen handles hyphens in owner correctly."""
    result = _extract_repo_from_worktree("/home/user/.openclaw/worktrees/my-org-repo/feature/test")
    assert result == "my-org/repo"


# ---------------------------------------------------------------------------
# Error class derivation
# ---------------------------------------------------------------------------


def test_derive_error_class_from_type() -> None:
    """Error type should be derived from CI failure and normalized to kebab-case."""
    ci_failure = "ERROR: Some error\nTraceback (most recent call last):\nNameError: name 'foo' is not defined"
    error_class = derive_error_class(ci_failure)
    assert error_class == "ci-failed:name-error"


def test_derive_error_class_from_import_error() -> None:
    """ImportError should produce correct error class in kebab-case."""
    ci_failure = "FAILED test_module.py::test_import\nImportError: cannot import 'missing'"
    error_class = derive_error_class(ci_failure)
    assert error_class == "ci-failed:import-error"


def test_derive_error_class_from_test_name() -> None:
    """Test name should be used when no error type."""
    ci_failure = "FAILED test_foo.py::test_bar\nExpected: 1, Got: 2"
    error_class = derive_error_class(ci_failure)
    assert error_class == "ci-failed:test:test_bar"


def test_derive_error_class_from_file() -> None:
    """File path should be used when no error type or test name."""
    ci_failure = "ERROR: Something failed\n    at /path/to/utils.py:42"
    error_class = derive_error_class(ci_failure)
    assert error_class == "ci-failed:file:utils"


def test_derive_error_class_unparseable_returns_none() -> None:
    """Unparseable CI failure should return None."""
    ci_failure = "The build failed for some reason"
    error_class = derive_error_class(ci_failure)
    assert error_class is None


def test_derive_error_class_returns_none_for_generic_failure() -> None:
    """Generic failure message without parseable patterns returns None."""
    ci_failure = "CI build failed"
    error_class = derive_error_class(ci_failure)
    assert error_class is None


# ---------------------------------------------------------------------------
# File path regex — no leading slash case
# ---------------------------------------------------------------------------


def test_parse_ci_error_file_no_slash() -> None:
    """File path regex must match 'in test_auth.py:42' (no directory prefix)."""
    ci_failure = "FAILED tests/test_auth.py::test_login\n AssertionError: in test_auth.py:42"
    parsed = parse_ci_error(ci_failure)
    assert parsed is not None
    assert parsed.get("file") is not None, "file should be extracted without requiring '/' prefix"


def test_derive_error_class_file_no_slash() -> None:
    """derive_error_class should classify by file even when path has no slash."""
    ci_failure = "AssertionError\n  in utils.py:99"
    error_class = derive_error_class(ci_failure)
    # Should derive file class since error_type is AssertionError
    assert error_class is not None


def test_load_winning_strategies_returns_empty_for_unknown_class(tmp_path) -> None:
    """Load winning strategies returns empty list for unknown error class."""
    from orchestration.parallel_retry import load_winning_strategies
    
    outcomes_file = tmp_path / "outcomes.jsonl"
    strategies = load_winning_strategies("unknown-error-class", outcomes_path=str(outcomes_file))
    assert strategies == []


def test_load_winning_strategies_parses_outcome_file(tmp_path) -> None:
    """Load winning strategies parses outcomes.jsonl and returns strategies."""
    from orchestration.parallel_retry import load_winning_strategies
    
    outcomes_file = tmp_path / "outcomes.jsonl"
    # Write test outcomes (matching OutcomeRecorder format)
    # fix-imports: 3 wins, 0 losses = 100%
    # fix-types: 1 win, 1 loss = 50%
    outcomes_file.write_text('''{"error_class": "test-error", "winning_strategy": {"approach_id": "fix-imports", "description": "Fix imports", "prompt_injection": "fix imports"}, "losing_strategies": [], "timestamp": "2026-01-01T00:00:00Z", "session_id": "s1"}
{"error_class": "test-error", "winning_strategy": {"approach_id": "fix-imports", "description": "Fix imports", "prompt_injection": "fix imports"}, "losing_strategies": [], "timestamp": "2026-01-02T00:00:00Z", "session_id": "s2"}
{"error_class": "test-error", "winning_strategy": {"approach_id": "fix-types", "description": "Fix types", "prompt_injection": "fix types"}, "losing_strategies": [{"approach_id": "fix-imports", "description": "Fix imports", "prompt_injection": "fix imports"}], "timestamp": "2026-01-03T00:00:00Z", "session_id": "s3"}
{"error_class": "test-error", "winning_strategy": {"approach_id": "fix-imports", "description": "Fix imports", "prompt_injection": "fix imports"}, "losing_strategies": [{"approach_id": "fix-types", "description": "Fix types", "prompt_injection": "fix types"}], "timestamp": "2026-01-04T00:00:00Z", "session_id": "s4"}
''')
    
    strategies = load_winning_strategies("test-error", outcomes_path=str(outcomes_file))
    
    # Should return fix-imports first (highest win rate: 3 wins, 1 loss = 75% vs 50%)
    assert len(strategies) > 0
    assert strategies[0].approach_id == "fix-imports"
    assert "win rate" in strategies[0].description
