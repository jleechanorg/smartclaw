"""Tests for the generalized evidence packet schema (src/orchestration/evidence.py)."""

from __future__ import annotations

import pytest

from orchestration.evidence import (
    ArtifactType,
    EvidenceArtifact,
    EvidenceLevel,
    EvidencePacket,
    PipelineStage,
)


# ---------------------------------------------------------------------------
# EvidenceArtifact
# ---------------------------------------------------------------------------


class TestEvidenceArtifact:
    def test_valid_with_path(self) -> None:
        a = EvidenceArtifact(
            artifact_type=ArtifactType.EXECUTOR_LOG,
            summary="run log",
            path="/tmp/run.log",
        )
        assert a.is_valid()

    def test_valid_with_url(self) -> None:
        a = EvidenceArtifact(
            artifact_type=ArtifactType.DIFF,
            summary="pr diff",
            url="https://github.com/owner/repo/pull/1.diff",
        )
        assert a.is_valid()

    def test_invalid_without_locator(self) -> None:
        a = EvidenceArtifact(
            artifact_type=ArtifactType.HANDOFF,
            summary="no locator",
        )
        assert not a.is_valid()

    def test_timestamp_is_set(self) -> None:
        a = EvidenceArtifact(artifact_type=ArtifactType.CI_CHECK, summary="check")
        assert a.timestamp_utc  # non-empty

    def test_excerpt_optional(self) -> None:
        a = EvidenceArtifact(artifact_type=ArtifactType.QUEUE_RECORD, summary="q", path="/tmp/q")
        assert a.excerpt is None


# ---------------------------------------------------------------------------
# EvidencePacket.completeness
# ---------------------------------------------------------------------------


class TestEvidencePacketCompleteness:
    def _make_packet(self, types: list[ArtifactType]) -> EvidencePacket:
        ev = EvidencePacket(task_id="t-1", pipeline_stage=PipelineStage.EXECUTE)
        for t in types:
            ev.add_artifact(t, "test", url="https://example.com")
        return ev

    def test_complete_all_sections(self) -> None:
        ev = self._make_packet([
            ArtifactType.QUEUE_RECORD,
            ArtifactType.EXECUTOR_LOG,
            ArtifactType.DIFF,
            ArtifactType.HANDOFF,
        ])
        assert ev.completeness == EvidenceLevel.COMPLETE

    def test_complete_with_test_output_instead_of_diff(self) -> None:
        ev = self._make_packet([
            ArtifactType.QUEUE_RECORD,
            ArtifactType.EXECUTOR_LOG,
            ArtifactType.TEST_OUTPUT,
            ArtifactType.HANDOFF,
        ])
        assert ev.completeness == EvidenceLevel.COMPLETE

    def test_complete_with_ci_check_instead_of_diff(self) -> None:
        ev = self._make_packet([
            ArtifactType.QUEUE_RECORD,
            ArtifactType.EXECUTOR_LOG,
            ArtifactType.CI_CHECK,
            ArtifactType.HANDOFF,
        ])
        assert ev.completeness == EvidenceLevel.COMPLETE

    def test_partial_missing_handoff(self) -> None:
        ev = self._make_packet([
            ArtifactType.QUEUE_RECORD,
            ArtifactType.EXECUTOR_LOG,
            ArtifactType.DIFF,
        ])
        assert ev.completeness == EvidenceLevel.PARTIAL

    def test_partial_missing_artifact(self) -> None:
        ev = self._make_packet([
            ArtifactType.QUEUE_RECORD,
            ArtifactType.EXECUTOR_LOG,
        ])
        assert ev.completeness == EvidenceLevel.PARTIAL

    def test_missing_no_queue(self) -> None:
        ev = self._make_packet([
            ArtifactType.EXECUTOR_LOG,
            ArtifactType.DIFF,
            ArtifactType.HANDOFF,
        ])
        assert ev.completeness == EvidenceLevel.MISSING

    def test_missing_no_executor(self) -> None:
        ev = self._make_packet([
            ArtifactType.QUEUE_RECORD,
            ArtifactType.DIFF,
            ArtifactType.HANDOFF,
        ])
        assert ev.completeness == EvidenceLevel.MISSING

    def test_missing_empty(self) -> None:
        ev = self._make_packet([])
        assert ev.completeness == EvidenceLevel.MISSING

    def test_invalid_artifacts_do_not_count_toward_completeness(self) -> None:
        ev = EvidencePacket(task_id="t-invalid", pipeline_stage=PipelineStage.EXECUTE)
        ev.artifacts.append(EvidenceArtifact(ArtifactType.QUEUE_RECORD, "invalid queue"))
        ev.artifacts.append(EvidenceArtifact(ArtifactType.EXECUTOR_LOG, "invalid log"))
        ev.artifacts.append(EvidenceArtifact(ArtifactType.DIFF, "invalid diff"))
        ev.artifacts.append(EvidenceArtifact(ArtifactType.HANDOFF, "invalid handoff"))
        assert ev.completeness == EvidenceLevel.MISSING


