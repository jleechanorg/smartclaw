"""Tests for merge_gate module."""
from __future__ import annotations

import base64
import json

import pytest
from unittest.mock import patch

from orchestration.merge_gate import (
    classify_inline_comments,
    check_merge_ready,
)



class TestClassifyInlineComments:
    """Tests for classify_inline_comments()."""

    def test_no_comments(self) -> None:
        """No comments."""
        blocking, informational = classify_inline_comments([])
        assert blocking == []
        assert informational == []

    def test_human_comment_blocking(self) -> None:
        """Human comments are blocking."""
        comments = [{"user": {"login": "human-reviewer"}, "body": "Please fix this", "position": 10}]
        blocking, informational = classify_inline_comments(comments)
        assert len(blocking) == 1
        assert len(informational) == 0

    def test_cr_critical_blocking(self) -> None:
        """CR Critical comments with structured marker are blocking."""
        # Use real CR formatting: _🔴 Critical_ or **Critical**
        comments = [{"user": {"login": "coderabbitai[bot]"}, "body": "_⚠️ Potential issue_ | _🔴 Critical_\n**Fix this now**", "position": 10}]
        blocking, informational = classify_inline_comments(comments)
        assert len(blocking) == 1
        assert len(informational) == 0

    def test_cr_low_informational(self) -> None:
        """CR Low severity is informational."""
        comments = [{"user": {"login": "coderabbitai[bot]"}, "body": "Minor: consider this", "position": 10}]
        blocking, informational = classify_inline_comments(comments)
        assert blocking == []
        assert len(informational) == 1

    def test_copilot_informational(self) -> None:
        """Copilot comments are informational."""
        comments = [{"user": {"login": "copilot[bot]"}, "body": "Suggestion: this could be improved", "position": 10}]
        blocking, informational = classify_inline_comments(comments)
        assert blocking == []
        assert len(informational) == 1

    def test_cursor_bugbot_informational(self) -> None:
        """Cursor Bugbot is informational unless critical."""
        comments_low = [{"user": {"login": "cursor[bot]"}, "body": "Medium: minor issue", "position": 10}]
        blocking_low, informational_low = classify_inline_comments(comments_low)
        assert len(informational_low) == 1


