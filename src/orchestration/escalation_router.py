"""Escalation router: deterministic-first judgment routing of AO events.

This module routes escalation events from AO to appropriate actions based on
deterministic rules. LLM judgment (NeedsJudgmentAction) is only returned when
no deterministic rule matches.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Union

from orchestration.ao_events import AOEvent
from orchestration.parallel_retry import check_ci_status, is_parseable_ci_failure
from orchestration.coderabbit_gate import check_coderabbit
# Lazy import: openclaw_self_review_gate module may not exist yet (orch-j9e0.4)
# Usage site (line ~600) is wrapped in try/except that fail-opens.
from orchestration.auto_resolve_threads import get_review_threads

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Policy and Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EscalationPolicy:
    """Policy configuration for escalation routing.

    Attributes:
        max_retries_per_session: Maximum retry attempts per session before escalating.
        session_timeout_minutes: Session timeout before considering it stuck.
        subtask_timeout_minutes: Subtask timeout before considering it stalled.
        max_strategy_changes: Maximum strategy changes per task before escalating.
        min_confidence: Minimum confidence threshold for LLM judgments.
        ci_grace_period_minutes: Extra time to wait when CI is running (pending/in_progress).
    """

    max_retries_per_session: int = 3
    session_timeout_minutes: int = 10
    subtask_timeout_minutes: int = 30
    max_strategy_changes: int = 2
    min_confidence: float = 0.6
    ci_grace_period_minutes: int = 5


# ---------------------------------------------------------------------------
# Failure Budget Tracking
# ---------------------------------------------------------------------------


@dataclass
class BudgetEntry:
    """Single budget entry for a subtask."""

    subtask_id: str
    task_id: str
    attempts: int = 0
    strategy_changes: int = 0
    first_escalation: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class FailureBudget:
    """Tracks failure attempts across sessions for the same task.

    This class tracks escalation attempts and strategy changes to determine
    when a task or subtask should be escalated to human review.
    """

    def __init__(self) -> None:
        self._subtasks: dict[str, BudgetEntry] = {}
        self._tasks: dict[str, int] = {}  # task_id -> strategy_changes

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

        # First, add all tasks with strategy changes
        for task_id, changes in self._tasks.items():
            result[task_id] = {
                "strategy_changes": changes,
                "subtasks": {},
            }

        # Then add subtask details
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


# ---------------------------------------------------------------------------
# Escalation Actions
# ---------------------------------------------------------------------------


@dataclass
class RetryAction:
    """Action to retry the session with an enriched prompt."""

    session_id: str
    project_id: str
    prompt: str
    reason: str
    pr_url: str | None = None  # Optional PR URL for auto-resolve after fix push


@dataclass
class KillAndRespawnAction:
    """Action to kill a stuck session and respawn a new one."""

    session_id: str  # Session to kill (alias for compatibility)
    session_to_kill: str
    project_id: str
    reason: str
    task: str  # Task description for the new session


@dataclass
class NotifyJeffreyAction:
    """Action to notify Jeffrey (human) about the event."""

    session_id: str
    message: str
    pr_url: str | None = None
    details: dict | None = None


@dataclass
class NeedsJudgmentAction:
    """Action that requires LLM judgment (non-deterministic)."""

    event: AOEvent
    context: dict
    options: list[str]


@dataclass
class ParallelRetryAction:
    """Action to attempt parallel fixes for CI failures.

    Used when budget >= 2 and CI error is parseable. Spawns multiple
    AO sessions with different fix strategies in parallel.
    """

    session_id: str
    project_id: str
    ci_failure: str
    diff: str
    max_strategies: int = 3
    reason: str = "Parallel retry for CI failure"


@dataclass
class MergeAction:
    """Action to merge a PR (auto-merge) or notify (notify-only).

    When auto-merge is enabled in user preferences, this action will
    execute the merge. Otherwise it falls back to notify-only behavior.
    """

    session_id: str
    pr_url: str | None
    merge_method: str = "squash"  # squash, merge, rebase


@dataclass
class WaitForCIAction:
    """Action to wait for CI to complete before declaring session stuck.

    Used when a session appears stuck but CI is still running (pending/in_progress).
    Extends the idle threshold instead of killing the session prematurely.
    """

    session_id: str
    project_id: str
    reason: str
    ci_status: str  # "pending" or "in_progress"
    extended_timeout_minutes: int  # session_timeout_minutes + ci_grace_period_minutes


# ---------------------------------------------------------------------------
# User Preferences
# ---------------------------------------------------------------------------


def _get_user_preferences_path() -> Path:
    """Get the path to user preferences file."""
    return Path("~/.openclaw/state/user_preferences.json").expanduser()


def is_auto_merge_enabled() -> bool:
    """Check if auto-merge is enabled in user preferences.

    Reads from ~/.openclaw/state/user_preferences.json.
    Defaults to False (notify-only) if file doesn't exist or is invalid.
    """
    prefs_path = _get_user_preferences_path()
    if not prefs_path.exists():
        logger.debug(f"User preferences not found at {prefs_path}, auto-merge disabled")
        return False

    try:
        with open(prefs_path, encoding="utf-8") as f:
            prefs = json.load(f)
        return bool(prefs.get("auto_merge", False))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Failed to read user preferences from {prefs_path}: {e}, auto-merge disabled")
        return False


# Type alias for all escalation actions
EscalationAction = Union[
    RetryAction,
    KillAndRespawnAction,
    NotifyJeffreyAction,
    NeedsJudgmentAction,
    ParallelRetryAction,
    MergeAction,
    WaitForCIAction,
]


# ---------------------------------------------------------------------------
# Judgment Result
# ---------------------------------------------------------------------------


@dataclass
class JudgmentResult:
    """Result of routing an escalation event to an action."""

    action: EscalationAction
    confidence: float
    reasoning: str


# ---------------------------------------------------------------------------
# Routing Logic
# ---------------------------------------------------------------------------


def _extract_subtask_id(event: AOEvent) -> str:
    """Extract subtask ID from event.

    First checks event.data for explicit subtask_id, then falls back to sessionId,
    and finally to session_id.
    """
    # Try explicit subtask_id first
    if "subtask_id" in event.data:
        return event.data["subtask_id"]
    # Try sessionId from AO
    if "sessionId" in event.data:
        return event.data["sessionId"]
    # Fall back to session_id
    return event.session_id


def _route_reaction_escalated(
    event: AOEvent,
    budget: FailureBudget,
    policy: EscalationPolicy,
) -> JudgmentResult:
    """Route reaction.escalated events based on reaction key."""
    ctx = event.escalation_context
    if ctx is None:
        # No escalation context, escalate to Jeffrey
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

    # ci-failed: retry within budget, escalate when exceeded
    if reaction_key == "ci-failed":
        if total_attempts <= policy.max_retries_per_session:
            # Check if we should use parallel retry:
            # - remaining attempts >= 2
            # - CI error is parseable
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

            # Retry with enriched prompt
            return JudgmentResult(
                action=RetryAction(
                    session_id=event.session_id,
                    project_id=event.project_id,
                    prompt=f"Previous attempt failed with CI. {event.message}. "
                    f"This is attempt {total_attempts} of {policy.max_retries_per_session}. "
                    "Please analyze the failure and try a different approach.",
                    reason=f"CI failed (attempt {total_attempts}/{policy.max_retries_per_session})",
                    pr_url=event.data.get("pr_url"),
                ),
                confidence=1.0,
                reasoning="Deterministic retry for ci-failed within budget",
            )
        else:
            # Budget exhausted, escalate to Jeffrey
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

    # changes-requested: always retry (deterministic)
    if reaction_key == "changes-requested":
        return JudgmentResult(
            action=RetryAction(
                session_id=event.session_id,
                project_id=event.project_id,
                prompt=f"Reviewer requested changes: {event.message}. "
                "Please address the feedback and resubmit.",
                reason="changes-requested (deterministic retry)",
                pr_url=event.data.get("pr_url"),
            ),
            confidence=1.0,
            reasoning="Deterministic retry for changes-requested",
        )

    # Unknown reaction key - escalate to Jeffrey
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
    """Route session.stuck events to kill and respawn.

    Before declaring stuck, checks if CI is pending/in_progress on the session's branch.
    If CI is running, returns WaitForCIAction instead of killing the session.
    """
    idle_minutes = event.data.get("idle_duration_minutes", 0)

    # Check CI status before declaring stuck
    # This prevents false positives when CI is just running slowly
    try:
        # Extract optional context from event.data
        bead_id = event.data.get("bead_id")
        branch = event.data.get("branch")
        
        # Skip CI check if branch is unknown to avoid checking wrong branch
        if not branch:
            logger.debug("Branch unknown for session %s, skipping CI check", event.session_id)
            ci_status = "pending"
        else:
            ci_result = check_ci_status(
                event.session_id,
                bead_id=bead_id,
                repo=event.project_id,
                branch=branch,
            )
            ci_status = ci_result.get("status", "pending")
    except Exception as e:
        # If CI check fails, log the error and default to pending to avoid false positives
        logger.warning(
            "check_ci_status failed for session %s (project=%s, bead_id=%s): %s — defaulting to pending",
            event.session_id,
            event.project_id,
            event.data.get("bead_id"),
            e,
        )
        ci_status = "pending"

    # If CI is in_progress or pending (actively running), extend the timeout instead of killing
    if ci_status in ("in_progress", "pending"):
        extended_timeout = policy.session_timeout_minutes + policy.ci_grace_period_minutes
        return JudgmentResult(
            action=WaitForCIAction(
                session_id=event.session_id,
                project_id=event.project_id,
                reason=f"Session idle for {idle_minutes} minutes, but CI is {ci_status}. "
                f"Extended timeout to {extended_timeout} minutes (session: {policy.session_timeout_minutes} + CI grace: {policy.ci_grace_period_minutes})",
                ci_status=ci_status,
                extended_timeout_minutes=extended_timeout,
            ),
            confidence=1.0,
            reasoning=f"CI is {ci_status}, waiting instead of killing",
        )

    # Extract task from event data or use a generic continuation message
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
    """Route merge.ready events to MergeAction or notify Jeffrey.

    If auto-merge is enabled in user preferences, returns MergeAction.
    Otherwise returns NotifyJeffreyAction (notify-only mode).
    """
    pr_url = event.data.get("pr_url")
    pr_number = event.data.get("pr_number")
    merge_method = event.data.get("merge_method", "squash")

    if not pr_url or not isinstance(pr_url, str):
        return JudgmentResult(
            action=NotifyJeffreyAction(
                session_id=event.session_id,
                message=f"Merge skipped: pr_url is missing or invalid (got {pr_url!r})",
                pr_url=None,
            ),
            confidence=1.0,
            reasoning="pr_url missing or invalid — cannot merge without a valid PR URL",
        )

    # Validate merge_method against allowed GitHub merge strategies
    allowed_methods = {"squash", "merge", "rebase"}
    if not isinstance(merge_method, str) or merge_method not in allowed_methods:
        return JudgmentResult(
            action=NotifyJeffreyAction(
                session_id=event.session_id,
                message=f"Merge skipped: invalid merge_method {merge_method!r}",
                pr_url=pr_url,
                details={"allowed_methods": sorted(allowed_methods)},
            ),
            confidence=1.0,
            reasoning="Invalid merge_method — refusing auto-merge",
        )

    # Check if auto-merge is enabled - only run CodeRabbit gate when we actually auto-merge
    auto_merge_enabled = is_auto_merge_enabled()

    # Check merge gates when PR URL is available (not just auto-merge)
    # Gate 7 (OpenClaw self-review) runs regardless of auto_merge_enabled
    if pr_url:
        # Validate pr_number explicitly before checking gates
        if pr_number is None:
            logger.warning("pr_number is None - cannot check merge gates, skipping auto-merge")
            return JudgmentResult(
                action=NotifyJeffreyAction(
                    session_id=event.session_id,
                    message=f"Merge skipped: pr_number is missing — {pr_url}",
                    pr_url=pr_url,
                    details={"error": "pr_number missing"},
                ),
                confidence=1.0,
                reasoning="pr_number required for merge gates",
            )

        # Extract owner/repo from pr_url (e.g., https://github.com/owner/repo/pull/123)
        try:
            parts = pr_url.rstrip("/").split("/")
            pull_idx = parts.index("pull")
            owner, repo = parts[pull_idx - 2], parts[pull_idx - 1]
        except (ValueError, IndexError) as e:
            logger.error(f"Failed to parse owner/repo from pr_url {pr_url}: {e}")
            return JudgmentResult(
                action=NotifyJeffreyAction(
                    session_id=event.session_id,
                    message=f"Merge blocked: Invalid pr_url format — {pr_url}",
                    pr_url=pr_url,
                    details={"parse_error": str(e)},
                ),
                confidence=1.0,
                reasoning=f"pr_url parse exception: {e}",
            )

        # Gate 1: OpenClaw LLM review check (PRIMARY gate - no rate limit)
        # Check if PR has been reviewed by OpenClaw LLM reviewer
        openclaw_review_path = Path(os.path.expanduser("~/.openclaw/state/openclaw_pr_reviews.jsonl"))
        openclaw_approved = False
        if openclaw_review_path.exists():
            try:
                with open(openclaw_review_path) as f:
                    for line in f:
                        review = json.loads(line)
                        if review.get("repo") == f"{owner}/{repo}" and review.get("pr_number") == pr_number:
                            if review.get("decision") == "approve":
                                openclaw_approved = True
                                break
            except Exception as e:
                logger.warning(f"Failed to read OpenClaw review state: {e}")

        # Gate 2: CodeRabbit check (INFORMATIONAL only - not blocking)
        # CR runs async and is rate-limited; we treat findings as post-merge follow-up
        cr_blocked = False
        cr_reason = ""
        try:
            cr_result = check_coderabbit(owner, repo, pr_number)
            cr_blocked = not cr_result.passed
            cr_reason = cr_result.reason if cr_blocked else ""
            if cr_blocked:
                logger.warning(f"CodeRabbit found issues (non-blocking): {cr_reason}")
        except Exception as e:
            logger.warning(f"CodeRabbit check failed (non-blocking): {e}")

        # Gate 3: OpenClaw self-review comment check
        # Check if agent has posted a self-review comment on the PR
        self_review_blocked = False
        self_review_reason = ""
        try:
            from orchestration.openclaw_self_review_gate import check_openclaw_self_review
            self_review_result = check_openclaw_self_review(owner, repo, pr_number)
            self_review_blocked = not self_review_result.passed
            self_review_reason = self_review_result.reason
            if self_review_blocked:
                logger.warning(f"OpenClaw self-review check failed: {self_review_reason}")
        except Exception as e:
            logger.warning(f"OpenClaw self-review check failed (fail-open): {e}")

        # PRIMARY GATE: OpenClaw LLM review must pass
        if not openclaw_approved:
            # Check if we need to trigger a review
            return JudgmentResult(
                action=NotifyJeffreyAction(
                    session_id=event.session_id,
                    message=f"Merge blocked: OpenClaw LLM review required — {pr_url}",
                    pr_url=pr_url,
                    details={"openclaw_review_pending": True, "cr_issues": cr_reason if cr_blocked else None},
                ),
                confidence=1.0,
                reasoning="OpenClaw LLM review not approved - trigger review before merge",
            )

        # Gate 4: OpenClaw self-review comment must be present
        if self_review_blocked:
            return JudgmentResult(
                action=NotifyJeffreyAction(
                    session_id=event.session_id,
                    message=f"Merge blocked: OpenClaw self-review comment required — {pr_url}",
                    pr_url=pr_url,
                    details={"self_review_missing": True, "reason": self_review_reason},
                ),
                confidence=1.0,
                reasoning="OpenClaw self-review comment not found - agent must comment on PR before merge",
            )

        # Only check CI and threads when auto-merge is enabled
        if auto_merge_enabled:
            # Check CI status is green before merge
            try:
                # Extract owner/repo from pr_url
                parts = pr_url.rstrip("/").split("/")
                pull_idx = parts.index("pull")
                owner, repo = parts[pull_idx - 2], parts[pull_idx - 1]
                branch = event.data.get("branch")
                
                # If branch is missing, fetch it from the PR to avoid checking wrong branch
                if not branch:
                    try:
                        result = subprocess.run(
                            ["gh", "pr", "view", str(pr_number), "--repo", f"{owner}/{repo}", "--json", "headRefName"],
                            capture_output=True,
                            text=True,
                            timeout=10,
                        )
                        if result.returncode == 0:
                            pr_data = json.loads(result.stdout)
                            branch = pr_data.get("headRefName")
                            logger.debug(f"Extracted branch '{branch}' from PR #{pr_number}")
                    except Exception as e:
                        logger.warning(f"Failed to extract branch from PR #{pr_number}: {e}")
                
                # Fail closed if branch is still unknown
                if not branch:
                    logger.warning(f"Cannot determine branch for PR #{pr_number}, blocking merge (fail-closed)")
                    return JudgmentResult(
                        action=NotifyJeffreyAction(
                            session_id=event.session_id,
                            message=f"Merge blocked: Cannot determine PR branch for CI check — {pr_url}",
                            pr_url=pr_url,
                            details={"error": "branch_unknown"},
                        ),
                        confidence=1.0,
                        reasoning="Cannot determine PR branch, fail-closed to prevent wrong-branch CI check",
                    )
                
                ci_result = check_ci_status(
                    event.session_id,
                    repo=f"{owner}/{repo}",
                    branch=branch,
                )
                ci_status = ci_result.get("status", "pending")
                if ci_status != "green":
                    logger.warning(f"CI status is {ci_status}, blocking merge for PR #{pr_number}")
                    return JudgmentResult(
                        action=NotifyJeffreyAction(
                            session_id=event.session_id,
                            message=f"Merge blocked: CI status is {ci_status} — {pr_url}",
                            pr_url=pr_url,
                            details={"ci_status": ci_status},
                        ),
                        confidence=1.0,
                        reasoning=f"CI status is {ci_status}, not green",
                    )
            except Exception as e:
                logger.error(f"Failed to check CI status (fail-closed): {e}")
                return JudgmentResult(
                    action=NotifyJeffreyAction(
                        session_id=event.session_id,
                        message=f"Merge blocked: CI check failed — {pr_url}",
                        pr_url=pr_url,
                        details={"ci_check_error": str(e)},
                    ),
                    confidence=1.0,
                    reasoning=f"CI check exception (fail-closed): {e}",
                )

            # Check unresolved thread count is 0 before merge
            try:
                parts = pr_url.rstrip("/").split("/")
                pull_idx = parts.index("pull")
                owner, repo = parts[pull_idx - 2], parts[pull_idx - 1]
                threads = get_review_threads(owner, repo, pr_number)
                unresolved_count = len(threads)
                if unresolved_count > 0:
                    logger.warning(f"PR #{pr_number} has {unresolved_count} unresolved threads, blocking merge")
                    return JudgmentResult(
                        action=NotifyJeffreyAction(
                            session_id=event.session_id,
                            message=f"Merge blocked: {unresolved_count} unresolved review threads — {pr_url}",
                            pr_url=pr_url,
                            details={"unresolved_threads": unresolved_count},
                        ),
                        confidence=1.0,
                        reasoning=f"{unresolved_count} unresolved threads found",
                    )
            except Exception as e:
                logger.error(f"Failed to check review threads (fail-closed): {e}")
                return JudgmentResult(
                    action=NotifyJeffreyAction(
                        session_id=event.session_id,
                        message=f"Merge blocked: Thread check failed — {pr_url}",
                        pr_url=pr_url,
                        details={"thread_check_error": str(e)},
                    ),
                    confidence=1.0,
                    reasoning=f"Thread check exception (fail-closed): {e}",
                )

            # Auto-merge enabled - execute merge
            return JudgmentResult(
                action=MergeAction(
                    session_id=event.session_id,
                    pr_url=pr_url,
                    merge_method=merge_method,
                ),
                confidence=1.0,
                reasoning="Auto-merge enabled, executing merge",
            )

    # Default: notify-only mode (auto-merge disabled or no pr_url)
    return JudgmentResult(
        action=NotifyJeffreyAction(
            session_id=event.session_id,
            message=f"PR #{pr_number} is ready for merge: {pr_url}",
            pr_url=pr_url,
        ),
        confidence=1.0,
        reasoning="Deterministic notify for merge.ready (auto-merge disabled)",
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

    This function implements deterministic-first routing. Only when no
    deterministic rule matches does it return NeedsJudgmentAction.

    Args:
        event: The AO event to route.
        budget: The failure budget tracking attempts.
        policy: The escalation policy configuration.

    Returns:
        JudgmentResult containing the action and confidence level.
    """
    # Extract subtask and task IDs
    subtask_id = _extract_subtask_id(event)
    task_id = event.data.get("task_id") or event.data.get("project_id") or subtask_id

    # Get current event's attempts if available
    ctx = event.escalation_context
    event_attempts = ctx.attempts if ctx else 0
    reaction_key = ctx.reaction_key if ctx else None

    # Check task exhaustion BEFORE routing (but not for changes-requested which is always deterministic)
    # ci-failed events don't add to strategy change count (they're retries), but they should still
    # respect existing strategy changes from other reaction types
    if reaction_key != "changes-requested":
        tracked_strategy_changes = budget.get_strategy_changes(task_id)
        # Only add potential change for non-ci-failed events (ci-failed are retries, not strategy changes)
        if reaction_key != "ci-failed":
            potential_changes = tracked_strategy_changes + (1 if event.event_type == "reaction.escalated" else 0)
        else:
            potential_changes = tracked_strategy_changes
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

        # Check subtask exhaustion (add event attempts to tracked budget)
        # But NOT for changes-requested which is always deterministic retry
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

    # Route based on event type
    if event.event_type == "reaction.escalated":
        return _route_reaction_escalated(event, budget, policy)

    if event.event_type == "session.stuck":
        return _route_session_stuck(event, budget, policy)

    if event.event_type == "merge.ready":
        return _route_merge_ready(event, budget, policy)

    if event.event_type == "merge.conflicts":
        return _route_merge_conflicts(event, budget, policy)

    # Unknown event type - fail-safe: escalate to Jeffrey
    return JudgmentResult(
        action=NotifyJeffreyAction(
            session_id=event.session_id,
            message=f"Unknown event type '{event.event_type}': {event.message}",
            details={"event_type": event.event_type, "data": event.data},
        ),
        confidence=1.0,
        reasoning="Unknown event type, fail-safe escalation",
    )
