"""Generalized evidence packet schema for orchestration pipeline stages.

Evidence packets attach to pipeline stage transitions, providing
auditable proof of execution, artifacts, and handoffs.

Evidence completeness classifications:
  COMPLETE  — all required sections (queue, executor, artifact, handoff) present.
  PARTIAL   — queue + executor present; artifact or handoff partial/missing.
  MISSING   — one or more of queue or executor sections absent.

Sections mirror the canonical orchestration evidence contract:
  A) Queue evidence  (ArtifactType.QUEUE_RECORD)
  B) Executor evidence (ArtifactType.EXECUTOR_LOG)
  C) Artifact evidence (ArtifactType.DIFF | TEST_OUTPUT | CI_CHECK)
  D) Handoff evidence  (ArtifactType.HANDOFF)
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from enum import StrEnum


class EvidenceLevel(StrEnum):
    """Evidence completeness classification."""
    COMPLETE = "complete"    # All required sections present
    PARTIAL = "partial"      # A/B present; C/D partial or missing
    MISSING = "missing"      # One or more of A/B absent


class ArtifactType(StrEnum):
    """Types of evidence artifacts."""
    QUEUE_RECORD = "queue_record"       # Section A: task enqueue event
    EXECUTOR_LOG = "executor_log"       # Section B: agent/CLI execution log
    DIFF = "diff"                       # Section C: code diff or patch
    TEST_OUTPUT = "test_output"         # Section C: test run result
    CI_CHECK = "ci_check"               # Section C: CI status snapshot
    REVIEW_THREAD = "review_thread"     # Supplemental review evidence (non-required)
    HANDOFF = "handoff"                 # Section D: task→PR/branch mapping


class PipelineStage(StrEnum):
    """Pipeline stages where evidence packets are attached."""
    INTAKE = "intake"
    PLAN = "plan"
    EXECUTE = "execute"
    PR_OPEN = "pr_open"
    CI_REMEDIATION = "ci_remediation"
    REVIEW_REMEDIATION = "review_remediation"
    MERGE_JUDGMENT = "merge_judgment"
    COMPLETE = "pipeline_complete"  # distinct from EvidenceLevel.COMPLETE ("complete")


@dataclass
class EvidenceArtifact:
    """Single piece of evidence attached to a pipeline stage.

    At least one of `path` (local file) or `url` (remote link) must be set
    for the artifact to be considered valid.
    """
    artifact_type: ArtifactType
    summary: str
    path: str | None = None
    url: str | None = None
    timestamp_utc: str = field(default_factory=lambda: _utcnow())
    # Optional raw excerpt for quick inspection without fetching the artifact
    excerpt: str | None = None

    def is_valid(self) -> bool:
        """True when at least one locator (path or url) is set."""
        return bool(self.path or self.url)


@dataclass
class EvidencePacket:
    """Auditable evidence record for a pipeline stage transition.

    Required for any claim of "executed via orchestration pipeline."

    Attach an EvidencePacket at each stage transition. Call `close()` when
    the stage is complete. Derive `completeness` to determine if all required
    evidence sections are present before advancing.

    Usage::

        ev = EvidencePacket(task_id="t-123", pipeline_stage=PipelineStage.EXECUTE)
        ev.add_artifact(ArtifactType.QUEUE_RECORD, "Task enqueued", url="...")
        ev.add_artifact(ArtifactType.EXECUTOR_LOG, "claude run log", path="/tmp/...")
        ev.add_artifact(ArtifactType.DIFF, "PR patch", url="https://github.com/...")
        ev.add_artifact(ArtifactType.HANDOFF, "task→PR", url="https://github.com/...")
        ev.close()
        assert ev.completeness == EvidenceLevel.COMPLETE
    """

    task_id: str
    pipeline_stage: PipelineStage
    artifacts: list[EvidenceArtifact] = field(default_factory=list)
    executor_run_ids: list[str] = field(default_factory=list)
    timestamp_start_utc: str = field(default_factory=lambda: _utcnow())
    timestamp_end_utc: str = ""
    # "none" when no manual edits; otherwise a reason string
    manual_edits: str = "none"
    # Optional: executor session or worktree identity
    session_id: str = ""

    def close(self) -> None:
        """Stamp the end timestamp. Call when stage transition is complete."""
        self.timestamp_end_utc = _utcnow()

    @property
    def completeness(self) -> EvidenceLevel:
        """Derive evidence completeness from artifact types present.

        COMPLETE  = A + B + C + D all present
        PARTIAL   = A + B present (C or D missing)
        MISSING   = A or B absent
        """
        # Only valid, retrievable artifacts count toward completeness.
        types_present = {a.artifact_type for a in self.artifacts if a.is_valid()}
        has_queue = ArtifactType.QUEUE_RECORD in types_present
        has_executor = ArtifactType.EXECUTOR_LOG in types_present
        has_artifact = bool(
            types_present & {ArtifactType.DIFF, ArtifactType.TEST_OUTPUT, ArtifactType.CI_CHECK}
        )
        has_handoff = ArtifactType.HANDOFF in types_present

        if has_queue and has_executor and has_artifact and has_handoff:
            return EvidenceLevel.COMPLETE
        if has_queue and has_executor:
            return EvidenceLevel.PARTIAL
        return EvidenceLevel.MISSING

    def add_artifact(
        self,
        artifact_type: ArtifactType,
        summary: str,
        *,
        path: str | None = None,
        url: str | None = None,
        excerpt: str | None = None,
    ) -> EvidenceArtifact:
        """Append an evidence artifact and return it."""
        artifact = EvidenceArtifact(
            artifact_type=artifact_type,
            summary=summary,
            path=path,
            url=url,
            excerpt=excerpt,
        )
        if not artifact.is_valid():
            raise ValueError("EvidenceArtifact requires at least one of `path` or `url`.")
        self.artifacts.append(artifact)
        return artifact

    def as_dict(self) -> dict:
        """Serialize to a plain dict for storage or transmission."""
        return {
            "task_id": self.task_id,
            "session_id": self.session_id,
            "pipeline_stage": str(self.pipeline_stage),
            "executor_run_ids": self.executor_run_ids,
            "timestamp_start_utc": self.timestamp_start_utc,
            "timestamp_end_utc": self.timestamp_end_utc,
            "manual_edits": self.manual_edits,
            "completeness": str(self.completeness),
            "artifacts": [
                {
                    "artifact_type": str(a.artifact_type),
                    "summary": a.summary,
                    "path": a.path,
                    "url": a.url,
                    "timestamp_utc": a.timestamp_utc,
                    "excerpt": a.excerpt,
                }
                for a in self.artifacts
            ],
        }


def _utcnow() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()
