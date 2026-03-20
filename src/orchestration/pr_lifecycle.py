"""PR lifecycle routing, duplicate suppression, and catch-up classification.

This module is intentionally Mission-Control-free. It provides deterministic
lane routing and idempotency semantics that upstream callers can use to decide
whether to dispatch work via mctrl.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


class SkipReason(StrEnum):
    """Skip reasons for events that are not routed to a workflow lane."""

    DUPLICATE_EVENT_SAME_HEAD_SHA = "duplicate_event_same_head_sha"
    UNMAPPED_TRIGGER = "unmapped_trigger"
    FRESH_EVENT_RUN_EXISTS = "fresh_event_run_exists"
    CATCH_UP_NOT_ACTIONABLE = "catch_up_not_actionable"
    NO_PR_ASSOCIATED = "no_pr_associated"


WORKFLOW_LANE_COMMENT_VALIDATION = "comment-validation"
WORKFLOW_LANE_FIX_COMMENT = "fix-comment"
WORKFLOW_LANE_FIXPR = "fixpr"

RUN_OUTCOME_EXECUTED = "executed"
RUN_OUTCOME_DUPLICATE_SUPPRESSED = "duplicate_suppressed"
RUN_OUTCOME_SKIPPED_INELIGIBLE = "skipped_ineligible"
RUN_OUTCOME_STALE_RECOVERED = "stale_recovered"


@dataclass(frozen=True)
class RouteDecision:
    """Canonical result for PR lifecycle routing."""

    workflow_lane: str | None
    trigger_source: str
    pr_number: int | None
    head_sha: str
    run_outcome: str
    idempotency_key: str | None
    skip_reason: SkipReason | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "workflow_lane": self.workflow_lane,
            "trigger_source": self.trigger_source,
            "pr_number": self.pr_number,
            "head_sha": self.head_sha,
            "run_outcome": self.run_outcome,
            "idempotency_key": self.idempotency_key,
        }
        if self.skip_reason is not None:
            payload["skip_reason"] = self.skip_reason.value
        return payload


def _build_idempotency_key(pr_number: int, head_sha: str, workflow_lane: str) -> str:
    return f"{pr_number}|{head_sha}|{workflow_lane}"


def _route_workflow_lane(trigger_type: str) -> str | None:
    mapping = {
        "pull_request.opened": WORKFLOW_LANE_COMMENT_VALIDATION,
        "pull_request.ready_for_review": WORKFLOW_LANE_COMMENT_VALIDATION,
        "pull_request.synchronize": WORKFLOW_LANE_COMMENT_VALIDATION,
        "pull_request_review.submitted": WORKFLOW_LANE_FIX_COMMENT,
        "pull_request_review_comment.created": WORKFLOW_LANE_FIX_COMMENT,
        "check_suite.completed.failure": WORKFLOW_LANE_FIXPR,
        "check_run.completed.failure": WORKFLOW_LANE_FIXPR,
    }
    return mapping.get(trigger_type)


def _successful_outcome(run_outcome: str | None) -> bool:
    return run_outcome in (RUN_OUTCOME_EXECUTED, RUN_OUTCOME_STALE_RECOVERED)


def _find_matching_run(
    previous_runs: list[dict[str, Any]],
    *,
    idempotency_key: str,
) -> dict[str, Any] | None:
    for previous_run in previous_runs:
        if previous_run.get("idempotency_key") == idempotency_key:
            return previous_run
    return None


def _parse_ts(timestamp: str | None) -> datetime | None:
    if not timestamp:
        return None
    normalized = timestamp.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def route_event(
    event: dict[str, Any],
    *,
    previous_runs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Route a PR lifecycle event and apply duplicate suppression."""
    trigger_source = str(event.get("trigger_source") or "event")
    pr_number_raw = event.get("pr_number")
    pr_number: int | None = int(pr_number_raw) if pr_number_raw is not None else None
    head_sha = str(event["head_sha"])
    trigger_type = str(event["trigger_type"])
    workflow_lane = _route_workflow_lane(trigger_type)

    # Skip events without an associated PR (e.g., check_suite on main branch)
    if pr_number is None:
        return RouteDecision(
            workflow_lane=None,
            trigger_source=trigger_source,
            pr_number=None,
            head_sha=head_sha,
            run_outcome=RUN_OUTCOME_SKIPPED_INELIGIBLE,
            idempotency_key=None,
            skip_reason=SkipReason.NO_PR_ASSOCIATED,
        ).to_dict()

    if workflow_lane is None:
        return RouteDecision(
            workflow_lane=None,
            trigger_source=trigger_source,
            pr_number=pr_number,
            head_sha=head_sha,
            run_outcome=RUN_OUTCOME_SKIPPED_INELIGIBLE,
            idempotency_key=None,
            skip_reason=SkipReason.UNMAPPED_TRIGGER,
        ).to_dict()

    idempotency_key = _build_idempotency_key(pr_number, head_sha, workflow_lane)
    previous_run = _find_matching_run(previous_runs or [], idempotency_key=idempotency_key)
    if previous_run is not None and _successful_outcome(previous_run.get("run_outcome")):
        return RouteDecision(
            workflow_lane=workflow_lane,
            trigger_source=trigger_source,
            pr_number=pr_number,
            head_sha=head_sha,
            run_outcome=RUN_OUTCOME_DUPLICATE_SUPPRESSED,
            idempotency_key=idempotency_key,
            skip_reason=SkipReason.DUPLICATE_EVENT_SAME_HEAD_SHA,
        ).to_dict()

    return RouteDecision(
        workflow_lane=workflow_lane,
        trigger_source=trigger_source,
        pr_number=pr_number,
        head_sha=head_sha,
        run_outcome=RUN_OUTCOME_EXECUTED,
        idempotency_key=idempotency_key,
    ).to_dict()