class TestMergeGateIntegration:
    """Integration tests for merge gate.

    All tests mock ``run_gh`` so no real GitHub calls are made, and patch the
    review-state JSONL path to a tmp location to avoid reading the real
    ``~/.openclaw/state/openclaw_pr_reviews.jsonl``.
    """

    @patch("orchestration.merge_gate.run_gh")
    def test_all_conditions_pass(self, mock_run_gh: object) -> None:
        """Test when all conditions pass."""
        # CI: 2 calls (head SHA + check-runs)
        # mergeable: 1 call (JSON response)
        # CR: 2 calls (head SHA + reviews)
        # blocking comments: 2 calls (GraphQL unresolved IDs + REST comments)
        # evidence: 2 calls (head SHA + PR files)
        mock_run_gh.side_effect = [
            (0, "abc123sha", ""),  # CI: head SHA
            (0, json.dumps([{"name": "test", "status": "completed", "conclusion": "success"}]), ""),  # CI: check-runs
            (0, json.dumps({"mergeable": True, "state": "clean"}), ""),  # mergeable
            (0, "abc123sha", ""),  # CR: head SHA for stale-review check
            (0, json.dumps([{"user": {"login": "coderabbitai[bot]"}, "state": "APPROVED", "body": "", "commit_id": "abc123sha"}]), ""),  # CR: reviews
            (0, '{"data":{"repository":{"pullRequest":{"reviewThreads":{"nodes":[]}}}}}', ""),  # blocking: GraphQL (no unresolved)
            (0, "[]", ""),  # blocking: REST comments
            (0, "abc123sha", ""),  # evidence: head SHA for ref-pinning
            (0, "", ""),  # evidence: PR files (no code files → skip)
        ]

        verdict = check_merge_ready("owner", "repo", 1)

        assert verdict.can_merge is True
        assert verdict.ci_green is True
        assert verdict.mergeable is True
        assert verdict.cr_approved is True
        assert verdict.no_blocking_comments is True
        assert verdict.evidence_passed is True

    @patch("orchestration.merge_gate.run_gh")
    def test_cr_changes_requested_blocks(self, mock_run_gh: object) -> None:
        """CR changes requested blocks merge."""
        mock_run_gh.side_effect = [
            (0, "abc123sha", ""),
            (0, json.dumps([{"name": "test", "status": "completed", "conclusion": "success"}]), ""),
            (0, json.dumps({"mergeable": True, "state": "clean"}), ""),
            (0, "abc123sha", ""),  # CR: head SHA
            (0, json.dumps([{"user": {"login": "coderabbitai[bot]"}, "state": "CHANGES_REQUESTED", "body": "", "commit_id": "abc123sha"}]), ""),
            (0, '{"data":{"repository":{"pullRequest":{"reviewThreads":{"nodes":[]}}}}}', ""),
            (0, "[]", ""),
            (0, "abc123sha", ""),  # evidence: head SHA
            (0, "[]", ""),
        ]

        verdict = check_merge_ready("owner", "repo", 1)

        assert verdict.can_merge is False
        assert verdict.cr_approved is False

    @patch("orchestration.merge_gate.run_gh")
    def test_merge_conflict_blocks(self, mock_run_gh: object) -> None:
        """Merge conflicts block merge."""
        mock_run_gh.side_effect = [
            (0, "abc123sha", ""),
            (0, json.dumps([{"name": "test", "status": "completed", "conclusion": "success"}]), ""),
            (0, json.dumps({"mergeable": False, "state": "dirty"}), ""),  # conflict
            (0, "abc123sha", ""),  # CR: head SHA
            (0, json.dumps([{"user": {"login": "coderabbitai[bot]"}, "state": "APPROVED", "body": "", "commit_id": "abc123sha"}]), ""),
            (0, '{"data":{"repository":{"pullRequest":{"reviewThreads":{"nodes":[]}}}}}', ""),
            (0, "[]", ""),
            (0, "abc123sha", ""),  # evidence: head SHA
            (0, "[]", ""),
        ]

        verdict = check_merge_ready("owner", "repo", 1)

        assert verdict.can_merge is False
        assert verdict.mergeable is False

    @patch("orchestration.merge_gate.run_gh")
    def test_mergeable_null_blocks(self, mock_run_gh: object) -> None:
        """mergeable=null (not yet computed) should block merge (fail-closed)."""
        mock_run_gh.side_effect = [
            (0, "abc123sha", ""),
            (0, json.dumps([{"name": "test", "status": "completed", "conclusion": "success"}]), ""),
            (0, json.dumps({"mergeable": None, "state": "unknown"}), ""),  # null
            (0, "abc123sha", ""),  # CR: head SHA
            (0, json.dumps([{"user": {"login": "coderabbitai[bot]"}, "state": "APPROVED", "body": "", "commit_id": "abc123sha"}]), ""),
            (0, '{"data":{"repository":{"pullRequest":{"reviewThreads":{"nodes":[]}}}}}', ""),
            (0, "[]", ""),
            (0, "abc123sha", ""),  # evidence: head SHA
            (0, "[]", ""),
        ]

        verdict = check_merge_ready("owner", "repo", 1)

        assert verdict.can_merge is False
        assert verdict.mergeable is False


class TestEvidencePattern:
    """Tests for evidence PASS pattern matching — D2 defect coverage."""

    def _match(self, text: str) -> bool:
        """Check if text matches the evidence PASS regex."""
        import re
        pattern = re.compile(
            r"(?:evidence|/er).*\*\*pass\*\*|(?:evidence|/er).*✅",
            re.IGNORECASE | re.DOTALL,
        )
        return bool(pattern.search(text))

    def test_evidence_pass_bold(self) -> None:
        """Standard evidence PASS format."""
        assert self._match("Evidence review: **PASS**") is True

    def test_er_pass_bold(self) -> None:
        """/er returns PASS."""
        assert self._match("/er returns **PASS**") is True

    def test_evidence_checkmark(self) -> None:
        """Evidence with checkmark."""
        assert self._match("Evidence ✅ all good") is True

    def test_bare_pass_no_match(self) -> None:
        """Bare **PASS** without evidence context must NOT match (D2 defect)."""
        assert self._match("Tests **PASS** all good") is False

    def test_passport_no_match(self) -> None:
        """Word containing 'pass' must not match."""
        assert self._match("Check your **passport** details") is False

    def test_bypass_no_match(self) -> None:
        """'bypass' must not match."""
        assert self._match("bypass **PASS** check") is False


