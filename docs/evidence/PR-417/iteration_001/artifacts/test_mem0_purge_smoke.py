"""Smoke tests for mem0-purge.sh.

These are deterministic shell-check + dry-run tests.
They do NOT delete any real memories.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Iterator

import pytest
import re


def extract_hash(stderr: str) -> str | None:
    """Extract 64-char hex hash from mem0-purge confirmation output."""
    m = re.search(r"Confirmation hash.*?([0-9a-f]{64})", stderr)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Module-level fixture: create a minimal temp HOME with the hooks stub so
# all dry-run/verify-only tests are hermetic and do not require the real
# openclaw environment.
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module", autouse=True)
def temp_home_with_hooks() -> Iterator[str]:
    """Create a temp HOME with ~/.smartclaw/.claude/hooks/mem0_config.py stub.

    mem0-purge.sh reads Qdrant config from the hooks path. Without this stub,
    dry-run tests die with "mem0 hooks dir not found" even in safe dry-run mode.
    """
    tmp = tempfile.mkdtemp()
    hooks_dir = Path(tmp) / ".smartclaw" / ".claude" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    (hooks_dir / "mem0_config.py").write_text(
        "MEM0_CONFIG = {'vector_store': {'config': "
        "{'host': '127.0.0.1', 'port': 6333, 'collection_name': 'openclaw_mem0'}}}\n"
        "USER_ID = 'test'\n"
    )
    old_home = os.environ.get("HOME")
    os.environ["HOME"] = tmp
    yield tmp
    if old_home is not None:
        os.environ["HOME"] = old_home
    else:
        del os.environ["HOME"]
    shutil.rmtree(tmp, ignore_errors=True)


def _find_repo_root() -> Path:
    """Walk up from __file__ to find the repo root (has .git or scripts/)."""
    current = Path(__file__).resolve().parent
    while current != current.parent:
        if (current / ".git").exists() or (current / "scripts").is_dir():
            return current
        current = current.parent
    # Fallback: 4 levels up from artifacts/ → repo root
    return Path(__file__).parent.parent.parent.parent


def run_script(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run mem0-purge.sh from the repo scripts/ directory."""
    repo_root = _find_repo_root()
    script = repo_root / "scripts" / "mem0-purge.sh"
    return subprocess.run(
        ["bash", str(script)] + list(args),
        capture_output=True,
        text=True,
        check=check,
    )


class TestMem0PurgeShellcheck:
    """Static analysis: shellcheck passes."""

    @pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
    def test_shellcheck_clean(self) -> None:
        """ShellCheck finds no errors in mem0-purge.sh."""
        repo_root = _find_repo_root()
        script = repo_root / "scripts" / "mem0-purge.sh"
        r = subprocess.run(
            ["shellcheck", str(script)],
            capture_output=True,
            text=True,
        )
        # SC1090: Can't follow non-constant source (our python heredoc is intentional)
        # Allow those; everything else must be clean
        errors = [
            line for line in r.stdout.splitlines()
            if "SC1090" not in line and line.strip()
        ]
        assert r.returncode == 0 or (not errors), (
            f"shellcheck errors:\n{chr(10).join(errors)}\n\nFull output:\n{r.stdout}\n{r.stderr}"
        )


