"""Outcome recorder: log winning strategies by error class.

This module implements Phase 3.5d of the orchestration roadmap:
- Records the winning fix strategy for each error class to an append-only JSONL file
- Queries past outcomes by error class fingerprint
- Enables ORCH-cil: future versions skip speculation when a known fix exists

Storage: ~/.openclaw/state/outcomes.jsonl (append-only JSONL)
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Union

if TYPE_CHECKING:
    from orchestration.parallel_retry import FixStrategy


# Default paths
DEFAULT_STATE_DIR = "~/.openclaw/state"
DEFAULT_OUTCOMES_FILE = "outcomes.jsonl"


# ---------------------------------------------------------------------------
# Data Structures
# ---------------------------------------------------------------------------


# Type alias for FixStrategy (handles both import and inline definition)
FixStrategyType = Union["FixStrategy", None]


# For backwards compatibility and runtime use, we'll define a minimal FixStrategy-like
# dataclass that can be used when parallel_retry is not available
@dataclass
class FixStrategy:
    """A single fix strategy for a CI failure.

    This is a local definition to avoid circular imports with parallel_retry.py.
    The actual FixStrategy from parallel_retry.py has the same fields.
    """

    approach_id: str
    description: str
    prompt_injection: str


@dataclass
class OutcomeEntry:
    """A recorded outcome: which strategy won for a given error class.

    Attributes:
        error_class: Fingerprint/class of the error (e.g., "ci-failed:import-error")
        winning_strategy: The FixStrategy that succeeded
        losing_strategies: List of FixStrategy that failed
        timestamp: ISO-formatted timestamp (timezone-aware)
        session_id: Optional session identifier for traceability
    """

    error_class: str
    winning_strategy: FixStrategy
    losing_strategies: list[FixStrategy]
    timestamp: str
    session_id: str


# ---------------------------------------------------------------------------
# Outcome Recorder
# ---------------------------------------------------------------------------


class OutcomeRecorder:
    """Records and queries fix strategy outcomes by error class.

    Uses append-only JSONL storage to preserve full history.
    Enables the orchestration system to learn from past successful fixes.

    Example:
        recorder = OutcomeRecorder()
        recorder.record_outcome(
            error_class="ci-failed:import-error",
            winner=FixStrategy(...),
            losers=[FixStrategy(...), ...],
        )
        results = recorder.query_outcomes("ci-failed:import-error")
    """

    def __init__(self, outcomes_path: Path | str | None = None) -> None:
        """Initialize the outcome recorder.

        Args:
            outcomes_path: Path to the outcomes JSONL file. If None, uses
                ~/.openclaw/state/outcomes.jsonl
        """
        if outcomes_path is None:
            state_dir = os.path.expanduser(DEFAULT_STATE_DIR)
            os.makedirs(state_dir, exist_ok=True)
            outcomes_path = os.path.join(state_dir, DEFAULT_OUTCOMES_FILE)

        self._outcomes_path = Path(outcomes_path)

    def record_outcome(
        self,
        error_class: str,
        winner: FixStrategy,
        losers: list[FixStrategy],
        session_id: str | None = None,
    ) -> None:
        """Record a winning strategy for an error class.

        Appends a new entry to the outcomes JSONL file.

        Args:
            error_class: Fingerprint/class of the error
            winner: The FixStrategy that succeeded
            losers: List of FixStrategy that failed
            session_id: Optional session identifier (auto-generated if not provided)
        """
        if session_id is None:
            session_id = f"session-{uuid.uuid4().hex[:8]}"

        timestamp = datetime.now(timezone.utc).isoformat()

        entry = {
            "error_class": error_class,
            "winning_strategy": {
                "approach_id": winner.approach_id,
                "description": winner.description,
                "prompt_injection": winner.prompt_injection,
            },
            "losing_strategies": [
                {
                    "approach_id": s.approach_id,
                    "description": s.description,
                    "prompt_injection": s.prompt_injection,
                }
                for s in losers
            ],
            "timestamp": timestamp,
            "session_id": session_id,
        }

        # Ensure parent directory exists
        self._outcomes_path.parent.mkdir(parents=True, exist_ok=True)

        # Append to JSONL file
        with open(self._outcomes_path, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def query_outcomes(self, error_class: str) -> list[OutcomeEntry]:
        """Query past outcomes for a specific error class.

        Returns outcomes sorted by timestamp descending (most recent first).

        Args:
            error_class: The error class fingerprint to query

        Returns:
            List of OutcomeEntry objects for matching error class
        """
        if not self._outcomes_path.exists():
            return []

        results: list[OutcomeEntry] = []

        try:
            with open(self._outcomes_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue

                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        # Skip malformed lines
                        continue

                    if entry.get("error_class") == error_class:
                        # Reconstruct FixStrategy objects
                        winner_data = entry["winning_strategy"]
                        winner = FixStrategy(
                            approach_id=winner_data["approach_id"],
                            description=winner_data["description"],
                            prompt_injection=winner_data["prompt_injection"],
                        )

                        losers = []
                        for loser_data in entry.get("losing_strategies", []):
                            losers.append(
                                FixStrategy(
                                    approach_id=loser_data["approach_id"],
                                    description=loser_data["description"],
                                    prompt_injection=loser_data["prompt_injection"],
                                )
                            )

                        results.append(
                            OutcomeEntry(
                                error_class=entry["error_class"],
                                winning_strategy=winner,
                                losing_strategies=losers,
                                timestamp=entry["timestamp"],
                                session_id=entry.get("session_id", ""),
                            )
                        )
        except (FileNotFoundError, IOError):
            # File doesn't exist or can't be read - return empty
            return []

        # Sort by timestamp descending (most recent first)
        results.sort(key=lambda x: x.timestamp, reverse=True)

        return results
