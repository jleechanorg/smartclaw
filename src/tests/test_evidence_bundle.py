"""Tests for evidence_bundle module."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from orchestration.code_path_classifier import is_code_path
from orchestration.evidence_bundle import EvidenceBundle


class TestIsCodePath:
    """Test is_code_path classification."""

    def test_orchestration_code(self) -> None:
        assert is_code_path("src/orchestration/merge_gate.py") is True

    def test_scripts(self) -> None:
        assert is_code_path("scripts/ao-backfill.sh") is True

    def test_soul_md(self) -> None:
        assert is_code_path("SOUL.md") is True

    def test_docs_skipped(self) -> None:
        assert is_code_path("docs/readme.md") is False

    def test_beads_skipped(self) -> None:
        assert is_code_path(".beads/issues.jsonl") is False

    def test_tests_skipped(self) -> None:
        assert is_code_path("src/tests/test_foo.py") is False

    def test_roadmap_skipped(self) -> None:
        assert is_code_path("roadmap/design.md") is False

    def test_workspace_md(self) -> None:
        assert is_code_path("workspace/SOUL.md") is True
        assert is_code_path("workspace/TOOLS.md") is True


class TestEvidenceBundleGenerate:
    """Test EvidenceBundle.generate() creates proper directory structure."""

    @patch("orchestration.evidence_bundle.run_gh")
    def test_generates_bundle_structure(self, mock_run_gh: object, tmp_path: Path) -> None:
        """Bundle creates dirs, claims.md, self_review.md, verdict.json."""
        mock_run_gh.side_effect = [  # type: ignore[attr-defined]
            # get_pr_files (newline-delimited from --paginate .[].filename)
            (0, "src/orchestration/merge_gate.py", ""),
            # collect_ci: head SHA
            (0, "abc123", ""),
            # collect_ci: check-runs
            (0, json.dumps({"check_runs": [{"name": "test", "status": "completed", "conclusion": "success"}]}), ""),
            # collect_cr_review
            (0, json.dumps([{"user": {"login": "coderabbitai[bot]"}, "state": "COMMENTED"}]), ""),
            # _is_cr_paused: no CR issue comments (not paused)
            (0, "[]", ""),
            # collect_pr_diff
            (0, "+line1\n-line2\n", ""),
            # collect_unresolved_threads (paginated — includes pageInfo)
            (0, json.dumps({"data": {"repository": {"pullRequest": {"reviewThreads": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []}}}}}), ""),
            # run_pytest is called via subprocess, not run_gh
        ]

        bundle = EvidenceBundle(
            owner="owner", repo="repo", pr_number=42,
            repo_root=tmp_path, timestamp="20260317_0100_utc",
        )

        # Create a fake test dir so pytest doesn't fail
        (tmp_path / "src" / "tests").mkdir(parents=True)
        (tmp_path / "src" / "tests" / "test_dummy.py").write_text("def test_pass(): pass\n")
        (tmp_path / "src" / "orchestration").mkdir(parents=True)
        (tmp_path / "src" / "orchestration" / "__init__.py").write_text("")

        verdict = bundle.generate()

        # Check directory structure
        assert bundle.bundle_dir.exists()
        assert bundle.artifacts_dir.exists()
        assert (bundle.bundle_dir / "claims.md").exists()
        assert (bundle.bundle_dir / "self_review.md").exists()
        assert (bundle.bundle_dir / "verdict.json").exists()

        # Check verdict
        assert verdict["pr"] == 42
        assert verdict["repo"] == "owner/repo"
        assert verdict["stage1"]["status"] in ("PASS", "FAIL")
        assert verdict["stage2"]["status"] == "PENDING"
        # overall depends on whether pytest passed in the sandbox
        assert verdict["overall"] in ("PASS", "FAIL", "PENDING")

    @patch("orchestration.evidence_bundle.run_gh")
    def test_ci_failure_causes_stage1_fail(self, mock_run_gh: object, tmp_path: Path) -> None:
        """Failed CI should cause stage 1 FAIL."""
        mock_run_gh.side_effect = [  # type: ignore[attr-defined]
            (0, "scripts/foo.sh", ""),
            (0, "abc123", ""),
            (0, json.dumps({"check_runs": [
                {"name": "test", "status": "completed", "conclusion": "failure"},
            ]}), ""),
            (0, "[]", ""),
            (0, "", ""),
            (0, json.dumps({"data": {"repository": {"pullRequest": {"reviewThreads": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []}}}}}), ""),
        ]

        bundle = EvidenceBundle(owner="o", repo="r", pr_number=1, repo_root=tmp_path)
        (tmp_path / "src" / "tests").mkdir(parents=True)

        verdict = bundle.generate()
        assert verdict["stage1"]["status"] == "FAIL"
        assert any("failed" in f.lower() for f in verdict["stage1"]["findings"])

    @patch("orchestration.evidence_bundle.run_gh")
    def test_pr_file_fetch_failure_fails_stage1(self, mock_run_gh: object, tmp_path: Path) -> None:
        """API failure fetching PR files should fail Stage 1."""
        mock_run_gh.side_effect = [  # type: ignore[attr-defined]
            (1, "", "API error"),  # get_pr_files API failure
            (0, "abc123", ""),
            (0, json.dumps({"check_runs": [{"name": "test", "status": "completed", "conclusion": "success"}]}), ""),
            (0, "[]", ""),
            (0, "+line1\n-line2\n", ""),
            (0, json.dumps({"data": {"repository": {"pullRequest": {"reviewThreads": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []}}}}}), ""),
        ]

        bundle = EvidenceBundle(owner="o", repo="r", pr_number=1, repo_root=tmp_path)
        (tmp_path / "src" / "tests").mkdir(parents=True)

        verdict = bundle.generate()
        assert verdict["stage1"]["status"] == "FAIL"
        assert any("determine changed files" in f.lower() for f in verdict["stage1"]["findings"])


class TestCheckEvidencePassUpdated:
    """Test updated check_evidence_pass with verdict.json support."""

    @patch("orchestration.merge_gate.run_gh")
    def test_no_code_files_skips(self, mock_run_gh: object) -> None:
        """Docs-only PRs skip evidence."""
        from orchestration.merge_gate import check_evidence_pass

        mock_run_gh.side_effect = [  # type: ignore[attr-defined]
            (0, "abc123sha", ""),  # PR head SHA
            (0, "docs/readme.md\nroadmap/design.md", ""),  # PR files (newline-delimited)
        ]
        result = check_evidence_pass("o", "r", 1)
        assert result.passed is True
        assert "skipped" in result.details.lower()

    @patch("orchestration.merge_gate.run_gh")
    def test_code_files_without_evidence_blocks(self, mock_run_gh: object) -> None:
        """Code PR without evidence bundle blocks (no legacy fallback)."""
        from orchestration.merge_gate import check_evidence_pass

        mock_run_gh.side_effect = [  # type: ignore[attr-defined]
            (0, "abc123sha", ""),  # PR head SHA
            (0, "src/orchestration/foo.py", ""),  # PR files (newline-delimited)
        ]
        result = check_evidence_pass("o", "r", 1)
        assert result.passed is False
        assert "evidence bundle" in result.details.lower()

    @patch("orchestration.merge_gate.run_gh")
    def test_legacy_pass_comment_no_longer_accepted(self, mock_run_gh: object) -> None:
        """Legacy evidence PASS comment no longer passes — full bundle required."""
        from orchestration.merge_gate import check_evidence_pass

        mock_run_gh.side_effect = [  # type: ignore[attr-defined]
            (0, "abc123sha", ""),  # PR head SHA
            (0, "scripts/foo.sh", ""),  # PR files
        ]
        result = check_evidence_pass("o", "r", 1)
        assert result.passed is False
        assert "evidence bundle" in result.details.lower()


class TestValidateVerdict:
    """Tests for validate_verdict() invariant enforcement."""

    def test_stage1_fail_forces_overall_fail(self) -> None:
        from orchestration.evidence_bundle import validate_verdict
        verdict = {
            "stage1": {"status": "FAIL"},
            "stage2": {"status": "PASS", "independence_verified": True},
            "overall": "PASS",
        }
        result = validate_verdict(verdict)
        assert result["overall"] == "FAIL"

    def test_stage1_pass_stage2_pending_is_pending(self) -> None:
        from orchestration.evidence_bundle import validate_verdict
        verdict = {
            "stage1": {"status": "PASS"},
            "stage2": {"status": "PENDING", "independence_verified": False},
            "overall": "PASS",
        }
        result = validate_verdict(verdict)
        assert result["overall"] == "PENDING"

    def test_stage1_pass_stage2_pass_without_independence_is_pending(self) -> None:
        from orchestration.evidence_bundle import validate_verdict
        verdict = {
            "stage1": {"status": "PASS"},
            "stage2": {"status": "PASS", "independence_verified": False},
            "overall": "PASS",
        }
        result = validate_verdict(verdict)
        assert result["overall"] == "PENDING"

    def test_both_pass_with_independence_is_pass(self) -> None:
        from orchestration.evidence_bundle import validate_verdict
        verdict = {
            "stage1": {"status": "PASS"},
            "stage2": {"status": "PASS", "independence_verified": True, "model_family_differs_from_stage1": True},
            "overall": "FAIL",
        }
        result = validate_verdict(verdict)
        assert result["overall"] == "PASS"

    def test_same_family_blocks_overall_pass(self) -> None:
        """independence_verified but same model family should not be PASS."""
        from orchestration.evidence_bundle import validate_verdict
        verdict = {
            "stage1": {"status": "PASS"},
            "stage2": {"status": "PASS", "independence_verified": True, "model_family_differs_from_stage1": False},
            "overall": "PASS",
        }
        result = validate_verdict(verdict)
        assert result["overall"] == "PENDING"

    def test_stage2_fail_forces_overall_fail(self) -> None:
        from orchestration.evidence_bundle import validate_verdict
        verdict = {
            "stage1": {"status": "PASS"},
            "stage2": {"status": "FAIL", "independence_verified": False},
            "overall": "PENDING",
        }
        result = validate_verdict(verdict)
        assert result["overall"] == "FAIL"

    def test_missing_stage2_defaults_to_pending(self) -> None:
        from orchestration.evidence_bundle import validate_verdict
        verdict = {
            "stage1": {"status": "PASS"},
            "overall": "PASS",
        }
        result = validate_verdict(verdict)
        assert result["overall"] == "PENDING"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