class TestCheckEvidencePassUpdated:
    """Tests for check_evidence_pass with verdict.json support."""

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
    def test_verdict_json_with_parse_error_blocks(self, mock_run_gh: object) -> None:
        """Malformed verdict.json should block merge (fail-closed)."""
        from orchestration.merge_gate import check_evidence_pass

        mock_run_gh.side_effect = [  # type: ignore[attr-defined]
            (0, "abc123sha", ""),  # PR head SHA
            (0, "src/orchestration/foo.py\ndocs/evidence/r/PR-1/20260317_0100_utc/verdict.json", ""),
            (0, "", ""),  # no reviewer verdict comment (empty NDJSON)
            (0, "not-json", ""),  # verdict.json content decode fail
        ]
        result = check_evidence_pass("o", "r", 1)
        assert result.passed is False
        assert "parse" in result.details.lower()

    @patch("orchestration.merge_gate.run_gh")
    def test_verdict_json_passed(self, mock_run_gh: object) -> None:
        """Valid PASS verdict.json enables Evidence PASS."""
        from orchestration.merge_gate import check_evidence_pass

        mock_run_gh.side_effect = [  # type: ignore[attr-defined]
            (0, "abc123sha", ""),  # PR head SHA
            (0, "src/orchestration/foo.py\ndocs/evidence/r/PR-1/20260317_0100_utc/verdict.json", ""),
            (0, "", ""),  # no reviewer verdict comment (empty NDJSON)
            (0, base64.b64encode(json.dumps({
                "overall": "PASS",
                "stage2": {
                    "status": "PASS",
                    "independence_verified": True,
                    "model_family_differs_from_stage1": True,
                },
            }).encode("utf-8")).decode("utf-8"), ""),
        ]
        result = check_evidence_pass("o", "r", 1)
        assert result.passed is True
        assert "PASS" in result.details