# ---------------------------------------------------------------------------
# EvidencePacket.close + timestamps
# ---------------------------------------------------------------------------


class TestEvidencePacketClose:
    def test_close_sets_end_timestamp(self) -> None:
        ev = EvidencePacket(task_id="t-2", pipeline_stage=PipelineStage.PR_OPEN)
        assert ev.timestamp_end_utc == ""
        ev.close()
        assert ev.timestamp_end_utc != ""

    def test_start_timestamp_is_set_on_creation(self) -> None:
        ev = EvidencePacket(task_id="t-3", pipeline_stage=PipelineStage.INTAKE)
        assert ev.timestamp_start_utc != ""


# ---------------------------------------------------------------------------
# EvidencePacket.add_artifact
# ---------------------------------------------------------------------------


class TestAddArtifact:
    def test_add_artifact_returns_artifact(self) -> None:
        ev = EvidencePacket(task_id="t-4", pipeline_stage=PipelineStage.EXECUTE)
        art = ev.add_artifact(ArtifactType.QUEUE_RECORD, "queue entry", url="https://ex.com")
        assert isinstance(art, EvidenceArtifact)
        assert art in ev.artifacts

    def test_add_multiple_artifacts(self) -> None:
        ev = EvidencePacket(task_id="t-5", pipeline_stage=PipelineStage.EXECUTE)
        ev.add_artifact(ArtifactType.QUEUE_RECORD, "q", url="https://ex.com")
        ev.add_artifact(ArtifactType.EXECUTOR_LOG, "log", path="/tmp/log")
        assert len(ev.artifacts) == 2

    def test_add_artifact_rejects_missing_locator(self) -> None:
        ev = EvidencePacket(task_id="t-5b", pipeline_stage=PipelineStage.EXECUTE)
        with pytest.raises(ValueError, match="requires at least one"):
            ev.add_artifact(ArtifactType.HANDOFF, "missing both path and url")


# ---------------------------------------------------------------------------
# EvidencePacket.as_dict
# ---------------------------------------------------------------------------


class TestAsDictSerialization:
    def test_as_dict_required_keys(self) -> None:
        ev = EvidencePacket(task_id="t-6", pipeline_stage=PipelineStage.CI_REMEDIATION)
        ev.add_artifact(ArtifactType.QUEUE_RECORD, "q", url="https://ex.com")
        ev.add_artifact(ArtifactType.EXECUTOR_LOG, "log", path="/tmp/log")
        d = ev.as_dict()

        for key in (
            "task_id", "session_id", "pipeline_stage", "executor_run_ids",
            "timestamp_start_utc", "timestamp_end_utc", "manual_edits",
            "completeness", "artifacts",
        ):
            assert key in d, f"missing key: {key}"

    def test_as_dict_artifacts_serialized(self) -> None:
        ev = EvidencePacket(task_id="t-7", pipeline_stage=PipelineStage.PR_OPEN)
        ev.add_artifact(ArtifactType.DIFF, "pr diff", url="https://gh.com/diff")
        d = ev.as_dict()
        assert len(d["artifacts"]) == 1
        art = d["artifacts"][0]
        assert art["artifact_type"] == "diff"
        assert art["summary"] == "pr diff"
        assert art["url"] == "https://gh.com/diff"

    def test_as_dict_completeness_string(self) -> None:
        ev = EvidencePacket(task_id="t-8", pipeline_stage=PipelineStage.EXECUTE)
        d = ev.as_dict()
        assert d["completeness"] == "missing"

    def test_as_dict_manual_edits_default(self) -> None:
        ev = EvidencePacket(task_id="t-9", pipeline_stage=PipelineStage.INTAKE)
        assert ev.as_dict()["manual_edits"] == "none"


