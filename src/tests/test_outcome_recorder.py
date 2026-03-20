"""Tests for outcome_recorder: log winning strategies by error class.

These tests verify the OutcomeRecorder class handles:
- Recording winning strategy for error class → stored in outcomes.jsonl
- Query past outcomes by error class fingerprint → returns winning strategies
- No prior outcomes → returns empty (system works without history)
- Duplicate error class → appends, keeps full history

TDD: These tests will fail until outcome_recorder.py is implemented.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

# These imports will fail until outcome_recorder.py is implemented (TDD)
from orchestration.outcome_recorder import (
    OutcomeRecorder,
    OutcomeEntry,
    FixStrategy,
)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_state_dir(tmp_path) -> Path:
    """Create a temporary state directory for outcome files."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    return state_dir


@pytest.fixture
def outcomes_file(temp_state_dir: Path) -> Path:
    """Path to the outcomes JSONL file."""
    return temp_state_dir / "outcomes.jsonl"


@pytest.fixture
def recorder(outcomes_file: Path) -> OutcomeRecorder:
    """Create an OutcomeRecorder instance backed by the temp file."""
    return OutcomeRecorder(outcomes_path=outcomes_file)


# Sample FixStrategy objects for testing
@pytest.fixture
def strategy_winner() -> FixStrategy:
    """A winning fix strategy."""
    return FixStrategy(
        approach_id="approach-001",
        description="Fix import order to resolve circular dependency",
        prompt_injection="Try reordering imports to avoid circular dependencies",
    )


@pytest.fixture
def strategy_loser_1() -> FixStrategy:
    """First losing fix strategy."""
    return FixStrategy(
        approach_id="approach-002",
        description="Add type hints to fix typing error",
        prompt_injection="Add type annotations to resolve type errors",
    )


@pytest.fixture
def strategy_loser_2() -> FixStrategy:
    """Second losing fix strategy."""
    return FixStrategy(
        approach_id="approach-003",
        description="Extract function to fix scope issue",
        prompt_injection="Extract logic into separate function",
    )


# ---------------------------------------------------------------------------
# Test: Record winning strategy for error class → stored in outcomes.jsonl
# ---------------------------------------------------------------------------


def test_record_outcome_writes_to_jsonl(
    recorder: OutcomeRecorder,
    outcomes_file: Path,
    strategy_winner: FixStrategy,
    strategy_loser_1: FixStrategy,
) -> None:
    """Recording an outcome should append to outcomes.jsonl."""
    error_class = "ci-failed:import-error"

    recorder.record_outcome(
        error_class=error_class,
        winner=strategy_winner,
        losers=[strategy_loser_1],
    )

    # Verify file exists and contains valid JSONL
    assert outcomes_file.exists()
    with open(outcomes_file) as f:
        lines = f.readlines()
        assert len(lines) == 1

    # Parse and verify the entry
    entry = json.loads(lines[0])
    assert entry["error_class"] == error_class
    assert entry["winning_strategy"]["approach_id"] == "approach-001"
    assert entry["timestamp"] is not None


def test_record_outcome_includes_all_fields(
    recorder: OutcomeRecorder,
    outcomes_file: Path,
    strategy_winner: FixStrategy,
    strategy_loser_1: FixStrategy,
    strategy_loser_2: FixStrategy,
) -> None:
    """Recorded outcome should include error_class, winner, losers, timestamp, session_id."""
    error_class = "ci-failed:syntax-error"
    session_id = "session-12345"

    recorder.record_outcome(
        error_class=error_class,
        winner=strategy_winner,
        losers=[strategy_loser_1, strategy_loser_2],
        session_id=session_id,
    )

    with open(outcomes_file) as f:
        entry = json.loads(f.readline())

    assert entry["error_class"] == error_class
    assert entry["winning_strategy"]["approach_id"] == "approach-001"
    assert entry["winning_strategy"]["description"] == strategy_winner.description
    assert len(entry["losing_strategies"]) == 2
    assert entry["session_id"] == session_id
    assert "timestamp" in entry


# ---------------------------------------------------------------------------
# Test: Query past outcomes by error class fingerprint → returns winning strategies
# ---------------------------------------------------------------------------


def test_query_outcomes_returns_matching_entries(
    recorder: OutcomeRecorder,
    strategy_winner: FixStrategy,
    strategy_loser_1: FixStrategy,
) -> None:
    """Querying by error class should return all matching outcome entries."""
    error_class = "ci-failed:import-error"

    # Record first outcome
    recorder.record_outcome(
        error_class=error_class,
        winner=strategy_winner,
        losers=[strategy_loser_1],
    )

    # Query and verify
    results = recorder.query_outcomes(error_class)

    assert len(results) == 1
    assert results[0].error_class == error_class
    assert results[0].winning_strategy.approach_id == "approach-001"


def test_query_outcomes_returns_empty_for_unknown_class(
    recorder: OutcomeRecorder,
) -> None:
    """Querying unknown error class should return empty list."""
    # No outcomes recorded yet
    results = recorder.query_outcomes("unknown-error-class")
    assert results == []


def test_query_outcomes_returns_multiple_entries(
    recorder: OutcomeRecorder,
    strategy_winner: FixStrategy,
    strategy_loser_1: FixStrategy,
) -> None:
    """Multiple outcomes for same error class should all be returned."""
    error_class = "ci-failed:import-error"

    # Record multiple outcomes for the same error class
    winner1 = FixStrategy(
        approach_id="approach-a",
        description="First approach that worked",
        prompt_injection="Try approach A",
    )
    winner2 = FixStrategy(
        approach_id="approach-b",
        description="Second approach that worked later",
        prompt_injection="Try approach B",
    )

    recorder.record_outcome(error_class, winner1, [strategy_loser_1])
    recorder.record_outcome(error_class, winner2, [strategy_loser_1])

    results = recorder.query_outcomes(error_class)
    assert len(results) == 2
    # Most recent should be first (by timestamp descending)
    assert results[0].winning_strategy.approach_id == "approach-b"
    assert results[1].winning_strategy.approach_id == "approach-a"