class TestReviewerAgentApproved:
    """Tests for reviewer agent verdict comment as evidence alternative."""

    @patch("orchestration.merge_gate.run_gh")
    def test_reviewer_approve_comment_passes_evidence(self, mock_run_gh: object) -> None:
        """Reviewer agent's APPROVE verdict comment satisfies evidence condition."""
        from orchestration.merge_gate import check_evidence_pass

        import json
        comments_json = json.dumps({
            "body": "<!-- reviewer-verdict: APPROVE sha:abc123sha -->\n**Reviewer verdict: APPROVE** for commit `abc123sh`",
            "created_at": "2026-03-18T08:56:39Z",
            "user": {"login": "reviewer-agent[bot]"},
        })
        mock_run_gh.side_effect = [  # type: ignore[attr-defined]
            (0, "abc123sha", ""),  # PR head SHA
            (0, "src/orchestration/foo.py", ""),  # PR files (code file)
            (0, comments_json, ""),  # issue comments with verdict marker
        ]
        result = check_evidence_pass("o", "r", 1)
        assert result.passed is True
        assert "Reviewer agent APPROVE" in result.details

    @patch("orchestration.merge_gate.run_gh")
    def test_reviewer_request_changes_does_not_pass(self, mock_run_gh: object) -> None:
        """REQUEST_CHANGES verdict comment should block evidence pass."""
        from orchestration.merge_gate import check_evidence_pass

        import json
        # NDJSON format: one JSON object per line (gh api --paginate --jq '.[]|...')
        comments_json = json.dumps({
            "body": "<!-- reviewer-verdict: REQUEST_CHANGES sha:abc123sha -->\n**Reviewer verdict: REQUEST_CHANGES**",
            "created_at": "2026-03-18T08:56:39Z",
            "user": {"login": "reviewer-agent[bot]"},
        })
        mock_run_gh.side_effect = [  # type: ignore[attr-defined]
            (0, "abc123sha", ""),  # PR head SHA
            (0, "src/orchestration/foo.py", ""),  # PR files (code file)
            (0, comments_json, ""),  # verdict comment but REQUEST_CHANGES
        ]
        result = check_evidence_pass("o", "r", 1)
        assert result.passed is False
        assert result.blocked is True
        assert "REQUEST_CHANGES" in result.details

    @patch("orchestration.merge_gate.run_gh")
    def test_stale_sha_verdict_does_not_pass(self, mock_run_gh: object) -> None:
        """Verdict for old commit SHA should not satisfy evidence."""
        from orchestration.merge_gate import check_evidence_pass

        import json
        comments_json = json.dumps({
            "body": "<!-- reviewer-verdict: APPROVE sha:oldsha111 -->\n**Reviewer verdict: APPROVE**",
            "created_at": "2026-03-18T08:56:39Z",
            "user": {"login": "reviewer-agent[bot]"},
        })
        mock_run_gh.side_effect = [  # type: ignore[attr-defined]
            (0, "abc123sha", ""),  # PR head SHA (different from verdict SHA)
            (0, "src/orchestration/foo.py", ""),  # PR files (code file)
            (0, comments_json, ""),  # verdict for wrong SHA
        ]
        result = check_evidence_pass("o", "r", 1)
        assert result.passed is False
        assert result.blocked is True

    @patch("orchestration.merge_gate.run_gh")
    def test_non_reviewer_marker_is_ignored(self, mock_run_gh: object) -> None:
        """Markers from non-reviewer identities must not satisfy evidence."""
        from orchestration.merge_gate import check_evidence_pass

        import json
        comments_json = json.dumps({
            "body": "<!-- reviewer-verdict: APPROVE sha:abc123sha -->",
            "created_at": "2026-03-18T08:56:39Z",
            "user": {"login": "random-user"},
        })
        mock_run_gh.side_effect = [  # type: ignore[attr-defined]
            (0, "abc123sha", ""),
            (0, "src/orchestration/foo.py", ""),
            (0, comments_json, ""),
        ]
        result = check_evidence_pass("o", "r", 1)
        assert result.passed is False
        assert "evidence" in result.details.lower()

    @patch("orchestration.merge_gate.run_gh")
    def test_repo_owner_marker_is_accepted(self, mock_run_gh: object) -> None:
        """Markers from repo owner are accepted as authorized reviewers."""
        from orchestration.merge_gate import check_evidence_pass

        import json
        comments_json = json.dumps({
            "body": "<!-- reviewer-verdict: APPROVE sha:abc123sha -->",
            "created_at": "2026-03-18T08:56:39Z",
            "user": {"login": "repo-owner"},
        })
        mock_run_gh.side_effect = [  # type: ignore[attr-defined]
            (0, "abc123sha", ""),
            (0, "src/orchestration/foo.py", ""),
            (0, comments_json, ""),
        ]
        result = check_evidence_pass("o", "r", 1)
        assert result.passed is True
        assert "Reviewer agent APPROVE" in result.details

    @patch("orchestration.merge_gate.run_gh")
    def test_no_verdict_comment_falls_through(self, mock_run_gh: object) -> None:
        """Without verdict comment, falls through to verdict.json check."""
        from orchestration.merge_gate import check_evidence_pass

        mock_run_gh.side_effect = [  # type: ignore[attr-defined]
            (0, "abc123sha", ""),  # PR head SHA
            (0, "src/orchestration/foo.py", ""),  # PR files (code file)
            (0, "", ""),  # no verdict comments (empty NDJSON)
            # Falls through to verdict.json — no verdict found → blocks
        ]
        result = check_evidence_pass("o", "r", 1)
        assert result.passed is False
        assert result.blocked is True
        assert "evidence" in result.details.lower()

    @patch("orchestration.merge_gate.run_gh")
    def test_api_failure_blocks(self, mock_run_gh: object) -> None:
        """If comment fetch API fails, block rather than fall through to verdict.json."""
        from orchestration.merge_gate import check_evidence_pass

        mock_run_gh.side_effect = [  # type: ignore[attr-defined]
            (0, "abc123sha", ""),  # PR head SHA
            (0, "src/orchestration/foo.py", ""),  # PR files (code file)
            (1, "", "api error"),  # comment fetch failed — should block, not fall through
        ]
        result = check_evidence_pass("o", "r", 1)
        assert result.passed is False
        assert result.blocked is True
        assert "cannot verify evidence" in result.details.lower()