# ---------------------------------------------------------------------------
# Pipeline stage coverage
# ---------------------------------------------------------------------------


class TestPipelineStages:
    def test_all_stages_instantiable(self) -> None:
        for stage in PipelineStage:
            ev = EvidencePacket(task_id="t-s", pipeline_stage=stage)
            assert str(ev.pipeline_stage) == str(stage)


# ---------------------------------------------------------------------------
# GitHub webhook parsing (gh_integration helpers)
# ---------------------------------------------------------------------------


class TestParseGitHubWebhookPrNumber:
    def setup_method(self) -> None:
        from orchestration.gh_integration import (
            parse_github_webhook_pr_number,
            parse_github_webhook_repo,
            parse_github_webhook_actor,
            parse_github_webhook_author_association,
        )
        self.pr_number = parse_github_webhook_pr_number
        self.repo = parse_github_webhook_repo
        self.actor = parse_github_webhook_actor
        self.association = parse_github_webhook_author_association

    def test_pull_request_event(self) -> None:
        payload = {"pull_request": {"number": 42}}
        assert self.pr_number(payload) == 42

    def test_pull_request_review_event(self) -> None:
        payload = {"pull_request": {"number": 7}}
        assert self.pr_number(payload) == 7

    def test_issue_comment_pr(self) -> None:
        payload = {
            "issue": {
                "number": 15,
                "pull_request": {"url": "https://api.github.com/repos/o/r/pulls/15"},
            }
        }
        assert self.pr_number(payload) == 15

    def test_issue_comment_not_pr(self) -> None:
        payload = {"issue": {"number": 99}}
        assert self.pr_number(payload) is None

    def test_unrelated_event(self) -> None:
        payload = {"pusher": {"name": "user"}}
        assert self.pr_number(payload) is None

    def test_check_run_pull_requests(self) -> None:
        payload = {
            "check_run": {
                "pull_requests": [{"number": 27}],
            }
        }
        assert self.pr_number(payload) == 27

    def test_check_suite_pull_requests(self) -> None:
        payload = {
            "check_suite": {
                "pull_requests": [{"number": 31}],
            }
        }
        assert self.pr_number(payload) == 31

    def test_repo_full_name(self) -> None:
        payload = {"repository": {"full_name": "owner/myrepo"}}
        assert self.repo(payload) == "owner/myrepo"

    def test_repo_fallback_from_owner_name(self) -> None:
        payload = {
            "repository": {
                "owner": {"login": "owner"},
                "name": "myrepo",
            }
        }
        assert self.repo(payload) == "owner/myrepo"

    def test_repo_missing(self) -> None:
        assert self.repo({}) is None

    def test_repo_wrong_shape(self) -> None:
        assert self.repo({"repository": "not-a-dict"}) is None

    def test_repo_owner_wrong_shape(self) -> None:
        payload = {"repository": {"owner": "not-a-dict", "name": "myrepo"}}
        assert self.repo(payload) is None

    def test_actor_from_sender(self) -> None:
        payload = {"sender": {"login": "jleechan"}}
        assert self.actor(payload) == "jleechan"

    def test_actor_missing(self) -> None:
        assert self.actor({}) == ""

    def test_author_association(self) -> None:
        payload = {"comment": {"author_association": "OWNER"}}
        assert self.association(payload) == "OWNER"

    def test_author_association_missing(self) -> None:
        assert self.association({}) == ""


# ---------------------------------------------------------------------------
# webhook_bridge.receive_github_event
# ---------------------------------------------------------------------------