# ---------------------------------------------------------------------------
# Test: No prior outcomes → returns empty (system works without history)
# ---------------------------------------------------------------------------


def test_works_without_prior_file(
    outcomes_file: Path,
    strategy_winner: FixStrategy,
) -> None:
    """Recorder should work even if file doesn't exist yet."""
    assert not outcomes_file.exists()

    recorder = OutcomeRecorder(outcomes_path=outcomes_file)

    # Query should return empty, not error
    results = recorder.query_outcomes("any-error")
    assert results == []

    # Recording should create the file
    recorder.record_outcome(
        error_class="test-error",
        winner=strategy_winner,
        losers=[],
    )

    assert outcomes_file.exists()


def test_query_empty_file_returns_empty(
    recorder: OutcomeRecorder,
    outcomes_file: Path,
) -> None:
    """Querying an empty file should return empty list."""
    # Create empty file
    outcomes_file.touch()

    results = recorder.query_outcomes("any-error")
    assert results == []


# ---------------------------------------------------------------------------
# Test: Duplicate error class → appends, keeps full history
# ---------------------------------------------------------------------------


def test_duplicate_error_class_appends(
    recorder: OutcomeRecorder,
    outcomes_file: Path,
    strategy_winner: FixStrategy,
    strategy_loser_1: FixStrategy,
) -> None:
    """Recording outcome for same error class should append, not overwrite."""
    error_class = "ci-failed:import-error"

    # Record first outcome
    recorder.record_outcome(
        error_class=error_class,
        winner=strategy_winner,
        losers=[strategy_loser_1],
    )

    # Record second outcome for same error class
    winner2 = FixStrategy(
        approach_id="different-approach",
        description="Different winning strategy",
        prompt_injection="Different approach",
    )
    recorder.record_outcome(
        error_class=error_class,
        winner=winner2,
        losers=[strategy_loser_1],
    )

    # Should have 2 lines in file
    with open(outcomes_file) as f:
        lines = f.readlines()
    assert len(lines) == 2

    # Both should be queryable
    results = recorder.query_outcomes(error_class)
    assert len(results) == 2


def test_full_history_preserved(
    recorder: OutcomeRecorder,
    outcomes_file: Path,
    strategy_winner: FixStrategy,
) -> None:
    """All historical outcomes should be preserved, not truncated."""
    error_class = "ci-failed:import-error"

    # Record many outcomes
    for i in range(10):
        winner = FixStrategy(
            approach_id=f"approach-{i:03d}",
            description=f"Strategy {i}",
            prompt_injection=f"Try approach {i}",
        )
        recorder.record_outcome(error_class, winner, [])

    # File should have all 10 entries
    with open(outcomes_file) as f:
        lines = f.readlines()
    assert len(lines) == 10

    # Query should return all 10
    results = recorder.query_outcomes(error_class)
    assert len(results) == 10


# ---------------------------------------------------------------------------
# Test: Edge cases
# ---------------------------------------------------------------------------


def test_record_outcome_with_no_losers(
    recorder: OutcomeRecorder,
    outcomes_file: Path,
    strategy_winner: FixStrategy,
) -> None:
    """Recording outcome with empty losers list should work."""
    error_class = "ci-failed:single-attempt-success"

    recorder.record_outcome(
        error_class=error_class,
        winner=strategy_winner,
        losers=[],  # Empty list
    )

    with open(outcomes_file) as f:
        entry = json.loads(f.readline())

    assert entry["losing_strategies"] == []


def test_query_outcomes_exact_match_required(
    recorder: OutcomeRecorder,
    strategy_winner: FixStrategy,
    strategy_loser_1: FixStrategy,
) -> None:
    """Query should require exact error class match, not substring."""
    # Record with specific error class
    recorder.record_outcome(
        error_class="ci-failed:import-error",
        winner=strategy_winner,
        losers=[strategy_loser_1],
    )

    # Query with different (even if related) error class
    results = recorder.query_outcomes("ci-failed:different-error")
    assert results == []

    results = recorder.query_outcomes("import-error")
    assert results == []


def test_timestamp_is_iso_format(
    recorder: OutcomeRecorder,
    outcomes_file: Path,
    strategy_winner: FixStrategy,
) -> None:
    """Timestamp should be in ISO format for easy parsing."""
    recorder.record_outcome(
        error_class="test",
        winner=strategy_winner,
        losers=[],
    )

    with open(outcomes_file) as f:
        entry = json.loads(f.readline())

    # Should be parseable as ISO datetime
    parsed = datetime.fromisoformat(entry["timestamp"])
    assert parsed.tzinfo is not None  # Should be timezone-aware


def test_outcome_entry_dataclass_fields(
    strategy_winner: FixStrategy,
) -> None:
    """OutcomeEntry should have all required fields."""
    now = datetime.now(timezone.utc)
    entry = OutcomeEntry(
        error_class="test-error",
        winning_strategy=strategy_winner,
        losing_strategies=[],
        timestamp=now.isoformat(),
        session_id="session-001",
    )

    assert entry.error_class == "test-error"
    assert entry.winning_strategy == strategy_winner
    assert entry.losing_strategies == []
    assert entry.timestamp == now.isoformat()
    assert entry.session_id == "session-001"