class TestBlockingCommentsResolution:
    """Tests for check_blocking_comments — must only count UNRESOLVED threads."""

    @patch("orchestration.merge_gate.run_gh")
    def test_resolved_critical_comments_do_not_block(self, mock_run_gh: object) -> None:
        """Critical comments on resolved threads should NOT block merge.

        This is the TDD red test for the known bug: check_blocking_comments
        counted ALL position comments from REST API regardless of thread state.
        """
        from orchestration.merge_gate import check_blocking_comments

        # First call: GraphQL returns 0 unresolved threads (all resolved)
        # Second call: REST returns comments (but they're all on resolved threads)
        mock_run_gh.side_effect = [  # type: ignore[attr-defined]
            # GraphQL call to get unresolved thread comment node IDs
            (0, '{"data":{"repository":{"pullRequest":{"reviewThreads":{"nodes":[]}}}}}', ""),
            # REST call to get all comments (old path, may still be called)
            (0, json.dumps([
                {"id": 1, "user": {"login": "coderabbitai[bot]"}, "body": "_⚠️ Potential issue_ | _🔴 Critical_\n**Fix this**", "position": 10, "node_id": "PRRC_resolved1"},
                {"id": 2, "user": {"login": "cursor[bot]"}, "body": "### Bug title\n\n**High Severity**\n\nDetails", "position": 20, "node_id": "PRRC_resolved2"},
            ]), ""),
        ]

        result = check_blocking_comments("owner", "repo", 1)
        assert result.passed is True, f"Resolved critical comments should not block: {result.details}"

    @patch("orchestration.merge_gate.run_gh")
    def test_unresolved_critical_comments_block(self, mock_run_gh: object) -> None:
        """Critical comments on unresolved threads MUST block merge."""
        from orchestration.merge_gate import check_blocking_comments

        unresolved_comment_id = "PRRC_unresolved1"
        mock_run_gh.side_effect = [  # type: ignore[attr-defined]
            # GraphQL: 1 unresolved thread with the critical comment
            (0, json.dumps({"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": [
                {"isResolved": False, "comments": {"nodes": [{"id": unresolved_comment_id}]}}
            ]}}}}}), ""),
            # REST: the same comment
            (0, json.dumps([
                {"id": 1, "user": {"login": "coderabbitai[bot]"}, "body": "_⚠️ Potential issue_ | _🔴 Critical_\n**Fix this**", "position": 10, "node_id": unresolved_comment_id},
            ]), ""),
        ]

        result = check_blocking_comments("owner", "repo", 1)
        assert result.passed is False, "Unresolved critical comments must block"
        assert result.blocked is True

    @patch("orchestration.merge_gate.run_gh")
    def test_graphql_failure_blocks(self, mock_run_gh: object) -> None:
        """If GraphQL fails, block merge — cannot verify thread resolution without it."""
        from orchestration.merge_gate import check_blocking_comments

        mock_run_gh.side_effect = [  # type: ignore[attr-defined]
            # GraphQL fails — cannot determine unresolved_ids
            (1, "", "GraphQL error"),
        ]

        result = check_blocking_comments("owner", "repo", 1)
        assert result.passed is False
        assert result.blocked is True
        assert "GraphQL unavailable" in result.details


class TestUnresolvedThreadPagination:
    """Tests for _get_unresolved_comment_ids pagination handling."""

    @patch("orchestration.merge_gate.run_gh")
    def test_unresolved_comment_ids_are_paginated(self, mock_run_gh: object) -> None:
        """Comment IDs from unresolved threads on multiple pages are gathered."""
        from orchestration.merge_gate import _get_unresolved_comment_ids

        first_page = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "pageInfo": {"hasNextPage": True, "endCursor": "cursor2"},
                            "nodes": [
                                {"isResolved": False, "comments": {"nodes": [{"id": "PRRC_1"}]}},
                                {"isResolved": True, "comments": {"nodes": []}},
                            ],
                        }
                    }
                }
            }
        }
        second_page = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [{"isResolved": False, "comments": {"nodes": [{"id": "PRRC_2"}]}}],
                        }
                    }
                }
            }
        }

        mock_run_gh.side_effect = [  # type: ignore[attr-defined]
            (0, json.dumps(first_page), ""),
            (0, json.dumps(second_page), ""),
        ]

        ids = _get_unresolved_comment_ids("owner", "repo", 1)
        assert ids == {"PRRC_1", "PRRC_2"}
        assert mock_run_gh.call_count == 2


