"""Tests for pattern_synthesizer: outcome ledger pattern synthesis.

These tests verify the PatternSynthesizer class handles:
- Empty outcomes.jsonl → empty patterns
- Single error_class with multiple outcomes → correct win rate calculation
- Win rate below threshold → excluded from patterns
- Multiple error_classes → separate patterns each

TDD: These tests will fail until pattern_synthesizer.py is implemented.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# These imports will fail until pattern_synthesizer.py is implemented (TDD)
from orchestration.pattern_synthesizer import (
    PatternSynthesizer,
    SynthesizedPattern,
    StrategyOutcome,
)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_state_dir(tmp_path) -> Path:
    """Create a temporary state directory for test files."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    return state_dir


@pytest.fixture
def outcomes_file(temp_state_dir: Path) -> Path:
    """Path to the outcomes JSONL file."""
    return temp_state_dir / "outcomes.jsonl"


@pytest.fixture
def patterns_file(temp_state_dir: Path) -> Path:
    """Path to the patterns JSON file."""
    return temp_state_dir / "patterns.json"


@pytest.fixture
def synthesizer(outcomes_file: Path, patterns_file: Path) -> PatternSynthesizer:
    """Create a PatternSynthesizer instance backed by temp files."""
    return PatternSynthesizer(
        outcomes_path=outcomes_file,
        patterns_path=patterns_file,
    )


# ---------------------------------------------------------------------------
# Test: Empty outcomes.jsonl → empty patterns
# ---------------------------------------------------------------------------


def test_empty_outcomes_returns_empty_patterns(synthesizer: PatternSynthesizer) -> None:
    """Empty outcomes file should produce no patterns."""
    patterns = synthesizer.synthesize()
    assert patterns == []


def test_nonexistent_outcomes_file_returns_empty_patterns(synthesizer: Path) -> None:
    """Nonexistent outcomes file should produce no patterns."""
    # Use a synthesizer with non-existent paths
    synth = PatternSynthesizer(
        outcomes_path="/nonexistent/outcomes.jsonl",
        patterns_path="/nonexistent/patterns.jsonl",
    )
    patterns = synth.synthesize()
    assert patterns == []


# ---------------------------------------------------------------------------
# Test: Single error_class with multiple outcomes → correct win rate calculation
# ---------------------------------------------------------------------------


def test_single_error_class_win_rate(
    synthesizer: PatternSynthesizer,
    outcomes_file: Path,
) -> None:
    """Multiple outcomes for same error class should calculate correct win rate."""
    # Record 5 outcomes: approach-001 wins 3 times, approach-002 wins 2 times
    now = datetime.now(timezone.utc)

    outcomes = [
        {
            "error_class": "ci-failed:import-error",
            "winning_strategy": {"approach_id": "approach-001", "description": "fix 1", "prompt_injection": "do 1"},
            "losing_strategies": [],
            "timestamp": (now - timedelta(hours=i)).isoformat(),
            "session_id": f"session-{i}",
        }
        for i in range(3)
    ] + [
        {
            "error_class": "ci-failed:import-error",
            "winning_strategy": {"approach_id": "approach-002", "description": "fix 2", "prompt_injection": "do 2"},
            "losing_strategies": [],
            "timestamp": (now - timedelta(hours=i + 3)).isoformat(),
            "session_id": f"session-{i + 3}",
        }
        for i in range(2)
    ]

    with open(outcomes_file, "w") as f:
        for outcome in outcomes:
            f.write(json.dumps(outcome) + "\n")

    # Synthesize with 50% threshold
    patterns = synthesizer.synthesize(min_confidence=0.5)

    assert len(patterns) == 1
    pattern = patterns[0]
    assert pattern.error_class == "ci-failed:import-error"
    assert pattern.total_attempts == 5
    # approach-001 has 3/5 = 0.6 win rate
    assert pattern.win_rate == 0.6
    assert pattern.winning_strategy == "approach-001"