class TestMem0PurgeDryRunParsing:
    """Dry-run parsing: guards, ID validation, preview output."""

    def test_help_flag_exits_zero(self) -> None:
        r = run_script("--help", check=False)
        assert r.returncode == 0

    def test_verify_only_exits_zero(self) -> None:
        # Skip if Qdrant is not reachable so the test is hermetic on clean CI machines.
        try:
            import urllib.request
            urllib.request.urlopen("http://127.0.0.1:6333/healthz", timeout=2)
        except Exception:
            pytest.skip("Qdrant not reachable on localhost:6333")
        r = run_script("--verify-only", check=False)
        assert r.returncode == 0
        # Should mention the collection
        output = r.stdout + r.stderr
        assert "openclaw_mem0" in output

    def test_dry_run_unknown_option_fails(self) -> None:
        r = run_script("--not-a-real-flag", check=False)
        assert r.returncode != 0
        assert "Unknown option" in r.stderr or "error" in r.stderr.lower()

    def test_missing_ids_file_fails(self) -> None:
        r = run_script("--ids-file", "/nonexistent/path/ids.txt", check=False)
        assert r.returncode != 0
        assert "not found" in r.stderr.lower()

    def test_empty_ids_file_fails(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("# only a comment\n\n")
            path = f.name
        try:
            r = run_script("--ids-file", path, check=False)
            assert r.returncode != 0
            assert "no valid IDs" in r.stderr.lower() or r.returncode != 0
        finally:
            Path(path).unlink()

    def test_invalid_uuid_skipped(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("not-a-uuid\n12345\n")
            path = f.name
        try:
            r = run_script("--ids-file", path, check=False)
            assert r.returncode != 0
            assert "invalid" in r.stderr.lower()
        finally:
            Path(path).unlink()

    def test_valid_uuid_accepted_no_error(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("00000000-0000-0000-0000-000000000000\n")
            path = f.name
        try:
            r = run_script("--ids-file", path, check=False)
            # Should not fail on UUID validation; may fail on Qdrant lookup
            # but the error should NOT be about UUID parsing
            assert "invalid UUID" not in r.stderr
            assert "Skipping invalid UUID" not in r.stderr
        finally:
            Path(path).unlink()

    def test_inline_ids_invalid_rejected(self) -> None:
        r = run_script(
            "--ids-inline", "not-a-uuid-thing,also-invalid",
            check=False,
        )
        assert r.returncode != 0

    def test_confirm_without_ids_file_fails(self) -> None:
        r = run_script("--confirm", check=False)
        assert r.returncode != 0

    def test_confirm_alone_requires_at_least_one_guard(self) -> None:
        """--confirm without --confirm-count or --confirm-hash must be rejected."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa\n")
            path = f.name
        try:
            r = run_script("--ids-file", path, "--confirm", check=False)
            assert r.returncode != 0
            output = (r.stderr + r.stdout).lower()
            assert any(kw in output for kw in ["requires", "neither provided", "guard"]), \
                f"Expected guard-requirement message, got: {output[:200]}"
        finally:
            Path(path).unlink()

    def test_duplicate_ids_rejected(self) -> None:
        """Duplicate UUIDs in the allowlist must be rejected with a clear error."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa\n")
            f.write("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb\n")
            f.write("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa\n")  # duplicate
            path = f.name
        try:
            r = run_script("--ids-file", path, check=False)
            assert r.returncode != 0
            output = (r.stderr + r.stdout).lower()
            assert "duplicate" in output, \
                f"Expected duplicate-ID rejection, got: {output[:300]}"
        finally:
            Path(path).unlink()

    def test_dry_run_default_prints_preview(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            # Use a UUID that definitely won't exist in Qdrant
            f.write("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa\n")
            path = f.name
        try:
            r = run_script("--ids-file", path, "--dry-run", check=False)
            assert r.returncode == 0, f"dry-run should exit 0, got {r.returncode}: {r.stderr}"
            assert "PREVIEW" in r.stderr or "preview" in r.stderr.lower()
            assert "No deletions performed" in r.stderr or "DRY-RUN complete" in r.stderr
            # Should NOT say "LIVE DELETION"
            assert "LIVE DELETION" not in r.stderr
        finally:
            Path(path).unlink()

    def test_confirm_flag_blocks_dry_run(self) -> None:
        """Passing --confirm without guards should still reach confirmation check."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa\n")
            path = f.name
        try:
            r = run_script("--ids-file", path, "--confirm", check=False)
            # Should reach confirmation guards and fail on count/hash mismatch
            # not fail silently
            output = r.stderr + r.stdout
            assert any(
                kw in output.lower()
                for kw in ["mismatch", "abort", "failed", "guards"]
            ), f"Expected confirmation guard failure, got: {output[:300]}"
        finally:
            Path(path).unlink()

    def test_confirm_hash_mismatch_fails(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa\n")
            path = f.name
        try:
            r = run_script(
                "--ids-file", path,
                "--confirm",
                "--confirm-count", "1",
                "--confirm-hash", "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
                check=False,
            )
            assert r.returncode != 0
            assert "mismatch" in r.stderr.lower() or "abort" in r.stderr.lower()
        finally:
            Path(path).unlink()

    def test_confirm_count_mismatch_fails(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa\n")
            path = f.name
        try:
            r = run_script(
                "--ids-file", path,
                "--confirm",
                "--confirm-count", "99",  # wrong count
                check=False,
            )
            assert r.returncode != 0
        finally:
            Path(path).unlink()

    def test_hash_is_deterministic(self) -> None:
        """Confirm the same IDs always produce the same hash."""
        ids1 = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa\nbbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb\n"
        ids2 = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb\naaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa\n"

        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write(ids1)
            path1 = f.name
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write(ids2)
            path2 = f.name

        try:
            r1 = run_script("--ids-file", path1, check=False)
            r2 = run_script("--ids-file", path2, check=False)

            # Extract hash from output
            h1 = extract_hash(r1.stderr)
            h2 = extract_hash(r2.stderr)

            assert h1 is not None, f"Could not extract hash from output: {r1.stderr[:200]}"
            assert h1 == h2, "Hash must be order-independent (sorted IDs)"
        finally:
            Path(path1).unlink()
            Path(path2).unlink()

    def test_hash_changes_on_different_ids(self) -> None:
        """Different ID sets produce different hashes."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa\n")
            path1 = f.name
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb\n")
            path2 = f.name

        try:
            r1 = run_script("--ids-file", path1, check=False)
            r2 = run_script("--ids-file", path2, check=False)

            h1 = extract_hash(r1.stderr)
            h2 = extract_hash(r2.stderr)

            assert h1 != h2, "Different ID sets must produce different hashes"
        finally:
            Path(path1).unlink()
            Path(path2).unlink()

    def test_known_benjamin_hash_verification(self) -> None:
        """Verify the hard-coded confirmation hash in the runbook is correct.

        Mirrors the script's compute_hash pipeline:
        unsorted IDs -> LC_ALL=C sort -> python3 hashlib sha256
        """
        import hashlib

        # Unsorted order as it appears in the allowlist file
        benjamin_ids = (
            "14ddf0c0-a8e4-49e3-941c-849c071c713c\n"
            "196d0128-a0d7-4492-a7d0-154e0be33ab7\n"
            "17a00b39-7563-4428-bf2a-e83f9670180e\n"
            "3684c632-97dc-443e-b425-e89717c7d299\n"
            "7ee22498-0598-4bcc-b9e6-1cb815c29868\n"
            "dade246e-aa4d-4c93-9c58-4a98ddb31984\n"
            "d06b88e0-2d3b-4499-b159-a65dafa791ab\n"
            "d5f1143e-5e3d-4219-b998-9a2c0f5f9275\n"
        )

        # Pipe through LC_ALL=C sort | python3 hashlib (matches compute_hash in the script)
        sorted_ids = subprocess.run(
            ["sh", "-c", "LC_ALL=C sort"],
            input=benjamin_ids,
            capture_output=True,
            text=True,
            check=True,
        )
        computed = hashlib.sha256(sorted_ids.stdout.encode()).hexdigest()
        # This must match what compute_hash() in mem0-purge.sh produces
        # for the 8 Benjamin IDs. Verify with:
        #   bash scripts/mem0-purge.sh --ids-file /tmp/b.txt --dry-run
        expected = "03e4c283a85a1df739c3d7b2d61d642bd6d543d87b5f204f58db56f0562a1f57"
        assert computed == expected, (
            f"Runbook hash mismatch: expected {expected}, got {computed}"
        )