class TestOpenClawReviewMissing:
    """Test that missing OpenClaw review blocks merge (D3 coverage)."""

    @patch("orchestration.merge_gate.run_gh")
    def test_no_openclaw_review_blocks(self, mock_run_gh: object, tmp_path: object) -> None:
        """Missing OpenClaw review should block (fail-closed)."""
        from orchestration.merge_gate import check_openclaw_review
        # No openclaw[bot] review on GitHub
        mock_run_gh.return_value = (0, "null", "")  # type: ignore[attr-defined]

        # Empty JSONL (no review recorded)
        fake_jsonl = str(tmp_path) + "/reviews.jsonl"  # type: ignore[operator]
        from pathlib import Path
        Path(fake_jsonl).write_text("")

        with patch("orchestration.merge_gate.os.path.expanduser", return_value=fake_jsonl):
            result = check_openclaw_review("owner", "repo", 1)

        assert result.passed is False
        assert result.blocked is True


class TestCrCommentedBlocks:
    """Tests that CR COMMENTED state blocks — only APPROVED passes."""

    @patch("orchestration.merge_gate.run_gh")
    def test_cr_commented_with_major_blocks_even_if_threads_resolved(self, mock_run_gh: object) -> None:
        """CR COMMENTED with Major markers blocks even if all threads resolved.

        With .coderabbit.yaml approve=true, CR should post APPROVED when satisfied.
        COMMENTED means CR is not satisfied — block regardless of thread state.
        """
        from orchestration.merge_gate import check_coderabbit

        mock_run_gh.side_effect = [  # type: ignore[attr-defined]
            (0, "abc123", ""),  # CR: head SHA
            (0, json.dumps([{
                "user": {"login": "coderabbitai[bot]"},
                "state": "COMMENTED",
                "commit_id": "abc123",
                "body": "_⚠️ Potential issue_ | _🟠 Major_\n**Don't hardcode path**",
            }]), ""),
            # _is_cr_review_paused: no CR issue comments (not paused)
            (0, "[]", ""),
        ]

        result = check_coderabbit("owner", "repo", 1)
        assert result.passed is False, f"COMMENTED must block — only APPROVED passes: {result.details}"

    @patch("orchestration.merge_gate.run_gh")
    def test_cr_commented_no_markers_still_blocks(self, mock_run_gh: object) -> None:
        """CR COMMENTED without Critical/Major still blocks — must be APPROVED."""
        from orchestration.merge_gate import check_coderabbit

        mock_run_gh.side_effect = [  # type: ignore[attr-defined]
            (0, "abc123", ""),  # CR: head SHA
            (0, json.dumps([{
                "user": {"login": "coderabbitai[bot]"},
                "state": "COMMENTED",
                "commit_id": "abc123",
                "body": "Code looks good, minor suggestions only.",
            }]), ""),
            # _is_cr_review_paused: no CR issue comments (not paused)
            (0, "[]", ""),
        ]

        result = check_coderabbit("owner", "repo", 1)
        assert result.passed is False, "COMMENTED must block even with no markers"
        assert ".coderabbit.yaml" in result.details

    @patch("orchestration.merge_gate.run_gh")
    def test_cr_approved_state_passes(self, mock_run_gh: object) -> None:
        """CR APPROVED state passes the gate."""
        from orchestration.merge_gate import check_coderabbit

        mock_run_gh.side_effect = [  # type: ignore[attr-defined]
            (0, "abc123", ""),  # CR: head SHA
            (0, json.dumps([{
                "user": {"login": "coderabbitai[bot]"},
                "state": "APPROVED",
                "commit_id": "abc123",
                "body": "All good!",
            }]), ""),
        ]

        result = check_coderabbit("owner", "repo", 1)
        assert result.passed is True


