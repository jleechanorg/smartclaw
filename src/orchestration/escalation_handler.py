"""Escalation handler: orchestrates the full escalation flow from AO webhook to action execution.

This module provides:
- handle_escalation: Single entry point that parses, routes, and executes escalation actions
- load_escalation_policy: Load escalation policy from config file or use defaults

The handler maintains FailureBudget state across calls to properly track retry attempts.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Protocol

from orchestration.ao_events import AOEvent, parse_ao_webhook, AOWebhookError
from orchestration.escalation_router import (
    EscalationPolicy,
    FailureBudget,
    route_escalation,
    JudgmentResult,
)
from orchestration.action_executor import (
    execute_action,
    ActionResult,
    AOCli,
    SlackNotifier,
)


# Default state directory for policy and budget
DEFAULT_STATE_DIR = "~/.openclaw/state"

# Module-level budget state (persists across handle_escalation calls for same process)
_budget: FailureBudget | None = None
_budget_lock = threading.Lock()


class EscalationHandlerError(Exception):
    """Raised when escalation handling fails (parse error, execution error, etc.)."""

    pass


def load_escalation_policy(state_dir: str | None = None) -> EscalationPolicy:
    """Load escalation policy from config file or return defaults.

    Args:
        state_dir: Optional custom state directory. If not provided, uses
                   ~/.openclaw/state/escalation_policy.json

    Returns:
        EscalationPolicy with loaded or default values

    Default values:
        max_retries_per_session: 3
        session_timeout_minutes: 10
        subtask_timeout_minutes: 30
        max_strategy_changes: 2
        min_confidence: 0.6
    """
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

        # Validate and apply custom values
        return EscalationPolicy(
            max_retries_per_session=data.get("max_retries_per_session", default_policy.max_retries_per_session),
            session_timeout_minutes=data.get("session_timeout_minutes", default_policy.session_timeout_minutes),
            subtask_timeout_minutes=data.get("subtask_timeout_minutes", default_policy.subtask_timeout_minutes),
            max_strategy_changes=data.get("max_strategy_changes", default_policy.max_strategy_changes),
            min_confidence=data.get("min_confidence", default_policy.min_confidence),
        )
    except (json.JSONDecodeError, OSError):
        return default_policy



def _extract_subtask_id(event: AOEvent) -> str:
    """Extract subtask ID from event for budget tracking."""
    if "subtask_id" in event.data:
        return event.data["subtask_id"]
    if "sessionId" in event.data:
        return event.data["sessionId"]
    return event.session_id


def _extract_task_id(event: AOEvent) -> str:
    """Extract task ID from event for budget tracking."""
    if "task_id" in event.data:
        return event.data["task_id"]
    if "project_id" in event.data:
        return event.data["project_id"]
    return _extract_subtask_id(event)


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
    1. Parse webhook payload via parse_ao_webhook
    2. Create/update FailureBudget tracking
    3. Route via route_escalation
    4. Execute via execute_action

    Args:
        raw_payload: Raw dictionary from webhook POST body
        cli: AO CLI interface for send/kill/spawn operations
        notifier: Slack notifier for DM operations
        action_log_path: Path to action log JSONL file
        policy: Escalation policy configuration
        budget_path: Optional path to failure budget JSONL file; uses default if None

    Returns:
        ActionResult with success status and action details

    Raises:
        EscalationHandlerError: If payload is invalid or execution fails
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
        with _budget_lock:
            if _budget is None:
                _budget = FailureBudget()
            budget = _budget
    subtask_id = _extract_subtask_id(event)
    task_id = _extract_task_id(event)

    # Step 3: Route to action (router checks budget state for subtask exhaustion)
    judgment = route_escalation(event, budget, policy)
    action = judgment.action

    # Step 3b: Record escalation attempt in persistent budget only.
    # In-memory budgets rely on event.data.attempts for routing;
    # persistent budgets need their own tracking for cross-restart durability.
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
