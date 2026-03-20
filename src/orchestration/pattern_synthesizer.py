"""Pattern synthesizer — cron job for outcome ledger pattern synthesis.

Analyzes outcomes.jsonl to extract winning strategies per error class.
Outputs patterns.json for use by generate_fix_strategies.

This module implements ORCH-qvd: Outcome ledger pattern synthesis (self-improving prompts).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Default paths
DEFAULT_STATE_DIR = "~/.openclaw/state"
DEFAULT_OUTCOMES_FILE = "outcomes.jsonl"
DEFAULT_PATTERNS_FILE = "patterns.json"


# ---------------------------------------------------------------------------
# Data Structures
# ---------------------------------------------------------------------------


@dataclass
class StrategyOutcome:
    """A single outcome entry with winning strategy info."""

    error_class: str
    winning_strategy: str  # approach_id
    timestamp: str


@dataclass
class SynthesizedPattern:
    """A synthesized pattern: winning strategy for an error class with confidence metrics."""

    error_class: str
    winning_strategy: str  # approach_id
    win_rate: float
    total_attempts: int
    last_seen: str

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "error_class": self.error_class,
            "winning_strategy": self.winning_strategy,
            "win_rate": self.win_rate,
            "total_attempts": self.total_attempts,
            "last_seen": self.last_seen,
        }


# ---------------------------------------------------------------------------
# Pattern Synthesizer
# ---------------------------------------------------------------------------


class PatternSynthesizer:
    """Analyzes outcomes and synthesizes winning strategy patterns.

    Reads from outcomes.jsonl, groups by error_class, calculates win rates,
    and outputs high-confidence patterns to patterns.json.

    Example:
        synthesizer = PatternSynthesizer()
        patterns = synthesizer.synthesize(min_confidence=0.5, lookback_days=30)
        synthesizer.save_patterns(patterns)
    """

    def __init__(
        self,
        outcomes_path: Path | str | None = None,
        patterns_path: Path | str | None = None,
    ) -> None:
        """Initialize the pattern synthesizer.

        Args:
            outcomes_path: Path to outcomes.jsonl. If None, uses ~/.openclaw/state/outcomes.jsonl
            patterns_path: Path to patterns.json output. If None, uses ~/.openclaw/state/patterns.json
        """
        if outcomes_path is None:
            state_dir = os.path.expanduser(DEFAULT_STATE_DIR)
            outcomes_path = os.path.join(state_dir, DEFAULT_OUTCOMES_FILE)
        self._outcomes_path = Path(outcomes_path)

        if patterns_path is None:
            state_dir = os.path.expanduser(DEFAULT_STATE_DIR)
            patterns_path = os.path.join(state_dir, DEFAULT_PATTERNS_FILE)
        self._patterns_path = Path(patterns_path)

    def read_outcomes(self) -> list[StrategyOutcome]:
        """Read all outcomes from the JSONL file.

        Returns:
            List of StrategyOutcome objects
        """
        if not self._outcomes_path.exists():
            logger.debug(f"Outcomes file does not exist: {self._outcomes_path}")
            return []

        outcomes: list[StrategyOutcome] = []

        try:
            with open(self._outcomes_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue

                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        logger.warning(f"Skipping malformed JSON line in outcomes")
                        continue

                    outcomes.append(
                        StrategyOutcome(
                            error_class=entry.get("error_class", ""),
                            winning_strategy=entry.get("winning_strategy", {}).get("approach_id", ""),
                            timestamp=entry.get("timestamp", ""),
                        )
                    )
        except (FileNotFoundError, IOError) as e:
            logger.warning(f"Failed to read outcomes file: {e}")

        return outcomes

    def synthesize(
        self,
        min_confidence: float = 0.5,
        lookback_days: int = 30,
    ) -> list[SynthesizedPattern]:
        """Analyze outcomes and synthesize winning strategy patterns.

        Args:
            min_confidence: Minimum win rate to include (default 0.5)
            lookback_days: Only consider outcomes from last N days

        Returns:
            List of SynthesizedPattern with high-confidence winning strategies
        """
        outcomes = self.read_outcomes()

        if not outcomes:
            return []

        # Filter to lookback period
        cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
        recent_outcomes = []

        for outcome in outcomes:
            try:
                timestamp = datetime.fromisoformat(outcome.timestamp.replace("Z", "+00:00"))
                if timestamp >= cutoff:
                    recent_outcomes.append(outcome)
            except (ValueError, TypeError):
                # If timestamp is invalid, include it (conservative)
                recent_outcomes.append(outcome)

        if not recent_outcomes:
            return []

        # Group by error_class
        by_error_class: dict[str, list[StrategyOutcome]] = defaultdict(list)
        for outcome in recent_outcomes:
            if outcome.error_class and outcome.winning_strategy:
                by_error_class[outcome.error_class].append(outcome)

        # Calculate win rates per strategy for each error class
        patterns: list[SynthesizedPattern] = []

        for error_class, class_outcomes in by_error_class.items():
            # Count wins per strategy
            strategy_wins: dict[str, int] = defaultdict(int)
            for outcome in class_outcomes:
                strategy_wins[outcome.winning_strategy] += 1

            total = len(class_outcomes)

            # Find the winning strategy (most wins)
            if not strategy_wins:
                continue

            winning_strategy = max(strategy_wins.items(), key=lambda x: x[1])
            wins = winning_strategy[1]
            win_rate = wins / total

            # Filter by confidence threshold
            if win_rate < min_confidence:
                continue

            # Get last_seen (most recent timestamp)
            timestamps = [o.timestamp for o in class_outcomes if o.timestamp]
            last_seen = max(timestamps) if timestamps else ""

            patterns.append(
                SynthesizedPattern(
                    error_class=error_class,
                    winning_strategy=winning_strategy[0],
                    win_rate=round(win_rate, 3),
                    total_attempts=total,
                    last_seen=last_seen,
                )
            )

        # Sort by win_rate descending, then by total_attempts descending
        patterns.sort(key=lambda p: (-p.win_rate, -p.total_attempts))

        return patterns

    def save_patterns(self, patterns: list[SynthesizedPattern]) -> None:
        """Save synthesized patterns to patterns.json atomically.

        Uses atomic write pattern: write to temp file then rename to target.
        This prevents readers from observing truncated JSON mid-write.

        Args:
            patterns: List of SynthesizedPattern objects
        """
        # Ensure parent directory exists
        self._patterns_path.parent.mkdir(parents=True, exist_ok=True)

        output = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "patterns": [p.to_dict() for p in patterns],
        }

        # Atomic write: write to temp file then rename
        dir_path = self._patterns_path.parent
        temp_fd, temp_path = tempfile.mkstemp(
            dir=dir_path, prefix=".ps_", suffix=".tmp"
        )
        try:
            with os.fdopen(temp_fd, "w") as f:
                json.dump(output, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, self._patterns_path)
        except Exception:
            if os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
            raise

        logger.info(f"Saved {len(patterns)} patterns to {self._patterns_path}")

    def load_patterns(self) -> list[SynthesizedPattern]:
        """Load previously synthesized patterns.

        Returns:
            List of SynthesizedPattern objects, or empty list if file doesn't exist
        """
        if not self._patterns_path.exists():
            return []

        try:
            with open(self._patterns_path) as f:
                data = json.load(f)

            patterns = []
            for p in data.get("patterns", []):
                patterns.append(
                    SynthesizedPattern(
                        error_class=p["error_class"],
                        winning_strategy=p["winning_strategy"],
                        win_rate=p["win_rate"],
                        total_attempts=p["total_attempts"],
                        last_seen=p["last_seen"],
                    )
                )
            return patterns
        except (FileNotFoundError, json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Failed to load patterns: {e}")
            return []

    def get_pattern_for_error(self, error_class: str) -> SynthesizedPattern | None:
        """Get the winning pattern for a specific error class.

        Args:
            error_class: The error class to look up

        Returns:
            SynthesizedPattern if found, None otherwise
        """
        patterns = self.load_patterns()
        for pattern in patterns:
            if pattern.error_class == error_class:
                return pattern
        return None


# ---------------------------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------------------------


def run_synthesis(
    min_confidence: float = 0.5,
    lookback_days: int = 30,
) -> int:
    """Run pattern synthesis and save to patterns.json.

    Args:
        min_confidence: Minimum win rate to include
        lookback_days: Only consider outcomes from last N days

    Returns:
        Number of patterns synthesized
    """
    synthesizer = PatternSynthesizer()
    patterns = synthesizer.synthesize(min_confidence=min_confidence, lookback_days=lookback_days)
    synthesizer.save_patterns(patterns)
    return len(patterns)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    count = run_synthesis()
    print(f"Synthesized {count} patterns")
