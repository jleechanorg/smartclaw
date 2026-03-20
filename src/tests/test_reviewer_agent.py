"""Tests for reviewer_agent module."""
from __future__ import annotations

from unittest.mock import patch

from orchestration.reviewer_agent import (
    FindingSeverity,
    ReviewFinding,
    ReviewVerdict,
    _is_test_path,
    build_review_body,
    check_gate,
    format_mail_findings,
    review_diff,
    run_review,
)


class TestReviewDiff:
    """Tests for review_diff() structural analysis."""

    def test_empty_diff_returns_critical(self) -> None:
        findings = review_diff("")
        assert len(findings) == 1
        assert findings[0].severity == "critical"
        assert "cannot review" in findings[0].description.lower()

    def test_clean_diff_no_findings(self) -> None:
        diff = """\
--- a/src/foo.py
+++ b/src/foo.py
@@ -1,3 +1,3 @@
-def old():
+def new():
     pass
"""
        findings = review_diff(diff)
        assert len(findings) == 0

    def test_detects_hardcoded_secret(self) -> None:
        diff = """\
--- a/config.py
+++ b/config.py
@@ -1,1 +1,2 @@
+api_key = "FAKE_KEY_FOR_TEST"
"""
        findings = review_diff(diff)
        assert any(f.severity == "critical" and "secret" in f.description.lower() for f in findings)

    def test_detects_breakpoint_in_production(self) -> None:
        diff = """\
--- a/src/handler.py
+++ b/src/handler.py
@@ -1,1 +1,2 @@
+    breakpoint()
"""
        findings = review_diff(diff)
        assert any(f.severity == "major" and "breakpoint" in f.description.lower() for f in findings)

    def test_ignores_breakpoint_in_test_files(self) -> None:
        diff = """\
--- a/test_handler.py
+++ b/test_handler.py
@@ -1,1 +1,2 @@
+    breakpoint()
"""
        findings = review_diff(diff)
        assert not any(f.severity == "major" for f in findings)

    def test_detects_todo_markers(self) -> None:
        diff = """\
--- a/src/foo.py
+++ b/src/foo.py
@@ -1,1 +1,2 @@
+    # TODO: fix this later
"""
        findings = review_diff(diff)
        assert any(f.severity == "info" and "TODO" in f.description for f in findings)


class TestBuildReviewBody:
    """Tests for build_review_body()."""

    def test_all_green_approves(self) -> None:
        gate = {
            "CI green": {"passed": True, "details": "All checks passed", "blocked": False},
            "MERGEABLE": {"passed": True, "details": "Clean", "blocked": False},
        }
        verdict, body = build_review_body(gate, [])
        assert verdict == "APPROVE"
        assert "APPROVE" in body

    def test_blocked_gate_requests_changes(self) -> None:
        gate = {
            "CI green": {"passed": False, "details": "Failing", "blocked": True},
            "MERGEABLE": {"passed": True, "details": "Clean", "blocked": False},
        }
        verdict, body = build_review_body(gate, [])
        assert verdict == "REQUEST_CHANGES"
        assert "CI green" in body

    def test_critical_finding_requests_changes(self) -> None:
        gate = {
            "CI green": {"passed": True, "details": "Passed", "blocked": False},
        }
        findings = [ReviewFinding(FindingSeverity.CRITICAL, "config.py", "Hardcoded secret")]
        verdict, body = build_review_body(gate, findings)
        assert verdict == "REQUEST_CHANGES"
        assert "Hardcoded secret" in body

    def test_minor_finding_still_approves(self) -> None:
        gate = {
            "CI green": {"passed": True, "details": "Passed", "blocked": False},
        }
        findings = [ReviewFinding(FindingSeverity.INFO, "foo.py", "TODO marker")]
        verdict, body = build_review_body(gate, findings)
        assert verdict == "APPROVE"


class TestFormatMailFindings:
    """Tests for format_mail_findings() MCP mail body."""

    def test_no_issues(self) -> None:
        gate = {"CI green": {"passed": True, "details": "OK", "blocked": False}}
        body = format_mail_findings(gate, [], 42)
        assert "No issues" in body
        assert "PR #42" in body

    def test_gate_blockers_in_mail(self) -> None:
        gate = {"CI green": {"passed": False, "details": "Failing check: lint", "blocked": True}}
        body = format_mail_findings(gate, [], 42)
        assert "Gate Blockers" in body
        assert "CI green" in body

    def test_critical_findings_in_mail(self) -> None:
        gate = {"CI green": {"passed": True, "details": "OK", "blocked": False}}
        findings = [ReviewFinding(FindingSeverity.CRITICAL, "config.py", "Hardcoded API key")]
        body = format_mail_findings(gate, findings, 42)
        assert "Code Issues" in body
        assert "Hardcoded API key" in body


