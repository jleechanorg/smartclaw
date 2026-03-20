"""E2E orchestration output integrity tests.

Guards against two failure modes found in PR #10 (ORCH-6o5) evidence:

  ORCH-1ch — task ID mismatch: exit log was from a *different* run than the
              dispatch log. Fix: match job log by task ID, not by recency.

  ORCH-2lq — self-reported line counts all wrong (claimed vs actual):
              app.js 163/114, cart.html 36/83, index.html 31/46,
              product.html 42/76, style.css 278/404.

  ORCH-xt4 — addToCart count: claimed 3, actual 2.

Strategy: never trust agent stdout for verification. Always measure the actual
output directory independently.

Run modes:
  Normal pytest run (unit/integration, no real claudem):
    PYTHONPATH=src pytest src/tests/test_e2e_orch_output_integrity.py -v

  Against real PR #10 evidence (if /tmp/evidence/orch-e2e-pr10/ exists):
    PYTHONPATH=src pytest src/tests/test_e2e_orch_output_integrity.py -v -k evidence
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers — job log parsing (mirrors the historical dispatch log format)
# ---------------------------------------------------------------------------

def _parse_job_log(log_path: Path) -> dict:
    """Parse a job log written by build_claudem_dispatch().

    Expected format:
        # Task ID: <uuid>
        # Title: <title>
        # Timestamp: <int>
        # Exit code: <int>
        # STDOUT:
        <stdout content>
        # STDERR:
        <stderr content>

    Returns dict with keys: task_id, title, timestamp, exit_code, stdout, stderr.
    Missing keys are None.
    """
    text = log_path.read_text(encoding="utf-8", errors="replace")
    result: dict = {}

    for line in text.splitlines():
        if m := re.match(r"^# Task ID:\s*(.+)$", line):
            result["task_id"] = m.group(1).strip()
        elif m := re.match(r"^# Title:\s*(.+)$", line):
            result["title"] = m.group(1).strip()
        elif m := re.match(r"^# Timestamp:\s*(\d+)$", line):
            result["timestamp"] = int(m.group(1))
        elif m := re.match(r"^# Exit code:\s*(\d+)$", line):
            result["exit_code"] = int(m.group(1))

    result.setdefault("task_id", None)
    result["stdout"] = text
    return result


def _verify_output_dir(
    output_dir: Path,
    *,
    required_files: list[str],
    min_line_counts: dict[str, int] | None = None,
    required_strings: dict[str, list[str]] | None = None,
) -> dict:
    """Independently verify output directory contents.

    Args:
        output_dir: Directory to inspect.
        required_files: File names that must exist.
        min_line_counts: {filename: min_lines}. Each file must have >= min_lines.
        required_strings: {filename: [str, ...]}.  Each string must appear >= 1 time.

    Returns dict:
        {
          "passed": bool,
          "missing_files": [...],
          "line_counts": {filename: actual_count},
          "line_count_failures": {filename: {"min": n, "actual": m}},
          "string_failures": {filename: [missing_str, ...]},
        }
    """
    missing_files = [f for f in required_files if not (output_dir / f).exists()]
    line_counts: dict[str, int] = {}
    line_count_failures: dict[str, dict] = {}
    string_failures: dict[str, list[str]] = {}

    for fname in required_files:
        fpath = output_dir / fname
        if not fpath.exists():
            continue
        lines = fpath.read_text(encoding="utf-8", errors="replace").splitlines()
        line_counts[fname] = len(lines)

        if min_line_counts and fname in min_line_counts:
            min_n = min_line_counts[fname]
            if len(lines) < min_n:
                line_count_failures[fname] = {"min": min_n, "actual": len(lines)}

        if required_strings and fname in required_strings:
            text = fpath.read_text(encoding="utf-8", errors="replace")
            missing = [s for s in required_strings[fname] if s not in text]
            if missing:
                string_failures[fname] = missing

    passed = not missing_files and not line_count_failures and not string_failures
    return {
        "passed": passed,
        "missing_files": missing_files,
        "line_counts": line_counts,
        "line_count_failures": line_count_failures,
        "string_failures": string_failures,
    }


# ---------------------------------------------------------------------------
# Tests: job log parsing
# ---------------------------------------------------------------------------

class TestJobLogParsing:
    """_parse_job_log correctly extracts structured fields."""

    def test_parses_task_id(self, tmp_path):
        log = tmp_path / "job.log"
        log.write_text(
            "# Task ID: abc-123\n"
            "# Title: My Task\n"
            "# Timestamp: 1700000000\n"
            "# Exit code: 0\n"
            "# STDOUT:\nhello\n"
            "# STDERR:\n"
        )
        parsed = _parse_job_log(log)
        assert parsed["task_id"] == "abc-123"

    def test_parses_exit_code(self, tmp_path):
        log = tmp_path / "job.log"
        log.write_text("# Task ID: t1\n# Exit code: 1\n")
        assert _parse_job_log(log)["exit_code"] == 1

    def test_parses_timestamp(self, tmp_path):
        log = tmp_path / "job.log"
        log.write_text("# Task ID: t1\n# Timestamp: 1772418616\n# Exit code: 0\n")
        assert _parse_job_log(log)["timestamp"] == 1772418616

    def test_missing_task_id_returns_none(self, tmp_path):
        log = tmp_path / "job.log"
        log.write_text("# Exit code: 0\n")
        assert _parse_job_log(log)["task_id"] is None

    def test_job_log_filename_encodes_task_id(self, tmp_path):
        """Job log filename format is <task_id>_<timestamp>.log.
        Verify we can recover the task_id from the filename independently.
        """
        task_id = "7160f654-8cbb-49ae-afaa-7999663392ad"
        ts = 1772422879
        log_name = f"{task_id}_{ts}.log"
        log = tmp_path / log_name
        log.write_text(f"# Task ID: {task_id}\n# Exit code: 0\n")

        # task_id is recoverable from filename
        name_parts = log.stem.split("_", 1)
        assert name_parts[0] == task_id


# ---------------------------------------------------------------------------
# Tests: task ID correlation (ORCH-1ch)
# ---------------------------------------------------------------------------

class TestTaskIdCorrelation:
    """Job log must be correlated to the dispatched task by task_id, not recency.

    Guards against ORCH-1ch: evidence package mixed logs from two different runs.
    """

    def test_job_log_task_id_matches_dispatched_task(self, tmp_path):
        """The task_id written to the job log equals the task_id that was dispatched."""
        dispatched_task_id = "task-correct-id"
        stale_task_id = "task-stale-from-previous-run"

        # Simulate two log files — one stale (earlier run), one current
        log_dir = tmp_path / "ralph" / "jobs"
        log_dir.mkdir(parents=True)

        # Stale log written "earlier" (lower timestamp)
        stale_log = log_dir / f"{stale_task_id}_1000.log"
        stale_log.write_text(
            f"# Task ID: {stale_task_id}\n# Exit code: 0\n# STDOUT:\ndone\n"
        )

        # Current log written "later" — but might not be most recent on disk
        current_log = log_dir / f"{dispatched_task_id}_2000.log"
        current_log.write_text(
            f"# Task ID: {dispatched_task_id}\n# Exit code: 0\n# STDOUT:\ndone\n"
        )

        # Evidence collection must find the log by task_id match, not glob(*) recency
        matched = next(
            (p for p in log_dir.glob("*.log") if p.name.startswith(dispatched_task_id)),
            None,
        )
        assert matched is not None, "Log for dispatched task not found"
        parsed = _parse_job_log(matched)
        assert parsed["task_id"] == dispatched_task_id

    def test_selecting_most_recent_log_picks_wrong_task(self, tmp_path):
        """Demonstrates the PR #10 bug: most-recent-log selection picks the wrong task."""
        dispatched_task_id = "task-dispatched"
        newer_unrelated_task_id = "task-newer-unrelated"

        log_dir = tmp_path / "ralph" / "jobs"
        log_dir.mkdir(parents=True)

        # Dispatched task log written first (older mtime)
        dispatched_log = log_dir / f"{dispatched_task_id}_1000.log"
        dispatched_log.write_text(
            f"# Task ID: {dispatched_task_id}\n# Exit code: 0\n"
        )
        time.sleep(0.01)  # ensure mtime differs

        # A newer log from a *different* task runs later (higher mtime)
        newer_log = log_dir / f"{newer_unrelated_task_id}_2000.log"
        newer_log.write_text(
            f"# Task ID: {newer_unrelated_task_id}\n# Exit code: 0\n"
        )

        # Bug: picking most-recent log ignores which task was dispatched
        most_recent = max(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime)
        parsed_recent = _parse_job_log(most_recent)
        # This selects the WRONG task — demonstrates the PR #10 mismatch
        assert parsed_recent["task_id"] == newer_unrelated_task_id
        assert parsed_recent["task_id"] != dispatched_task_id

        # Correct: select by task_id prefix
        correct = next(log_dir.glob(f"{dispatched_task_id}_*.log"), None)
        assert correct is not None
        assert _parse_job_log(correct)["task_id"] == dispatched_task_id


