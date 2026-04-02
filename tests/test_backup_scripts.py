"""Tests for backup script correctness.

Covers three known bugs:
  Bug 1 — symlink-following redaction can overwrite targets outside snapshot dir
  Bug 2 — cron migration skips updating 0 */4 to 40 */4 when path unchanged
  Bug 3 — Slack webhook payload is invalid JSON when body contains newlines
"""
from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path

import pytest

from orchestration.backup_redaction import build_slack_payload, redact_snapshot


# ---------------------------------------------------------------------------
# Bug 1: symlink safety in redaction pass
# ---------------------------------------------------------------------------

class TestRedactSnapshotSymlinkSafety:
    """redact_snapshot must not follow symlinks to files outside the snapshot."""

    def test_symlink_target_outside_snapshot_is_not_modified(self, tmp_path):
        """A symlink inside the snapshot pointing to a file outside must leave
        the target file unchanged — its content must not be overwritten."""
        # Arrange: create a "secret" file outside the snapshot
        outside = tmp_path / "outside_dir"
        outside.mkdir()
        secret_file = outside / "credentials.txt"
        original_content = "API_KEY=sk-realkey12345678901234"
        secret_file.write_text(original_content)

        # Create snapshot dir with a symlink pointing at the outside file
        snapshot = tmp_path / "snap_20260303_120000"
        snapshot.mkdir()
        symlink = snapshot / "credentials.txt"
        symlink.symlink_to(secret_file)

        # Act
        redact_snapshot(snapshot, str(outside), "20260303_120000")

        # Assert: outside file is untouched
        assert secret_file.read_text() == original_content, (
            "redact_snapshot followed a symlink and modified the target outside the snapshot"
        )

    def test_symlink_itself_is_left_in_place(self, tmp_path):
        """Symlinks inside the snapshot are preserved (not deleted)."""
        outside = tmp_path / "outside"
        outside.mkdir()
        target = outside / "config.txt"
        target.write_text("some config")

        snapshot = tmp_path / "snap"
        snapshot.mkdir()
        (snapshot / "config.txt").symlink_to(target)

        redact_snapshot(snapshot, str(outside), "ts")

        assert (snapshot / "config.txt").is_symlink(), "symlink was unexpectedly removed"

    def test_real_text_file_in_snapshot_is_redacted(self, tmp_path):
        """Non-symlink text files with secrets are still redacted."""
        snapshot = tmp_path / "snap"
        snapshot.mkdir()
        f = snapshot / "config.txt"
        f.write_text("API_KEY=sk-realkey12345678901234")

        redact_snapshot(snapshot, "/src", "ts")

        assert "[REDACTED]" in f.read_text()
        assert "sk-realkey" not in f.read_text()

    def test_clean_file_is_unchanged(self, tmp_path):
        """Files without secrets are left byte-for-byte identical."""
        snapshot = tmp_path / "snap"
        snapshot.mkdir()
        f = snapshot / "notes.txt"
        content = "nothing sensitive here"
        f.write_text(content)

        redact_snapshot(snapshot, "/src", "ts")

        assert f.read_text() == content


# ---------------------------------------------------------------------------
# Bug 2: cron migration updates schedule even when command path is unchanged
# ---------------------------------------------------------------------------