class TestCheckGate:
    """Tests for check_gate() wrapper."""

    @patch("orchestration.merge_gate.check_merge_ready")
    def test_returns_dict_of_conditions(self, mock_check) -> None:
        from orchestration.merge_gate import ConditionResult, MergeVerdict

        mock_check.return_value = MergeVerdict(
            pr_url="https://github.com/o/r/pull/1",
            can_merge=True,
            conditions=[
                ConditionResult("CI green", True, "All passed"),
                ConditionResult("MERGEABLE", True, "Clean"),
            ],
        )
        result = check_gate("o", "r", 1)
        assert "CI green" in result
        assert result["CI green"]["passed"] is True
        assert "MERGEABLE" in result


class TestRunReview:
    """Tests for run_review() end-to-end."""

    @patch("orchestration.reviewer_agent.post_github_review")
    @patch("orchestration.reviewer_agent.get_pr_diff")
    @patch("orchestration.reviewer_agent.check_gate")
    def test_clean_pr_approves(self, mock_gate, mock_diff, mock_post) -> None:
        mock_gate.return_value = {
            "CI green": {"passed": True, "details": "All passed", "blocked": False},
            "MERGEABLE": {"passed": True, "details": "Clean", "blocked": False},
            "CodeRabbit approved": {"passed": True, "details": "APPROVED", "blocked": False},
            "No blocking comments": {"passed": True, "details": "None", "blocked": False},
            "Evidence review": {"passed": True, "details": "PASS", "blocked": False},
        }
        mock_diff.return_value = "+++ b/foo.py\n+def hello(): pass\n"
        mock_post.return_value = (True, "APPROVE")

        result = run_review("o", "r", 1)
        assert result.verdict == "APPROVE"
        assert result.gate_passed is True
        assert result.actual_event == "APPROVE"
        mock_post.assert_called_once()

    @patch("orchestration.reviewer_agent.post_github_review")
    @patch("orchestration.reviewer_agent.get_pr_diff")
    @patch("orchestration.reviewer_agent.check_gate")
    def test_blocked_gate_requests_changes(self, mock_gate, mock_diff, mock_post) -> None:
        mock_gate.return_value = {
            "CI green": {"passed": False, "details": "Failing", "blocked": True},
        }
        mock_diff.return_value = "+++ b/foo.py\n+pass\n"
        mock_post.return_value = (True, "REQUEST_CHANGES")

        result = run_review("o", "r", 1)
        assert result.verdict == "REQUEST_CHANGES"
        assert result.gate_passed is False

    @patch("orchestration.reviewer_agent.post_github_review")
    @patch("orchestration.reviewer_agent.get_pr_diff")
    @patch("orchestration.reviewer_agent.check_gate")
    def test_no_post_dry_run(self, mock_gate, mock_diff, mock_post) -> None:
        mock_gate.return_value = {
            "CI green": {"passed": True, "details": "OK", "blocked": False},
        }
        mock_diff.return_value = "+++ b/foo.py\n+pass\n"

        result = run_review("o", "r", 1, post_github=False)
        assert result.verdict == "APPROVE"
        assert result.github_posted is False
        mock_post.assert_not_called()

    @patch("orchestration.reviewer_agent.post_github_review")
    @patch("orchestration.reviewer_agent.get_pr_diff")
    @patch("orchestration.reviewer_agent.check_gate")
    def test_github_posted_true_on_success(self, mock_gate, mock_diff, mock_post) -> None:
        mock_gate.return_value = {
            "CI green": {"passed": True, "details": "OK", "blocked": False},
        }
        mock_diff.return_value = "+++ b/foo.py\n+pass\n"
        mock_post.return_value = (True, "APPROVE")

        result = run_review("o", "r", 1)
        assert result.github_posted is True

    @patch("orchestration.reviewer_agent.post_github_review")
    @patch("orchestration.reviewer_agent.get_pr_diff")
    @patch("orchestration.reviewer_agent.check_gate")
    def test_github_posted_false_on_failure(self, mock_gate, mock_diff, mock_post) -> None:
        mock_gate.return_value = {
            "CI green": {"passed": True, "details": "OK", "blocked": False},
        }
        mock_diff.return_value = "+++ b/foo.py\n+pass\n"
        mock_post.return_value = (False, "")

        result = run_review("o", "r", 1)
        assert result.github_posted is False


