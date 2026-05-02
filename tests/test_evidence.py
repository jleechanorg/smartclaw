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