def route_catch_up(
    inspection: dict[str, Any],
    *,
    previous_runs: list[dict[str, Any]] | None = None,
    freshness_window_seconds: int = 3600,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Classify whether catch-up should execute for a PR/lane snapshot."""
    workflow_lane = str(inspection["workflow_lane"])
    pr_number = int(inspection["pr_number"])
    head_sha = str(inspection["head_sha"])
    trigger_source = str(inspection.get("trigger_source") or "catch_up")
    idempotency_key = _build_idempotency_key(pr_number, head_sha, workflow_lane)
    previous_run = _find_matching_run(previous_runs or [], idempotency_key=idempotency_key)

    if previous_run is None:
        return RouteDecision(
            workflow_lane=workflow_lane,
            trigger_source=trigger_source,
            pr_number=pr_number,
            head_sha=head_sha,
            run_outcome=RUN_OUTCOME_STALE_RECOVERED,
            idempotency_key=idempotency_key,
        ).to_dict()

    previous_outcome = previous_run.get("run_outcome")
    if previous_outcome == "failed":
        return RouteDecision(
            workflow_lane=workflow_lane,
            trigger_source=trigger_source,
            pr_number=pr_number,
            head_sha=head_sha,
            run_outcome=RUN_OUTCOME_STALE_RECOVERED,
            idempotency_key=idempotency_key,
        ).to_dict()

    effective_now = now or datetime.now(timezone.utc)
    executed_at = _parse_ts(previous_run.get("completed_at") or previous_run.get("executed_at"))
    if _successful_outcome(previous_outcome) and executed_at is not None:
        age_seconds = (effective_now - executed_at).total_seconds()
        if age_seconds <= freshness_window_seconds:
            return RouteDecision(
                workflow_lane=workflow_lane,
                trigger_source=trigger_source,
                pr_number=pr_number,
                head_sha=head_sha,
                run_outcome=RUN_OUTCOME_SKIPPED_INELIGIBLE,
                idempotency_key=idempotency_key,
                skip_reason=SkipReason.FRESH_EVENT_RUN_EXISTS,
            ).to_dict()

    if _successful_outcome(previous_outcome):
        return RouteDecision(
            workflow_lane=workflow_lane,
            trigger_source=trigger_source,
            pr_number=pr_number,
            head_sha=head_sha,
            run_outcome=RUN_OUTCOME_STALE_RECOVERED,
            idempotency_key=idempotency_key,
        ).to_dict()

    return RouteDecision(
        workflow_lane=workflow_lane,
        trigger_source=trigger_source,
        pr_number=pr_number,
        head_sha=head_sha,
        run_outcome=RUN_OUTCOME_SKIPPED_INELIGIBLE,
        idempotency_key=idempotency_key,
        skip_reason=SkipReason.CATCH_UP_NOT_ACTIONABLE,
    ).to_dict()


def summarize_status(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Summarize runs into operator-readable status rows by PR."""
    rows = []
    for run in runs:
        run_outcome = str(run.get("run_outcome"))
        if run_outcome == RUN_OUTCOME_STALE_RECOVERED:
            status = "recovered_by_catch_up"
        elif run_outcome == RUN_OUTCOME_DUPLICATE_SUPPRESSED:
            status = "duplicate_suppressed"
        elif run_outcome == RUN_OUTCOME_EXECUTED:
            status = "handled_in_real_time" if run.get("trigger_source") == "event" else "executed"
        else:
            status = run_outcome
        rows.append({
            "pr_number": run.get("pr_number"),
            "workflow_lane": run.get("workflow_lane"),
            "trigger_source": run.get("trigger_source"),
            "status": status,
            "skip_reason": run.get("skip_reason"),
        })
    return rows