def test_win_rate_below_threshold_excluded(
    synthesizer: PatternSynthesizer,
    outcomes_file: Path,
) -> None:
    """Win rate below threshold should be excluded from patterns."""
    now = datetime.now(timezone.utc)

    # 3 outcomes: approach-001 wins 1 time (33%), approach-002 wins 2 times (67%)
    outcomes = [
        {
            "error_class": "ci-failed:type-error",
            "winning_strategy": {"approach_id": "approach-001", "description": "fix 1", "prompt_injection": "do 1"},
            "losing_strategies": [],
            "timestamp": (now - timedelta(hours=i)).isoformat(),
            "session_id": f"session-{i}",
        }
        for i in range(1)
    ] + [
        {
            "error_class": "ci-failed:type-error",
            "winning_strategy": {"approach_id": "approach-002", "description": "fix 2", "prompt_injection": "do 2"},
            "losing_strategies": [],
            "timestamp": (now - timedelta(hours=i + 1)).isoformat(),
            "session_id": f"session-{i + 1}",
        }
        for i in range(2)
    ]

    with open(outcomes_file, "w") as f:
        for outcome in outcomes:
            f.write(json.dumps(outcome) + "\n")

    # Synthesize with 70% threshold - should exclude because best win rate is 67%
    patterns = synthesizer.synthesize(min_confidence=0.7)

    assert len(patterns) == 0


# ---------------------------------------------------------------------------
# Test: Multiple error_classes → separate patterns each
# ---------------------------------------------------------------------------


def test_multiple_error_classes(
    synthesizer: PatternSynthesizer,
    outcomes_file: Path,
) -> None:
    """Multiple error classes should produce separate patterns."""
    now = datetime.now(timezone.utc)

    # Import error: approach-001 wins 2 times
    import_outcomes = [
        {
            "error_class": "ci-failed:import-error",
            "winning_strategy": {"approach_id": "approach-001", "description": "fix import", "prompt_injection": "fix import"},
            "losing_strategies": [],
            "timestamp": (now - timedelta(hours=i)).isoformat(),
            "session_id": f"session-import-{i}",
        }
        for i in range(2)
    ]

    # Syntax error: approach-002 wins 3 times
    syntax_outcomes = [
        {
            "error_class": "ci-failed:syntaxerror",
            "winning_strategy": {"approach_id": "approach-002", "description": "fix syntax", "prompt_injection": "fix syntax"},
            "losing_strategies": [],
            "timestamp": (now - timedelta(hours=i + 10)).isoformat(),
            "session_id": f"session-syntax-{i}",
        }
        for i in range(3)
    ]

    with open(outcomes_file, "w") as f:
        for outcome in import_outcomes + syntax_outcomes:
            f.write(json.dumps(outcome) + "\n")

    patterns = synthesizer.synthesize(min_confidence=0.5)

    assert len(patterns) == 2

    # Should have patterns for both error classes
    error_classes = {p.error_class for p in patterns}
    assert "ci-failed:import-error" in error_classes
    assert "ci-failed:syntaxerror" in error_classes


# ---------------------------------------------------------------------------
# Test: Lookback period filtering
# ---------------------------------------------------------------------------


def test_lookback_days_filters_old_outcomes(
    synthesizer: PatternSynthesizer,
    outcomes_file: Path,
) -> None:
    """Outcomes older than lookback_days should be excluded."""
    now = datetime.now(timezone.utc)

    # Recent outcome (within lookback)
    recent_outcome = {
        "error_class": "ci-failed:import-error",
        "winning_strategy": {"approach_id": "approach-001", "description": "fix", "prompt_injection": "fix"},
        "losing_strategies": [],
        "timestamp": now.isoformat(),
        "session_id": "session-recent",
    }

    # Old outcome (outside lookback - 60 days ago)
    old_outcome = {
        "error_class": "ci-failed:import-error",
        "winning_strategy": {"approach_id": "approach-002", "description": "old fix", "prompt_injection": "old fix"},
        "losing_strategies": [],
        "timestamp": (now - timedelta(days=60)).isoformat(),
        "session_id": "session-old",
    }

    with open(outcomes_file, "w") as f:
        f.write(json.dumps(recent_outcome) + "\n")
        f.write(json.dumps(old_outcome) + "\n")

    # Default lookback is 30 days
    patterns = synthesizer.synthesize(lookback_days=30)

    # Should only include recent outcome
    assert len(patterns) == 1
    assert patterns[0].winning_strategy == "approach-001"