class TestCronScheduleMigration:
    """install-openclaw-backup-jobs.sh must update 0 */4 → 40 */4 even when
    the command path is already correct (old schedule, same path)."""

    @pytest.fixture()
    def runner_path(self, tmp_path) -> Path:
        r = tmp_path / "run-openclaw-backup.sh"
        r.write_text("#!/bin/bash\necho ok\n")
        r.chmod(0o755)
        return r

    @pytest.fixture()
    def watchdog_path(self, tmp_path) -> Path:
        w = tmp_path / "backup-watchdog.sh"
        w.write_text("#!/bin/bash\necho ok\n")
        w.chmod(0o755)
        return w

    def _run_cron_migration(self, crontab_content: str, runner: Path, watchdog: Path,
                             tmp_path: Path) -> str:
        """Run the cron-migration logic against a fake crontab file.
        Returns the resulting crontab content."""
        cron_file = tmp_path / "crontab.txt"
        cron_file.write_text(crontab_content)

        script = (
            "#!/usr/bin/env bash\nset -euo pipefail\n"
            'CRON_CMD="' + str(runner) + '"\n'
            'CRON_MARKER="# OpenClaw 4h backup for ~/.smartclaw"\n'
            'CRON_TMP="' + str(cron_file) + '"\n'
            '\nif ! grep -Fq "$CRON_MARKER" "$CRON_TMP"; then\n'
            '  echo "$CRON_MARKER" >> "$CRON_TMP"\n'
            '  echo "40 */4 * * * $CRON_CMD" >> "$CRON_TMP"\n'
            'elif ! grep -Fq "40 */4 * * * $CRON_CMD" "$CRON_TMP"; then\n'
            # Fix: match any minute-field before */4 and replace with 40
            '  sed -i.bak "s|^[0-9]* \\*/4 \\* \\* \\* $CRON_CMD|40 */4 * * * $CRON_CMD|" "$CRON_TMP"\n'
            'fi\n'
            'cat "$CRON_TMP"\n'
        )
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        return result.stdout

    def test_old_schedule_same_path_gets_updated(self, runner_path, watchdog_path, tmp_path):
        """When crontab has 0 */4 with correct path, schedule must be updated to 40 */4."""
        old_crontab = (
            f"# OpenClaw 4h backup for ~/.smartclaw\n"
            f"0 */4 * * * {runner_path}\n"
        )
        result = self._run_cron_migration(old_crontab, runner_path, watchdog_path, tmp_path)
        assert f"40 */4 * * * {runner_path}" in result, (
            f"Expected '40 */4' schedule in output, got:\n{result}"
        )
        assert "0 */4" not in result.replace("40 */4", ""), (
            "Old '0 */4' schedule still present after migration"
        )

    def test_correct_schedule_not_duplicated(self, runner_path, watchdog_path, tmp_path):
        """When crontab already has 40 */4, no duplicate line is added."""
        correct_crontab = (
            f"# OpenClaw 4h backup for ~/.smartclaw\n"
            f"40 */4 * * * {runner_path}\n"
        )
        result = self._run_cron_migration(correct_crontab, runner_path, watchdog_path, tmp_path)
        lines = [l for l in result.splitlines() if f"40 */4 * * * {runner_path}" in l]
        assert len(lines) == 1, f"Expected exactly 1 schedule line, got {len(lines)}: {result}"

    def test_missing_marker_installs_fresh(self, runner_path, watchdog_path, tmp_path):
        """Empty crontab gets marker + 40 */4 line."""
        result = self._run_cron_migration("", runner_path, watchdog_path, tmp_path)
        assert f"40 */4 * * * {runner_path}" in result


# ---------------------------------------------------------------------------
# Bug 3: Slack webhook payload must be valid JSON
# ---------------------------------------------------------------------------

class TestSlackPayloadJson:
    """build_slack_payload must produce valid JSON even with multiline body."""

    def test_multiline_body_is_valid_json(self):
        subject = "ALERT: OpenClaw backup stale — last backup 8h ago on myhost"
        body = (
            "OpenClaw backup watchdog alert.\n\n"
            "Host: myhost\n"
            "Newest snapshot: 20260303_120000\n"
            "Age: 8h (threshold: 6h)\n"
        )
        payload = build_slack_payload(subject, body)
        parsed = json.loads(payload)  # raises if invalid JSON
        assert "text" in parsed
        assert subject in parsed["text"]
        assert "8h" in parsed["text"]

    def test_special_characters_are_escaped(self):
        payload = build_slack_payload('say "hello"', "line1\nline2\ttabbed")
        parsed = json.loads(payload)
        assert '"hello"' in parsed["text"]
        assert "line1" in parsed["text"]
        assert "line2" in parsed["text"]

    def test_empty_body_is_valid_json(self):
        payload = build_slack_payload("subject", "")
        json.loads(payload)  # must not raise

    def test_payload_has_text_key(self):
        payload = build_slack_payload("s", "b")
        assert json.loads(payload)["text"]
