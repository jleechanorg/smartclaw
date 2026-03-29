"""Escalation module: combines handler and router for AO webhook processing.

This module combines:
- escalation_handler: orchestrates the full escalation flow from AO webhook to action execution
- escalation_router: deterministic-first judgment routing of AO events

Responsibilities:
- Parse AO webhook payloads
- Route escalation events to appropriate actions based on deterministic rules
- Execute actions (retry, kill/respawn, notify, parallel retry)
- Track failure budgets across sessions
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from orchestration.ao_events import AOEvent, parse_ao_webhook, AOWebhookError
from orchestration.action_executor import (
    execute_action,
    ActionResult,
    AOCli,
    SlackNotifier,
)
from orchestration.escalation_router import (
    RetryAction,
    KillAndRespawnAction,
    NotifyJeffreyAction,
    NeedsJudgmentAction,
    ParallelRetryAction,
    MergeAction,
    WaitForCIAction,
    EscalationAction,
    JudgmentResult as _RouterJudgmentResult,
)
from orchestration.parallel_retry import is_parseable_ci_failure


# =============================================================================
# PART 1: Policy and Configuration
# =============================================================================

DEFAULT_STATE_DIR = "~/.openclaw/state"

# Module-level budget state (persists across handle_escalation calls for same process)
_budget: "FailureBudget | None" = None


@dataclass(frozen=True)
class EscalationPolicy:
    """Policy configuration for escalation routing.

    Attributes:
        max_retries_per_session: Maximum retry attempts per session before escalating.
        session_timeout_minutes: Session timeout before considering it stuck.
        subtask_timeout_minutes: Subtask timeout before considering it stalled.
        max_strategy_changes: Maximum strategy changes per task before escalating.
        min_confidence: Minimum confidence threshold for LLM judgments.
    """
    max_retries_per_session: int = 3
    session_timeout_minutes: int = 10
    subtask_timeout_minutes: int = 30
    max_strategy_changes: int = 2
    min_confidence: float = 0.6


# =============================================================================
# PART 2: Failure Budget Tracking
# =============================================================================


@dataclass
class BudgetEntry:
    """Single budget entry for a subtask."""
    subtask_id: str
    task_id: str
    attempts: int = 0
    strategy_changes: int = 0
    first_escalation: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class FailureBudget:
    """Tracks failure attempts across sessions for the same task."""

    def __init__(self) -> None:
        self._subtasks: dict[str, BudgetEntry] = {}
        self._tasks: dict[str, int] = {}

    def record_escalation(
        self,
        subtask_id: str,
        task_id: str,
        reaction_key: str,
    ) -> None:
        """Record an escalation attempt for a subtask."""
        if subtask_id not in self._subtasks:
            self._subtasks[subtask_id] = BudgetEntry(
                subtask_id=subtask_id,
                task_id=task_id,
            )
        self._subtasks[subtask_id].attempts += 1

    def record_strategy_change(self, task_id: str) -> None:
        """Record a strategy change for a task."""
        if task_id not in self._tasks:
            self._tasks[task_id] = 0
        self._tasks[task_id] += 1

    def get_attempts(self, subtask_id: str) -> int:
        """Get the number of attempts for a subtask."""
        entry = self._subtasks.get(subtask_id)
        return entry.attempts if entry else 0

    def get_strategy_changes(self, task_id: str) -> int:
        """Get the number of strategy changes for a task."""
        return self._tasks.get(task_id, 0)

    def is_exhausted(self, subtask_id: str, policy: EscalationPolicy) -> bool:
        """Check if the subtask budget is exhausted."""
        attempts = self.get_attempts(subtask_id)
        return attempts >= policy.max_retries_per_session

    def is_task_exhausted(self, task_id: str, policy: EscalationPolicy) -> bool:
        """Check if the task is exhausted (too many strategy changes)."""
        changes = self.get_strategy_changes(task_id)
        return changes >= policy.max_strategy_changes

    def summary(self) -> dict:
        """Get a summary of all tracked failures."""
        result = {}
        for task_id, changes in self._tasks.items():
            result[task_id] = {
                "strategy_changes": changes,
                "subtasks": {},
            }
        for entry in self._subtasks.values():
            task_id = entry.task_id
            if task_id not in result:
                result[task_id] = {
                    "strategy_changes": self.get_strategy_changes(task_id),
                    "subtasks": {},
                }
            result[task_id]["subtasks"][entry.subtask_id] = {
                "attempts": entry.attempts,
                "first_escalation": entry.first_escalation,
            }
        return result


# =============================================================================
# PART 3: Escalation Actions
# =============================================================================
# Action classes and EscalationAction / JudgmentResult are imported directly
# from escalation_router so that action_executor.execute_action() dispatches
# correctly via isinstance() checks against the single canonical type hierarchy.
# Do NOT redefine them here — duplicate definitions produce incompatible types.

# Re-export aliases for backwards compatibility with callers that import from
# this module.
JudgmentResult = _RouterJudgmentResult


# =============================================================================
# PART 4: Router Logic
# =============================================================================


def _extract_subtask_id(event: AOEvent) -> str:
    """Extract subtask ID from event."""
    if "subtask_id" in event.data:
        return event.data["subtask_id"]
    if "sessionId" in event.data:
        return event.data["sessionId"]
    return event.session_id


def _route_reaction_escalated(
    event: AOEvent,
    budget: FailureBudget,
    policy: EscalationPolicy,
) -> JudgmentResult:
    """Route reaction.escalated events based on reaction key."""
    ctx = event.escalation_context
    if ctx is None:
        return JudgmentResult(
            action=NotifyJeffreyAction(
                session_id=event.session_id,
                message=f"Unable to determine reaction key for escalation: {event.message}",
            ),
            confidence=1.0,
            reasoning="No escalation context available",
        )

    subtask_id = _extract_subtask_id(event)
    total_attempts = budget.get_attempts(subtask_id) + ctx.attempts

    reaction_key = ctx.reaction_key

    if reaction_key == "ci-failed":
        if total_attempts <= policy.max_retries_per_session:
            remaining_attempts = policy.max_retries_per_session - total_attempts + 1
            ci_failure = event.message or ""
            diff = event.data.get("diff", "")

            if remaining_attempts >= 2 and is_parseable_ci_failure(ci_failure):
                return JudgmentResult(
                    action=ParallelRetryAction(
                        session_id=event.session_id,
                        project_id=event.project_id,
                        ci_failure=ci_failure,
                        diff=diff,
                        max_strategies=min(remaining_attempts, 3),
                        reason=f"CI failed (parallel retry, attempt {total_attempts}/{policy.max_retries_per_session})",
                    ),
                    confidence=1.0,
                    reasoning="Parallel retry for ci-failed with sufficient budget and parseable error",
                )

            return JudgmentResult(
                action=RetryAction(
                    session_id=event.session_id,
                    project_id=event.project_id,
                    prompt=f"Previous attempt failed with CI. {event.message}. "
                    f"This is attempt {total_attempts} of {policy.max_retries_per_session}. "
                    "Please analyze the failure and try a different approach.",
                    reason=f"CI failed (attempt {total_attempts}/{policy.max_retries_per_session})",
                ),
                confidence=1.0,
                reasoning="Deterministic retry for ci-failed within budget",
            )
        else:
            return JudgmentResult(
                action=NotifyJeffreyAction(
                    session_id=event.session_id,
                    message=f"CI failed after {total_attempts} attempts (budget: {policy.max_retries_per_session}). "
                    "Human intervention required.",
                    details={"attempts": total_attempts, "reaction_key": reaction_key},
                ),
                confidence=1.0,
                reasoning="CI failure budget exceeded",
            )

    if reaction_key == "changes-requested":
        return JudgmentResult(
            action=RetryAction(
                session_id=event.session_id,
                project_id=event.project_id,
                prompt=f"Reviewer requested changes: {event.message}. "
                "Please address the feedback and resubmit.",
                reason="changes-requested (deterministic retry)",
            ),
            confidence=1.0,
            reasoning="Deterministic retry for changes-requested",
        )

    return JudgmentResult(
        action=NotifyJeffreyAction(
            session_id=event.session_id,
            message=f"Unknown reaction key '{reaction_key}': {event.message}",
            details={"reaction_key": reaction_key},
        ),
        confidence=1.0,
        reasoning="Unknown reaction key, fail-safe escalation",
    )


def _route_session_stuck(
    event: AOEvent,
    budget: FailureBudget,
    policy: EscalationPolicy,
) -> JudgmentResult:
    """Route session.stuck events to kill and respawn."""
    idle_minutes = event.data.get("idle_duration_minutes", 0)
    task = event.data.get("task") or event.data.get("issue") or f"Continue work on {event.project_id}"

    return JudgmentResult(
        action=KillAndRespawnAction(
            session_id=event.session_id,
            session_to_kill=event.session_id,
            project_id=event.project_id,
            reason=f"Session idle for {idle_minutes} minutes (timeout: {policy.session_timeout_minutes})",
            task=task,
        ),
        confidence=1.0,
        reasoning="Deterministic kill+respawn for stuck session",
    )


def _route_merge_ready(
    event: AOEvent,
    budget: FailureBudget,
    policy: EscalationPolicy,
) -> JudgmentResult:
    """Route merge.ready events to notify Jeffrey."""
    pr_url = event.data.get("pr_url")
    pr_number = event.data.get("pr_number")

    return JudgmentResult(
        action=NotifyJeffreyAction(
            session_id=event.session_id,
            message=f"PR #{pr_number} is ready for merge: {pr_url}",
            pr_url=pr_url,
        ),
        confidence=1.0,
        reasoning="Deterministic notify for merge.ready",
    )


def _route_merge_conflicts(
    event: AOEvent,
    budget: FailureBudget,
    policy: EscalationPolicy,
) -> JudgmentResult:
    """Route merge.conflicts events to notify Jeffrey with details."""
    pr_url = event.data.get("pr_url")
    pr_number = event.data.get("pr_number")
    conflicting_files = event.data.get("conflicting_files", [])

    return JudgmentResult(
        action=NotifyJeffreyAction(
            session_id=event.session_id,
            message=f"Merge conflicts detected in PR #{pr_number}: {pr_url}",
            pr_url=pr_url,
            details={"conflicting_files": conflicting_files},
        ),
        confidence=1.0,
        reasoning="Deterministic notify for merge.conflicts",
    )


def route_escalation(
    event: AOEvent,
    budget: FailureBudget,
    policy: EscalationPolicy,
) -> JudgmentResult:
    """Route an AO event to an appropriate escalation action.

    This function implements deterministic-first routing.
    """
    subtask_id = _extract_subtask_id(event)
    task_id = event.data.get("task_id") or event.data.get("project_id") or subtask_id

    ctx = event.escalation_context
    event_attempts = ctx.attempts if ctx else 0
    reaction_key = ctx.reaction_key if ctx else None

    if reaction_key != "changes-requested":
        tracked_strategy_changes = budget.get_strategy_changes(task_id)
        is_new_strategy = reaction_key != "ci-failed" and event.event_type == "reaction.escalated"
        potential_changes = tracked_strategy_changes + (1 if is_new_strategy else 0)
        if potential_changes >= policy.max_strategy_changes:
            return JudgmentResult(
                action=NotifyJeffreyAction(
                    session_id=event.session_id,
                    message=f"Task exhausted: {tracked_strategy_changes} strategy changes "
                    f"(limit: {policy.max_strategy_changes}). Human intervention required.",
                    details=budget.summary(),
                ),
                confidence=1.0,
                reasoning="Task strategy changes exhausted",
            )
        # Budget not exhausted — record the strategy change if this is a new one
        if is_new_strategy:
            budget.record_strategy_change(task_id)

        tracked_attempts = budget.get_attempts(subtask_id)
        total_attempts = tracked_attempts + event_attempts
        if total_attempts > policy.max_retries_per_session:
            return JudgmentResult(
                action=NotifyJeffreyAction(
                    session_id=event.session_id,
                    message=f"Subtask budget exceeded: {tracked_attempts} tracked + {event_attempts} new "
                    f"(limit: {policy.max_retries_per_session}). Human intervention required.",
                    details={"tracked_attempts": tracked_attempts, "event_attempts": event_attempts},
                ),
                confidence=1.0,
                reasoning="Subtask budget exhausted",
            )

    if event.event_type == "reaction.escalated":
        return _route_reaction_escalated(event, budget, policy)

    if event.event_type == "session.stuck":
        return _route_session_stuck(event, budget, policy)

    if event.event_type == "merge.ready":
        return _route_merge_ready(event, budget, policy)

    if event.event_type == "merge.conflicts":
        return _route_merge_conflicts(event, budget, policy)

    return JudgmentResult(
        action=NotifyJeffreyAction(
            session_id=event.session_id,
            message=f"Unknown event type '{event.event_type}': {event.message}",
            details={"event_type": event.event_type, "data": event.data},
        ),
        confidence=1.0,
        reasoning="Unknown event type, fail-safe escalation",
    )


# =============================================================================
# PART 5: Escalation Handler
# =============================================================================


class EscalationHandlerError(Exception):
    """Raised when escalation handling fails."""
    pass


def load_escalation_policy(state_dir: str | None = None) -> EscalationPolicy:
    """Load escalation policy from config file or return defaults."""
    default_policy = EscalationPolicy(
        max_retries_per_session=3,
        session_timeout_minutes=10,
        subtask_timeout_minutes=30,
        max_strategy_changes=2,
        min_confidence=0.6,
    )

    if state_dir is None:
        state_dir = DEFAULT_STATE_DIR

    policy_path = Path(state_dir).expanduser() / "escalation_policy.json"

    if not policy_path.exists():
        return default_policy

    try:
        with open(policy_path, encoding="utf-8") as f:
            data = json.load(f)

        return EscalationPolicy(
            max_retries_per_session=data.get("max_retries_per_session", default_policy.max_retries_per_session),
            session_timeout_minutes=data.get("session_timeout_minutes", default_policy.session_timeout_minutes),
            subtask_timeout_minutes=data.get("subtask_timeout_minutes", default_policy.subtask_timeout_minutes),
            max_strategy_changes=data.get("max_strategy_changes", default_policy.max_strategy_changes),
            min_confidence=data.get("min_confidence", default_policy.min_confidence),
        )
    except (json.JSONDecodeError, OSError):
        return default_policy


def _extract_subtask_id_from_event(event: AOEvent) -> str:
    """Extract subtask ID from event for budget tracking."""
    if "subtask_id" in event.data:
        return event.data["subtask_id"]
    if "sessionId" in event.data:
        return event.data["sessionId"]
    return event.session_id


def _extract_task_id_from_event(event: AOEvent) -> str:
    """Extract task ID from event for budget tracking."""
    if "task_id" in event.data:
        return event.data["task_id"]
    if "project_id" in event.data:
        return event.data["project_id"]
    return _extract_subtask_id_from_event(event)


def handle_escalation(
    raw_payload: dict,
    cli: AOCli,
    notifier: SlackNotifier,
    action_log_path: str,
    policy: EscalationPolicy,
    budget_path: Path | None = None,
) -> ActionResult:
    """Handle an escalation event from AO webhook.

    This is the single entry point for the escalation flow:
    1. Parse webhook payload
    2. Create/update FailureBudget tracking
    3. Route via route_escalation
    4. Execute via execute_action
    """
    # Step 1: Parse webhook payload
    try:
        event = parse_ao_webhook(raw_payload)
    except AOWebhookError as e:
        raise EscalationHandlerError(f"Invalid payload: {e}") from e

    # Step 2: Get budget for routing
    global _budget
    if budget_path is not None:
        from orchestration.failure_budget import FailureBudget as PersistentFailureBudget
        budget: FailureBudget = PersistentFailureBudget(budget_path=budget_path)
    else:
        if _budget is None:
            _budget = FailureBudget()
        budget = _budget
    subtask_id = _extract_subtask_id_from_event(event)
    task_id = _extract_task_id_from_event(event)

    # Step 3: Route to action
    judgment = route_escalation(event, budget, policy)
    action = judgment.action

    # Step 3b: Record escalation attempt
    if budget_path is not None:
        reaction_key = (
            event.escalation_context.reaction_key
            if event.escalation_context is not None
            else "unknown"
        )
        budget.record_escalation(subtask_id, task_id, reaction_key)

    # Step 4: Execute action
    try:
        result = execute_action(
            action,
            cli=cli,
            notifier=notifier,
            action_log_path=action_log_path,
        )
    except Exception as e:
        raise EscalationHandlerError(f"Action execution failed: {e}") from e

    return result


# Re-export for backwards compatibility
__all__ = [
    "EscalationPolicy",
    "FailureBudget",
    "EscalationAction",
    "RetryAction",
    "KillAndRespawnAction",
    "NotifyJeffreyAction",
    "NeedsJudgmentAction",
    "ParallelRetryAction",
    "JudgmentResult",
    "route_escalation",
    "EscalationHandlerError",
    "load_escalation_policy",
    "handle_escalation",
]
