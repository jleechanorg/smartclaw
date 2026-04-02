#!/usr/bin/env python3
"""Outcome Ledger - logs all results for analysis."""
from __future__ import annotations

import json
from pathlib import Path
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Optional


@dataclass
class StressTestResult:
    run_id: str
    timestamp: str
    slice: str
    lines_reviewed: int
    test_pr: int
    original_pr: Optional[int]
    ai_reviewers_responded: list[str]
    total_comments: int
    comments_by_severity: dict
    agento_attempts: int
    fix_success: bool
    time_to_green_minutes: int
    fixes_applied_to_original: bool
    error: Optional[str] = None


class OutcomeLedger:
    """Logs stress test results to JSONL."""

    def __init__(self, ledger_path: str | None = None) -> None:
        if ledger_path:
            self.ledger_path = Path(ledger_path)
        else:
            self.ledger_path = Path.home() / ".ai_review_stress_ledger.jsonl"
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, result: StressTestResult) -> None:
        """Append to JSONL ledger."""
        with open(self.ledger_path, "a") as f:
            f.write(json.dumps(asdict(result)) + "\n")
        print(f"Logged result to {self.ledger_path}")

    def get_recent(self, n: int = 10) -> list[StressTestResult]:
        """Get recent results."""
        results: list[StressTestResult] = []
        if not self.ledger_path.exists():
            return results

        with open(self.ledger_path) as f:
            for line in f:
                if line.strip():
                    try:
                        results.append(StressTestResult(**json.loads(line)))
                    except (json.JSONDecodeError, TypeError) as e:
                        print(f"  Warning: skipping malformed ledger line: {e}")
                        continue

        return results[-n:]

    def weekly_summary(self) -> dict:
        """Summarize recent runs."""
        recent = self.get_recent(n=50)

        if not recent:
            return {"error": "No results yet"}

        total = len(recent)
        successful = sum(1 for r in recent if r.fix_success)

        return {
            "total_runs": total,
            "success_rate": round(successful / total * 100, 1) if total else 0,
            "avg_time_to_green": sum(r.time_to_green_minutes for r in recent) / total if total else 0,
            "avg_comments": sum(r.total_comments for r in recent) / total if total else 0,
            "avg_agento_attempts": sum(r.agento_attempts for r in recent) / total if total else 0,
            "fixes_applied_count": sum(1 for r in recent if r.fixes_applied_to_original),
        }


if __name__ == "__main__":
    ledger = OutcomeLedger()

    # Test logging
    result = StressTestResult(
        run_id="test-001",
        timestamp=datetime.now().isoformat(),
        slice="src/orchestration",
        lines_reviewed=4500,
        test_pr=10,
        original_pr=None,
        ai_reviewers_responded=["CodeRabbit"],
        total_comments=5,
        comments_by_severity={"Major": 2, "Minor": 3},
        agento_attempts=1,
        fix_success=True,
        time_to_green_minutes=45,
        fixes_applied_to_original=False
    )

    ledger.log(result)
    print("Weekly summary:", ledger.weekly_summary())