class TestSelfReviewFallback:
    """Verdict comment must NOT be posted when review is downgraded to COMMENT."""

    @patch("orchestration.reviewer_agent._post_verdict_comment")
    @patch("orchestration.reviewer_agent._run_gh")
    @patch("orchestration.reviewer_agent.get_pr_diff")
    @patch("orchestration.reviewer_agent.check_gate")
    def test_self_review_fallback_skips_verdict_comment(
        self, mock_gate, mock_diff, mock_run_gh, mock_post_verdict,
    ) -> None:
        """When APPROVE is downgraded to COMMENT (self-review), no verdict marker is posted."""
        mock_gate.return_value = {
            "CI green": {"passed": True, "details": "OK", "blocked": False},
        }
        mock_diff.return_value = "+++ b/foo.py\n+pass\n"

        # _run_gh calls in run_review (after check_gate + get_pr_diff are mocked):
        # 1. PR HEAD SHA fetch
        # 2. TOCTOU re-check (same SHA)
        # 3. post_github_review: APPROVE fails (self-review)
        # 4. post_github_review: COMMENT fallback succeeds
        mock_run_gh.side_effect = [  # type: ignore[attr-defined]
            (0, "abc123sha", ""),       # PR HEAD SHA fetch
            (0, "abc123sha", ""),       # TOCTOU re-check (unchanged)
            (1, "", "cannot approve own PR"),  # post_github_review APPROVE → fails
            (0, '{"id": 1}', ""),       # post_github_review COMMENT fallback → succeeds
        ]

        result = run_review("o", "r", 1, post_github=True)

        # Review was posted as COMMENT (fallback), not APPROVE
        assert result.actual_event == "COMMENT"
        # Verdict comment must NOT have been posted (self-review path)
        assert result.verdict_comment_posted is False
        mock_post_verdict.assert_not_called()  # type: ignore[attr-defined]

    @patch("orchestration.reviewer_agent._post_verdict_comment")
    @patch("orchestration.reviewer_agent._run_gh")
    @patch("orchestration.reviewer_agent.get_pr_diff")
    @patch("orchestration.reviewer_agent.check_gate")
    def test_normal_approve_posts_verdict_comment(
        self, mock_gate, mock_diff, mock_run_gh, mock_post_verdict,
    ) -> None:
        """When APPROVE succeeds directly, verdict comment IS posted."""
        mock_gate.return_value = {
            "CI green": {"passed": True, "details": "OK", "blocked": False},
        }
        mock_diff.return_value = "+++ b/foo.py\n+pass\n"
        mock_post_verdict.return_value = True  # type: ignore[attr-defined]

        mock_run_gh.side_effect = [  # type: ignore[attr-defined]
            (0, "abc123sha", ""),       # PR HEAD SHA fetch
            (0, "abc123sha", ""),       # TOCTOU re-check (unchanged)
            (0, '{"id": 1}', ""),       # post_github_review APPROVE → succeeds
        ]

        result = run_review("o", "r", 1, post_github=True)

        assert result.actual_event == "APPROVE"
        assert result.verdict_comment_posted is True
        mock_post_verdict.assert_called_once()  # type: ignore[attr-defined]


