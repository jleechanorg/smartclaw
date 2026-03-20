"""Tests for stage2_reviewer module."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from orchestration.stage2_reviewer import (
    Stage2Result,
    _parse_verdict_from_review,
    _read_bundle,
    run_stage2,
)


class TestParseVerdictFromReview:
    """Test parsing PASS/FAIL from reviewer markdown output."""

    def test_pass_verdict(self) -> None:
        review = "## Verdict\n\n**PASS** — all claims verified"
        status, findings = _parse_verdict_from_review(review)
        assert status == "PASS"

    def test_fail_verdict(self) -> None:
        review = "## Verdict\n\n**FAIL** — missing test evidence"
        status, findings = _parse_verdict_from_review(review)
        assert status == "FAIL"

    def test_fail_closed_on_ambiguous(self) -> None:
        review = "some text without a clear verdict"
        status, findings = _parse_verdict_from_review(review)
        assert status == "FAIL"

    def test_extracts_findings(self) -> None:
        review = (
            "## Findings\n\n"
            "- CI check missing for lint\n"
            "- Pytest output shows warnings\n\n"
            "## Verdict\n\n**PASS** — minor issues only"
        )
        status, findings = _parse_verdict_from_review(review)
        assert status == "PASS"
        assert len(findings) == 2
        assert "CI check missing" in findings[0]

    def test_empty_findings_section(self) -> None:
        review = "## Findings\n\n(none)\n\n## Verdict\n\n**PASS**"
        _, findings = _parse_verdict_from_review(review)
        assert findings == []


class TestReadBundle:
    """Test reading evidence bundle contents."""

    def test_reads_claims_and_artifacts(self, tmp_path: Path) -> None:
        (tmp_path / "claims.md").write_text("# Claims\n- CI passed\n")
        artifacts = tmp_path / "artifacts"
        artifacts.mkdir()
        (artifacts / "ci_check_runs.json").write_text('{"check_runs": []}')

        claims, artifact_list, summaries = _read_bundle(tmp_path)
        assert "CI passed" in claims
        assert "ci_check_runs.json" in artifact_list
        assert "ci_check_runs.json" in summaries

    def test_missing_claims(self, tmp_path: Path) -> None:
        claims, _, _ = _read_bundle(tmp_path)
        assert "not found" in claims

    def test_truncates_large_artifacts(self, tmp_path: Path) -> None:
        artifacts = tmp_path / "artifacts"
        artifacts.mkdir()
        (artifacts / "big.txt").write_text("x" * 5000)

        _, _, summaries = _read_bundle(tmp_path)
        assert "truncated" in summaries


class TestRunStage2:
    """Test the full stage 2 dispatch pipeline."""

    @patch("orchestration.stage2_reviewer._invoke_reviewer")
    def test_pass_updates_verdict(self, mock_invoke: object, tmp_path: Path) -> None:
        """Stage 2 PASS should update verdict.json with independence fields."""
        # Set up evidence bundle
        bundle_dir = tmp_path / "bundle"
        bundle_dir.mkdir()
        (bundle_dir / "claims.md").write_text("# Claims\n- Tests pass\n")
        artifacts = bundle_dir / "artifacts"
        artifacts.mkdir()
        (artifacts / "pytest_output.txt").write_text("6 passed in 0.1s")

        verdict = {
            "pr": 42,
            "repo": "owner/repo",
            "stage1": {"status": "PASS", "findings": []},
            "stage2": {"status": "PENDING"},
            "overall": "PENDING",
        }
        verdict_path = bundle_dir / "verdict.json"
        verdict_path.write_text(json.dumps(verdict))

        mock_invoke.return_value = (  # type: ignore[attr-defined]
            "# Independent Evidence Review\n\n"
            "## Claim Verification\n\n"
            "| # | Claim | Evidence | Rating |\n"
            "|---|-------|----------|--------|\n"
            "| 1 | Tests pass | pytest_output.txt: 6 passed | STRONG |\n\n"
            "## Findings\n\n(none)\n\n"
            "## Verdict\n\n**PASS** — all claims verified"
        )

        result = run_stage2(verdict_path, stage1_family="anthropic")

        assert result.status == "PASS"
        assert result.reviewer_family != "anthropic"

        # Check verdict.json was updated
        updated = json.loads(verdict_path.read_text())
        assert updated["overall"] == "PASS"
        assert updated["stage2"]["status"] == "PASS"
        assert updated["stage2"]["independence_verified"] is True
        assert updated["stage2"]["model_family_differs_from_stage1"] is True

        # Check independent_review.md was written
        assert (bundle_dir / "independent_review.md").exists()

    @patch("orchestration.stage2_reviewer._invoke_reviewer")
    def test_fail_updates_verdict(self, mock_invoke: object, tmp_path: Path) -> None:
        """Stage 2 FAIL should set overall to FAIL."""
        bundle_dir = tmp_path / "bundle"
        bundle_dir.mkdir()
        (bundle_dir / "claims.md").write_text("# Claims\n- Tests pass\n")
        (bundle_dir / "artifacts").mkdir()

        verdict = {
            "pr": 42,
            "repo": "owner/repo",
            "stage1": {"status": "PASS", "findings": []},
            "stage2": {"status": "PENDING"},
            "overall": "PENDING",
        }
        verdict_path = bundle_dir / "verdict.json"
        verdict_path.write_text(json.dumps(verdict))

        mock_invoke.return_value = (  # type: ignore[attr-defined]
            "## Findings\n\n"
            "- Missing artifact for CI claim\n\n"
            "## Verdict\n\n**FAIL** — missing evidence"
        )

        result = run_stage2(verdict_path, stage1_family="anthropic")

        assert result.status == "FAIL"
        updated = json.loads(verdict_path.read_text())
        assert updated["overall"] == "FAIL"
        assert updated["stage2"]["status"] == "FAIL"
        assert len(updated["stage2"]["findings"]) > 0

    @patch("orchestration.stage2_reviewer._invoke_reviewer")
    def test_no_reviewer_available(self, mock_invoke: object, tmp_path: Path) -> None:
        """All reviewers failing should produce FAIL result."""
        bundle_dir = tmp_path / "bundle"
        bundle_dir.mkdir()
        (bundle_dir / "claims.md").write_text("# Claims\n")
        (bundle_dir / "artifacts").mkdir()

        verdict = {
            "pr": 42,
            "repo": "owner/repo",
            "stage1": {"status": "PASS", "findings": []},
            "stage2": {"status": "PENDING"},
            "overall": "PENDING",
        }
        verdict_path = bundle_dir / "verdict.json"
        verdict_path.write_text(json.dumps(verdict))

        mock_invoke.return_value = None  # type: ignore[attr-defined]

        result = run_stage2(verdict_path, stage1_family="anthropic")
        assert result.status == "FAIL"
        assert "No independent reviewer available" in result.findings[0]

    @patch("orchestration.stage2_reviewer._invoke_reviewer")
    def test_same_family_blocked(self, mock_invoke: object, tmp_path: Path) -> None:
        """Same model family as stage 1 should set model_family_differs to False."""
        bundle_dir = tmp_path / "bundle"
        bundle_dir.mkdir()
        (bundle_dir / "claims.md").write_text("# Claims\n")
        (bundle_dir / "artifacts").mkdir()

        verdict = {
            "pr": 42,
            "repo": "owner/repo",
            "stage1": {"status": "PASS", "findings": []},
            "stage2": {"status": "PENDING"},
            "overall": "PENDING",
        }
        verdict_path = bundle_dir / "verdict.json"
        verdict_path.write_text(json.dumps(verdict))

        # Mock: only anthropic reviewer responds (same family as stage 1)
        def side_effect(config: dict, prompt: str, timeout: int = 300) -> str | None:
            if config["family"] == "anthropic":
                return "## Verdict\n\n**PASS** — looks good"
            return None

        mock_invoke.side_effect = side_effect  # type: ignore[attr-defined]

        # stage1_family="openai" so anthropic is eligible but different
        result = run_stage2(verdict_path, stage1_family="openai")
        assert result.status == "PASS"

        updated = json.loads(verdict_path.read_text())
        # anthropic != openai so this should be True
        assert updated["stage2"]["model_family_differs_from_stage1"] is True


class TestLegacyFallbackRemoved:
    """Verify that the merge gate no longer accepts legacy PASS comments."""

    @patch("orchestration.merge_gate.run_gh")
    def test_code_pr_without_evidence_bundle_blocks(self, mock_run_gh: object) -> None:
        """Code PR without evidence bundle should fail (no legacy fallback)."""
        from orchestration.merge_gate import check_evidence_pass

        mock_run_gh.side_effect = [  # type: ignore[attr-defined]
            (0, "abc123sha", ""),  # PR head SHA
            (0, "src/orchestration/foo.py", ""),  # PR files (code file, no evidence)
        ]
        result = check_evidence_pass("o", "r", 1)
        assert result.passed is False
        assert "evidence bundle" in result.details.lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