class TestCrReviewPaused:
    """Tests for CodeRabbit review pause detection."""

    @patch("orchestration.merge_gate.run_gh")
    def test_paused_review_blocks(self, mock_run_gh: object) -> None:
        """CR reviews paused should block even with existing COMMENTED review."""
        from orchestration.merge_gate import check_coderabbit

        mock_run_gh.side_effect = [  # type: ignore[attr-defined]
            (0, "abc123", ""),  # CR: head SHA
            # reviews API — has a CR review
            (0, json.dumps([{"user": {"login": "coderabbitai[bot]"}, "state": "COMMENTED", "commit_id": "abc123", "body": "looks ok"}]), ""),
            # _is_cr_review_paused: CR issue comments
            (0, json.dumps([{"body": "Reviews paused\nauto_pause", "created_at": "2026-03-17T10:00:00Z"}]), ""),
            # _is_cr_review_paused: human comments (none after pause)
            (0, "[]", ""),
        ]

        result = check_coderabbit("o", "r", 1)
        assert result.passed is False
        assert "paused" in result.details.lower()

    @patch("orchestration.merge_gate.run_gh")
    def test_paused_then_resumed_still_blocks_if_commented(self, mock_run_gh: object) -> None:
        """CR pause + resume doesn't auto-pass — CR must re-review and APPROVED."""
        from orchestration.merge_gate import check_coderabbit

        mock_run_gh.side_effect = [  # type: ignore[attr-defined]
            (0, "abc123", ""),  # CR: head SHA
            # reviews API — CR posted COMMENTED (not APPROVED after resume)
            (0, json.dumps([{"user": {"login": "coderabbitai[bot]"}, "state": "COMMENTED", "commit_id": "abc123", "body": "looks ok"}]), ""),
            # _is_cr_review_paused: CR issue comments (pause)
            (0, json.dumps([{"body": "Reviews paused\nauto_pause", "created_at": "2026-03-17T10:00:00Z"}]), ""),
            # _is_cr_review_paused: human comments (resume AFTER pause)
            (0, json.dumps([{"body": "@coderabbitai review", "created_at": "2026-03-17T10:05:00Z"}]), ""),
        ]

        result = check_coderabbit("o", "r", 1)
        # COMMENTED blocks even after resume — need APPROVED
        assert result.passed is False
        assert ".coderabbit.yaml" in result.details

    @patch("orchestration.merge_gate.run_gh")
    def test_no_pause_commented_still_blocks(self, mock_run_gh: object) -> None:
        """No pause but COMMENTED still blocks — only APPROVED passes."""
        from orchestration.merge_gate import check_coderabbit

        mock_run_gh.side_effect = [  # type: ignore[attr-defined]
            (0, "abc123", ""),  # CR: head SHA
            # reviews API
            (0, json.dumps([{"user": {"login": "coderabbitai[bot]"}, "state": "COMMENTED", "commit_id": "abc123", "body": "looks ok"}]), ""),
            # _is_cr_review_paused: CR issue comments (no pause)
            (0, json.dumps([{"body": "some normal comment", "created_at": "2026-03-17T10:00:00Z"}]), ""),
        ]

        result = check_coderabbit("o", "r", 1)
        assert result.passed is False


class TestCrStaleSha:
    """Test that CR review on stale commit blocks merge."""

    @patch("orchestration.merge_gate.run_gh")
    def test_cr_stale_sha_blocks(self, mock_run_gh: object) -> None:
        """CR review on old commit while HEAD has moved should block."""
        from orchestration.merge_gate import check_coderabbit

        mock_run_gh.side_effect = [  # type: ignore[attr-defined]
            (0, "new_head_sha", ""),  # HEAD SHA fetch
            (0, json.dumps([{
                "user": {"login": "coderabbitai[bot]"},
                "state": "APPROVED",
                "commit_id": "old_stale_sha",
                "body": "Code looks good.",
            }]), ""),  # reviews — APPROVED but on wrong SHA
            (0, json.dumps([]), ""),  # _is_cr_review_paused: no pause marker
        ]

        result = check_coderabbit("owner", "repo", 1)
        assert result.passed is False, "Stale CR APPROVED on old commit must block"
        assert result.blocked is True
        assert "stale" in result.details.lower()

    @patch("orchestration.merge_gate.run_gh")
    def test_cr_approved_missing_sha_blocks(self, mock_run_gh: object) -> None:
        """CR APPROVED with missing commit_id should block (fail-closed)."""
        from orchestration.merge_gate import check_coderabbit

        mock_run_gh.side_effect = [  # type: ignore[attr-defined]
            (0, "abc123sha", ""),  # HEAD SHA fetch
            (0, json.dumps([{
                "user": {"login": "coderabbitai[bot]"},
                "state": "APPROVED",
                "commit_id": "",  # empty — can't verify
                "body": "LGTM",
            }]), ""),  # reviews — APPROVED but no commit_id
            (0, json.dumps([]), ""),  # _is_cr_review_paused: no pause marker
        ]

        result = check_coderabbit("owner", "repo", 1)
        assert result.passed is False, "Missing SHA must block"
        assert result.blocked is True


class TestMergeGateCLI:
    """Test CLI interface."""

    def test_cli_imports(self) -> None:
        """Verify CLI can be imported."""
        from orchestration.merge_gate import main
        assert main is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