class TestTOCTOUGuard:
    """Review must abort if PR HEAD moves between diff fetch and posting."""

    @patch("orchestration.reviewer_agent._post_verdict_comment")
    @patch("orchestration.reviewer_agent.post_github_review")
    @patch("orchestration.reviewer_agent._run_gh")
    @patch("orchestration.reviewer_agent.get_pr_diff")
    @patch("orchestration.reviewer_agent.check_gate")
    def test_head_moved_skips_post(
        self, mock_gate, mock_diff, mock_run_gh, mock_post_review, mock_post_verdict,
    ) -> None:
        """When HEAD moves during review, both review and verdict posts are skipped."""
        mock_gate.return_value = {
            "CI green": {"passed": True, "details": "OK", "blocked": False},
        }
        mock_diff.return_value = "+++ b/foo.py\n+pass\n"

        mock_run_gh.side_effect = [  # type: ignore[attr-defined]
            (0, "abc123sha", ""),       # PR HEAD SHA fetch
            (0, "def456sha", ""),       # TOCTOU re-check — HEAD moved!
        ]

        result = run_review("o", "r", 1, post_github=True)

        assert result.github_posted is False
        assert result.verdict_comment_posted is False
        mock_post_review.assert_not_called()  # type: ignore[attr-defined]
        mock_post_verdict.assert_not_called()  # type: ignore[attr-defined]

    @patch("orchestration.reviewer_agent._post_verdict_comment")
    @patch("orchestration.reviewer_agent.post_github_review")
    @patch("orchestration.reviewer_agent._run_gh")
    @patch("orchestration.reviewer_agent.get_pr_diff")
    @patch("orchestration.reviewer_agent.check_gate")
    def test_api_error_fails_closed(
        self, mock_gate, mock_diff, mock_run_gh, mock_post_review, mock_post_verdict,
    ) -> None:
        """When TOCTOU re-check API fails, skip posting (fail closed)."""
        mock_gate.return_value = {
            "CI green": {"passed": True, "details": "OK", "blocked": False},
        }
        mock_diff.return_value = "+++ b/foo.py\n+pass\n"

        mock_run_gh.side_effect = [  # type: ignore[attr-defined]
            (0, "abc123sha", ""),       # PR HEAD SHA fetch — OK
            (1, "", "API error"),       # TOCTOU re-check — API fails!
        ]

        result = run_review("o", "r", 1, post_github=True)

        assert result.github_posted is False
        assert result.verdict_comment_posted is False
        mock_post_review.assert_not_called()  # type: ignore[attr-defined]
        mock_post_verdict.assert_not_called()  # type: ignore[attr-defined]


class TestDowngradeScope:
    """Downgrade check should only block APPROVE markers, not REQUEST_CHANGES."""

    @patch("orchestration.reviewer_agent._post_verdict_comment")
    @patch("orchestration.reviewer_agent._run_gh")
    @patch("orchestration.reviewer_agent.get_pr_diff")
    @patch("orchestration.reviewer_agent.check_gate")
    def test_request_changes_marker_still_posts_when_downgraded(
        self, mock_gate, mock_diff, mock_run_gh, mock_post_verdict,
    ) -> None:
        """REQUEST_CHANGES downgraded to COMMENT should still post verdict marker."""
        mock_gate.return_value = {
            "CI green": {"passed": False, "details": "Failing", "blocked": True},
        }
        mock_diff.return_value = "+++ b/foo.py\n+pass\n"
        mock_post_verdict.return_value = True  # type: ignore[attr-defined]

        mock_run_gh.side_effect = [  # type: ignore[attr-defined]
            (0, "abc123sha", ""),       # PR HEAD SHA fetch
            (0, "abc123sha", ""),       # TOCTOU re-check (unchanged)
            (1, "", "permission denied"),  # post_github_review REQUEST_CHANGES → fails
            (0, '{"id": 1}', ""),       # post_github_review COMMENT fallback → succeeds
        ]

        result = run_review("o", "r", 1, post_github=True)

        assert result.verdict == "REQUEST_CHANGES"
        assert result.actual_event == "COMMENT"
        # REQUEST_CHANGES verdict marker SHOULD still be posted (not blocked by downgrade)
        mock_post_verdict.assert_called_once()  # type: ignore[attr-defined]
        assert result.verdict_comment_posted is True


class TestIsTestPath:
    """Tests for _is_test_path() heuristic."""

    def test_test_prefix(self) -> None:
        assert _is_test_path("test_handler.py") is True

    def test_tests_dir(self) -> None:
        assert _is_test_path("src/tests/test_foo.py") is True

    def test_test_dir(self) -> None:
        assert _is_test_path("test/integration.py") is True

    def test_test_suffix(self) -> None:
        assert _is_test_path("src/handler_test.py") is True

    def test_no_false_positive_contest(self) -> None:
        assert _is_test_path("src/contest/solver.py") is False

    def test_no_false_positive_latest(self) -> None:
        assert _is_test_path("src/latest/handler.py") is False

    def test_root_test_py(self) -> None:
        assert _is_test_path("test.py") is True

    def test_root_tests_py(self) -> None:
        assert _is_test_path("tests.py") is True

    def test_production_file(self) -> None:
        assert _is_test_path("src/orchestration/merge_gate.py") is False