# ---------------------------------------------------------------------------
# Tests: output integrity verification (ORCH-2lq, ORCH-xt4)
# ---------------------------------------------------------------------------

class TestOutputIntegrityVerification:
    """_verify_output_dir independently measures files; does not trust agent stdout.

    Guards against ORCH-2lq (wrong line counts) and ORCH-xt4 (wrong grep count).
    """

    def test_detects_missing_files(self, tmp_path):
        result = _verify_output_dir(
            tmp_path,
            required_files=["app.js", "index.html"],
        )
        assert not result["passed"]
        assert "app.js" in result["missing_files"]
        assert "index.html" in result["missing_files"]

    def test_passes_when_all_files_present(self, tmp_path):
        (tmp_path / "app.js").write_text("line1\nline2\n")
        (tmp_path / "index.html").write_text("<html/>\n")
        result = _verify_output_dir(
            tmp_path,
            required_files=["app.js", "index.html"],
        )
        assert result["passed"]

    def test_detects_file_below_min_line_count(self, tmp_path):
        (tmp_path / "app.js").write_text("only one line")
        result = _verify_output_dir(
            tmp_path,
            required_files=["app.js"],
            min_line_counts={"app.js": 20},
        )
        assert not result["passed"]
        assert "app.js" in result["line_count_failures"]
        assert result["line_count_failures"]["app.js"]["min"] == 20
        assert result["line_count_failures"]["app.js"]["actual"] == 1

    def test_reports_actual_line_count_not_claimed(self, tmp_path):
        """Actual count is measured independently; claimed count from stdout is irrelevant."""
        actual_lines = 114
        content = "\n".join(f"line{i}" for i in range(actual_lines))
        (tmp_path / "app.js").write_text(content)

        result = _verify_output_dir(
            tmp_path,
            required_files=["app.js"],
            min_line_counts={"app.js": 20},  # minimum, not claimed count
        )
        assert result["passed"]
        assert result["line_counts"]["app.js"] == actual_lines

    def test_detects_missing_required_string(self, tmp_path):
        (tmp_path / "app.js").write_text("function foo() {}\n")
        result = _verify_output_dir(
            tmp_path,
            required_files=["app.js"],
            required_strings={"app.js": ["addToCart", "localStorage"]},
        )
        assert not result["passed"]
        assert "addToCart" in result["string_failures"]["app.js"]
        assert "localStorage" in result["string_failures"]["app.js"]

    def test_passes_with_correct_string_counts(self, tmp_path):
        (tmp_path / "app.js").write_text(
            "function addToCart(id) {}\n"
            "function addToCart2(id) {}\n"  # 2 occurrences
            "localStorage.setItem('cart', JSON.stringify(cart));\n"
        )
        result = _verify_output_dir(
            tmp_path,
            required_files=["app.js"],
            required_strings={"app.js": ["addToCart", "localStorage"]},
        )
        assert result["passed"]