class TestReceiveGitHubEvent:
    def setup_method(self) -> None:
        from orchestration.webhook_bridge import receive_github_event, GitHubEventMode, current_github_event_mode
        self.receive = receive_github_event
        self.mode = current_github_event_mode
        self.GitHubEventMode = GitHubEventMode

    def _issue_comment_payload(
        self,
        pr_number: int = 1,
        association: str = "OWNER",
        body: str = "@smartclaw fix the tests",
        repo: str = "owner/repo",
    ) -> dict:
        return {
            "action": "created",
            "issue": {
                "number": pr_number,
                "pull_request": {"url": f"https://api.github.com/repos/{repo}/pulls/{pr_number}"},
            },
            "comment": {
                "body": body,
                "author_association": association,
            },
            "sender": {"login": "jleechan"},
            "repository": {"full_name": repo},
        }

    def test_trusted_issue_comment_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
        payload = self._issue_comment_payload(association="OWNER")
        result = self.receive(payload, "issue_comment")
        assert result is not None
        assert result["pr_number"] == 1
        assert result["repo"] == "owner/repo"
        assert result["event_type"] == "issue_comment"

    def test_untrusted_issue_comment_rejected(self) -> None:
        payload = self._issue_comment_payload(association="NONE")
        assert self.receive(payload, "issue_comment") is None

    def test_contributor_issue_comment_rejected(self) -> None:
        # CONTRIBUTOR is not in the default trusted set
        payload = self._issue_comment_payload(association="CONTRIBUTOR")
        assert self.receive(payload, "issue_comment") is None

    def test_member_association_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
        payload = self._issue_comment_payload(association="MEMBER")
        assert self.receive(payload, "issue_comment") is not None

    def test_collaborator_association_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
        payload = self._issue_comment_payload(association="COLLABORATOR")
        assert self.receive(payload, "issue_comment") is not None

    def test_non_pr_issue_comment_rejected(self) -> None:
        payload = {
            "action": "created",
            "issue": {"number": 5},  # no pull_request key
            "comment": {"body": "@smartclaw", "author_association": "OWNER"},
            "sender": {"login": "owner"},
            "repository": {"full_name": "owner/repo"},
        }
        assert self.receive(payload, "issue_comment") is None

    def test_pull_request_review_accepted_any_actor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
        payload = {
            "action": "submitted",
            "review": {"state": "approved"},
            "pull_request": {"number": 3, "head": {"sha": "review-sha"}},
            "sender": {"login": "reviewer"},
            "repository": {"full_name": "owner/repo"},
        }
        result = self.receive(payload, "pull_request_review")
        assert result is not None
        assert result["pr_number"] == 3
        assert result["workflow_lane"] == "fix-comment"
        assert result["run_outcome"] == "executed"
        assert result["idempotency_key"] == "3|review-sha|fix-comment"

    def test_review_comment_routes_to_fix_comment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
        payload = {
            "action": "created",
            "comment": {"author_association": "MEMBER", "body": "please fix this"},
            "pull_request": {"number": 8, "head": {"sha": "comment-sha"}},
            "sender": {"login": "reviewer"},
            "repository": {"full_name": "owner/repo"},
        }
        result = self.receive(payload, "pull_request_review_comment")
        assert result is not None
        assert result["workflow_lane"] == "fix-comment"
        assert result["run_outcome"] == "executed"
        assert result["idempotency_key"] == "8|comment-sha|fix-comment"

    def test_check_suite_failure_routes_to_fixpr(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
        payload = {
            "action": "completed",
            "check_suite": {
                "conclusion": "failure",
                "head_sha": "failed-sha",
                "pull_requests": [{"number": 9}],
            },
            "sender": {"login": "github-actions[bot]"},
            "repository": {"full_name": "owner/repo"},
        }
        result = self.receive(payload, "check_suite")
        assert result is not None
        assert result["workflow_lane"] == "fixpr"
        assert result["run_outcome"] == "executed"
        assert result["idempotency_key"] == "9|failed-sha|fixpr"

    def test_check_run_failure_routes_to_fixpr(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
        payload = {
            "action": "completed",
            "check_run": {
                "conclusion": "failure",
                "head_sha": "checkrun-failed-sha",
                "pull_requests": [{"number": 11}],
            },
            "sender": {"login": "github-actions[bot]"},
            "repository": {"full_name": "owner/repo"},
        }
        result = self.receive(payload, "check_run")
        assert result is not None
        assert result["workflow_lane"] == "fixpr"
        assert result["run_outcome"] == "executed"
        assert result["idempotency_key"] == "11|checkrun-failed-sha|fixpr"

    def test_duplicate_previous_run_is_suppressed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
        payload = {
            "action": "opened",
            "pull_request": {"number": 10, "head": {"sha": "dup-sha"}},
            "sender": {"login": "owner"},
            "repository": {"full_name": "owner/repo"},
        }
        result = self.receive(
            payload,
            "pull_request",
            previous_runs=[{
                "idempotency_key": "10|dup-sha|comment-validation",
                "run_outcome": "executed",
            }],
        )
        assert result is not None
        assert result["workflow_lane"] == "comment-validation"
        assert result["run_outcome"] == "duplicate_suppressed"
        assert result["skip_reason"] == "duplicate_event_same_head_sha"

    def test_unsupported_event_type_rejected(self) -> None:
        payload = {"pusher": {"name": "dev"}}
        assert self.receive(payload, "push") is None

    def test_mode_polling_when_no_secret(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
        assert self.mode() == self.GitHubEventMode.POLLING

    def test_mode_webhook_when_secret_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "s3cr3t")
        assert self.mode() == self.GitHubEventMode.WEBHOOK

    # Signature-validation tests (blocker #3)

    def test_secret_set_missing_header_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When secret is configured but no signature header provided, fail closed."""
        monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
        payload = self._issue_comment_payload(association="OWNER")
        raw_body = b"dummy"
        result = self.receive(
            payload,
            "issue_comment",
            webhook_secret="my-secret",
            raw_body=raw_body,
        )
        assert result is None

    def test_secret_set_missing_raw_body_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When secret is configured but raw_body absent, fail closed."""
        monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
        payload = self._issue_comment_payload(association="OWNER")
        result = self.receive(
            payload,
            "issue_comment",
            webhook_secret="my-secret",
            signature_header="sha256=abc123",
        )
        assert result is None

    def test_mismatched_signature_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When signature does not match raw body + secret, reject the event."""
        import hashlib
        import hmac as hmac_mod

        monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
        secret = b"correct-secret"
        raw_body = b"action-created"
        wrong_sig = "sha256=" + hmac_mod.new(secret, b"different-body", hashlib.sha256).hexdigest()
        payload = self._issue_comment_payload(association="OWNER")
        result = self.receive(
            payload,
            "issue_comment",
            webhook_secret="correct-secret",
            signature_header=wrong_sig,
            raw_body=raw_body,
        )
        assert result is None

    def test_valid_signature_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When secret, signature header, and raw body all match, event is accepted."""
        import hashlib
        import hmac as hmac_mod

        monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
        secret = "correct-secret"
        raw_body = b"action-created"
        sig = "sha256=" + hmac_mod.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
        payload = self._issue_comment_payload(association="OWNER")
        result = self.receive(
            payload,
            "issue_comment",
            webhook_secret=secret,
            signature_header=sig,
            raw_body=raw_body,
        )
        assert result is not None
        assert result["event_type"] == "issue_comment"

    def test_check_suite_without_pr_passes_through(self) -> None:
        """check_suite events without PR should pass through webhook_bridge."""
        payload = {
            "action": "completed",
            "check_suite": {
                "head_sha": "abc123def456",
                "conclusion": "success",
                "pull_requests": [],  # No PRs associated
            },
            "repository": {"full_name": "jleechanorg/smartclaw"},
            "sender": {"login": "github-actions[bot]"},
        }
        # Must provide valid webhook secret since GITHUB_WEBHOOK_SECRET is set
        import json
        raw_body = json.dumps(payload).encode()
        secret = "test-secret"
        import hmac
        import hashlib
        sig = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
        result = self.receive(payload, "check_suite", webhook_secret=secret, signature_header=sig, raw_body=raw_body)
        # Should NOT return None - check_suite without PR should pass through
        assert result is not None
        assert result["pr_number"] is None
        assert result["run_outcome"] == "skipped_ineligible"
        assert result["skip_reason"] == "no_pr_associated"

    def test_check_run_without_pr_passes_through(self) -> None:
        """check_run events without PR should pass through webhook_bridge."""
        payload = {
            "action": "completed",
            "check_run": {
                "head_sha": "deadbeef1234",
                "conclusion": "failure",
                "pull_requests": [],  # No PRs associated
            },
            "repository": {"full_name": "jleechanorg/smartclaw"},
            "sender": {"login": "github-actions[bot]"},
        }
        # Must provide valid webhook secret since GITHUB_WEBHOOK_SECRET is set
        import json
        raw_body = json.dumps(payload).encode()
        secret = "test-secret"
        import hmac
        import hashlib
        sig = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
        result = self.receive(payload, "check_run", webhook_secret=secret, signature_header=sig, raw_body=raw_body)
        # Should NOT return None - check_run without PR should pass through
        assert result is not None
        assert result["pr_number"] is None
        assert result["run_outcome"] == "skipped_ineligible"
        assert result["skip_reason"] == "no_pr_associated"