# ---------------------------------------------------------------------------
# Test: Save and load patterns
# ---------------------------------------------------------------------------


def test_save_and_load_patterns(
    synthesizer: PatternSynthesizer,
    outcomes_file: Path,
    patterns_file: Path,
) -> None:
    """Saved patterns should be loadable."""
    now = datetime.now(timezone.utc)

    outcomes = [
        {
            "error_class": "ci-failed:import-error",
            "winning_strategy": {"approach_id": "approach-001", "description": "fix", "prompt_injection": "fix"},
            "losing_strategies": [],
            "timestamp": now.isoformat(),
            "session_id": "session-1",
        }
    ]

    with open(outcomes_file, "w") as f:
        for outcome in outcomes:
            f.write(json.dumps(outcome) + "\n")

    # Synthesize and save
    patterns = synthesizer.synthesize()
    synthesizer.save_patterns(patterns)

    # Verify file was created
    assert patterns_file.exists()

    # Load patterns using a new synthesizer
    new_synthesizer = PatternSynthesizer(
        outcomes_path=outcomes_file,
        patterns_path=patterns_file,
    )
    loaded = new_synthesizer.load_patterns()

    assert len(loaded) == 1
    assert loaded[0].error_class == "ci-failed:import-error"
    assert loaded[0].winning_strategy == "approach-001"


def test_load_patterns_nonexistent_file(
    temp_state_dir: Path,
) -> None:
    """Loading from nonexistent file should return empty list."""
    synth = PatternSynthesizer(
        outcomes_path=temp_state_dir / "nonexistent.jsonl",
        patterns_path=temp_state_dir / "patterns.json",
    )
    patterns = synth.load_patterns()
    assert patterns == []


# ---------------------------------------------------------------------------
# Test: get_pattern_for_error
# ---------------------------------------------------------------------------


def test_get_pattern_for_error(
    synthesizer: PatternSynthesizer,
    outcomes_file: Path,
    patterns_file: Path,
) -> None:
    """get_pattern_for_error should return the matching pattern."""
    now = datetime.now(timezone.utc)

    outcomes = [
        {
            "error_class": "ci-failed:import-error",
            "winning_strategy": {"approach_id": "approach-001", "description": "fix import", "prompt_injection": "fix"},
            "losing_strategies": [],
            "timestamp": now.isoformat(),
            "session_id": "session-1",
        },
        {
            "error_class": "ci-failed:type-error",
            "winning_strategy": {"approach_id": "approach-002", "description": "fix type", "prompt_injection": "fix"},
            "losing_strategies": [],
            "timestamp": now.isoformat(),
            "session_id": "session-2",
        },
    ]

    with open(outcomes_file, "w") as f:
        for outcome in outcomes:
            f.write(json.dumps(outcome) + "\n")

    # Synthesize and save
    patterns = synthesizer.synthesize()
    synthesizer.save_patterns(patterns)

    # Get pattern for import-error
    pattern = synthesizer.get_pattern_for_error("ci-failed:import-error")
    assert pattern is not None
    assert pattern.winning_strategy == "approach-001"

    # Get pattern for type-error
    pattern = synthesizer.get_pattern_for_error("ci-failed:type-error")
    assert pattern is not None
    assert pattern.winning_strategy == "approach-002"

    # Get pattern for unknown error
    pattern = synthesizer.get_pattern_for_error("ci-failed:unknown")
    assert pattern is None