# ---------------------------------------------------------------------------
# Tests: reproduce PR #10 evidence failures (real evidence files)
# ---------------------------------------------------------------------------

EVIDENCE_DIR = Path("/tmp/evidence/orch-e2e-pr10")
AMAZON_CLONE_DIR = EVIDENCE_DIR / "amazon-clone"

_EVIDENCE_EXISTS = EVIDENCE_DIR.exists() and AMAZON_CLONE_DIR.exists()


@pytest.mark.skipif(not _EVIDENCE_EXISTS, reason="PR #10 evidence not present at /tmp/evidence/orch-e2e-pr10/")
class TestPR10EvidenceIntegrity:
    """Reproduce the exact failures found in PR #10 evidence.

    These tests run against the real /tmp/evidence/orch-e2e-pr10/ files.
    They are skipped in normal CI unless the evidence directory is present.
    """

    def test_task_id_mismatch_in_evidence_logs(self):
        """ORCH-1ch: task-poller.log and ralph-job-exit0.log have different task IDs."""
        poller_log = EVIDENCE_DIR / "task-poller.log"
        exit_log = EVIDENCE_DIR / "ralph-job-exit0.log"

        assert poller_log.exists(), "task-poller.log missing from evidence"
        assert exit_log.exists(), "ralph-job-exit0.log missing from evidence"

        # Extract task_id from task-poller.log (it logs the dispatched ID)
        poller_text = poller_log.read_text()
        poller_match = re.search(
            r"Dispatching task ([0-9a-f-]{36}) to claudem", poller_text
        )
        assert poller_match, f"Could not find dispatched task ID in task-poller.log:\n{poller_text}"
        dispatched_task_id = poller_match.group(1)

        # Extract task_id from exit log header
        exit_parsed = _parse_job_log(exit_log)
        exit_task_id = exit_parsed["task_id"]

        assert dispatched_task_id != exit_task_id, (
            "Expected mismatch (reproducing ORCH-1ch) but task IDs matched — "
            "evidence may have been fixed already."
        )
        # The bug: exit log is from a different run
        assert dispatched_task_id == "7160f654-8cbb-49ae-afaa-7999663392ad"
        assert exit_task_id == "4a992daf-750d-415e-a17d-915394edab23"

    def test_exit_log_line_counts_dont_match_actual_files(self):
        """ORCH-2lq: exit log self-reports wrong line counts for all 5 files."""
        # Counts the agent claimed in ralph-job-exit0.log
        claimed = {
            "app.js": 163,
            "cart.html": 36,
            "index.html": 31,
            "product.html": 42,
            "style.css": 278,
        }

        for fname, claimed_count in claimed.items():
            fpath = AMAZON_CLONE_DIR / fname
            assert fpath.exists(), f"{fname} missing from amazon-clone/"
            actual = len(fpath.read_text(encoding="utf-8").splitlines())
            assert actual != claimed_count, (
                f"{fname}: expected mismatch (ORCH-2lq bug) but counts match ({actual}). "
                "Evidence may have been regenerated."
            )

    def test_add_to_cart_count_wrong(self):
        """ORCH-xt4: exit log claimed 3 addToCart occurrences, actual is 2."""
        app_js = AMAZON_CLONE_DIR / "app.js"
        assert app_js.exists()
        text = app_js.read_text()
        actual_count = text.count("addToCart")
        assert actual_count == 2, (
            f"Expected 2 addToCart occurrences (exit log claimed 3), got {actual_count}"
        )

    def test_independent_verification_passes_with_real_thresholds(self):
        """Independent verifier passes against actual files using minimum thresholds."""
        result = _verify_output_dir(
            AMAZON_CLONE_DIR,
            required_files=["app.js", "index.html", "product.html", "cart.html", "style.css"],
            min_line_counts={
                "app.js": 20,
                "cart.html": 20,
                "index.html": 20,
                "product.html": 20,
                "style.css": 20,
            },
            required_strings={
                "app.js": ["addToCart", "localStorage"],
                "style.css": ["#131921"],
            },
        )
        assert result["passed"], (
            f"Independent verification failed:\n"
            f"  missing: {result['missing_files']}\n"
            f"  line count failures: {result['line_count_failures']}\n"
            f"  string failures: {result['string_failures']}"
        )