# ---------------------------------------------------------------------------
# Test: Edge cases
# ---------------------------------------------------------------------------


def test_malformed_json_in_outcomes(
    synthesizer: PatternSynthesizer,
    outcomes_file: Path,
) -> None:
    """Malformed JSON lines should be skipped."""
    now = datetime.now(timezone.utc)

    # Write valid and invalid lines
    with open(outcomes_file, "w") as f:
        f.write(json.dumps({
            "error_class": "ci-failed:import-error",
            "winning_strategy": {"approach_id": "approach-001", "description": "fix", "prompt_injection": "fix"},
            "losing_strategies": [],
            "timestamp": now.isoformat(),
            "session_id": "session-1",
        }) + "\n")
        f.write("not valid json\n")
        f.write(json.dumps({
            "error_class": "ci-failed:import-error",
            "winning_strategy": {"approach_id": "approach-002", "description": "fix", "prompt_injection": "fix"},
            "losing_strategies": [],
            "timestamp": now.isoformat(),
            "session_id": "session-2",
        }) + "\n")

    # Should still process valid entries
    patterns = synthesizer.synthesize()

    # Both strategies should be counted
    assert len(patterns) == 1


def test_empty_error_class_or_strategy_skipped(
    synthesizer: PatternSynthesizer,
    outcomes_file: Path,
) -> None:
    """Entries with empty error_class or winning_strategy should be skipped."""
    now = datetime.now(timezone.utc)

    with open(outcomes_file, "w") as f:
        # Valid entry
        f.write(json.dumps({
            "error_class": "ci-failed:import-error",
            "winning_strategy": {"approach_id": "approach-001", "description": "fix", "prompt_injection": "fix"},
            "losing_strategies": [],
            "timestamp": now.isoformat(),
            "session_id": "session-1",
        }) + "\n")
        # Empty error_class
        f.write(json.dumps({
            "error_class": "",
            "winning_strategy": {"approach_id": "approach-002", "description": "fix", "prompt_injection": "fix"},
            "losing_strategies": [],
            "timestamp": now.isoformat(),
            "session_id": "session-2",
        }) + "\n")

    patterns = synthesizer.synthesize()

    # Only valid entry should be included
    assert len(patterns) == 1
    assert patterns[0].winning_strategy == "approach-001"


def test_patterns_sorted_by_win_rate(
    synthesizer: PatternSynthesizer,
    outcomes_file: Path,
) -> None:
    """Patterns should be sorted by win_rate descending, then by total_attempts."""
    now = datetime.now(timezone.utc)

    # Import error: approach-001 wins 2/2 = 100%
    import_outcomes = [
        {
            "error_class": "ci-failed:import-error",
            "winning_strategy": {"approach_id": "approach-001", "description": "fix", "prompt_injection": "fix"},
            "losing_strategies": [],
            "timestamp": (now - timedelta(hours=i)).isoformat(),
            "session_id": f"session-import-{i}",
        }
        for i in range(2)
    ]

    # Syntax error: approach-002 wins 3/3 = 100%
    syntax_outcomes = [
        {
            "error_class": "ci-failed:syntaxerror",
            "winning_strategy": {"approach_id": "approach-002", "description": "fix", "prompt_injection": "fix"},
            "losing_strategies": [],
            "timestamp": (now - timedelta(hours=i + 10)).isoformat(),
            "session_id": f"session-syntax-{i}",
        }
        for i in range(3)
    ]

    with open(outcomes_file, "w") as f:
        for outcome in import_outcomes + syntax_outcomes:
            f.write(json.dumps(outcome) + "\n")

    patterns = synthesizer.synthesize(min_confidence=0.5)

    # Both have 100% win rate, but syntax has more attempts (3 > 2)
    assert len(patterns) == 2
    assert patterns[0].error_class == "ci-failed:syntaxerror"  # More attempts first
    assert patterns[1].error_class == "ci-failed:import-error"
