"""Action executor: translate router decisions to AO/Slack calls.

This module executes escalation actions returned by the escalation router:
- RetryAction: calls ao_send with enriched prompt
- KillAndRespawnAction: calls ao_kill then ao_spawn
- NotifyJeffreyAction: sends Slack DM to Jeffrey
- NeedsJudgmentAction: returns event for LLM (no side effects)
- All actions are logged to action_log.jsonl
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import smtplib
from dataclasses import dataclass
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path
from typing import Protocol
from urllib.request import Request, urlopen

from orchestration.ao_events import AOEvent
from orchestration.ao_cli import AOCommandError, ao_send, ao_kill, ao_spawn
from orchestration.gh_integration import PRInfo, merge_pr
from orchestration.openclaw_notifier import notify_slack_done
from orchestration.escalation_router import (
    RetryAction,
    KillAndRespawnAction,
    NotifyJeffreyAction,
    NeedsJudgmentAction,
    ParallelRetryAction,
    MergeAction,
    WaitForCIAction,
    EscalationAction,
)
from orchestration.parallel_retry import (
    generate_fix_strategies,
    execute_parallel_retry,
    ParallelRetryError,
)
from orchestration.guidance_tracker import (
    log_guidance_sent,
    check_ignored_guidance,
    auto_file_bead_for_ignored,
)
from orchestration.auto_resolve_threads import auto_resolve_threads_for_pr
from orchestration.mcp_mail import get_default_project_key

logger = logging.getLogger(__name__)

# Default action log path
DEFAULT_ACTION_LOG_PATH = "~/.openclaw/state/action_log.jsonl"

# Channel ID for owner DM — set via env var
JEFFREY_DM_CHANNEL = os.environ.get("SMARTCLAW_DM_CHANNEL", "")


def send_guidance_via_mcp_mail(
    session_id: str,
    guidance_type: str,
    action_type: str,
    strategy: str | None = None,
    error_class: str | None = None,
    known_winners: list[str] | None = None,
    confidence: float | None = None,
    reason: str | None = None,
) -> bool:
    """Send guidance to an agent via MCP mail.

    Args:
        session_id: The agent's session ID
        guidance_type: Type of guidance ("guidance", "abort", "strategy_override")
        action_type: The action type that was taken
        strategy: Optional fix strategy
        error_class: Optional error fingerprint
        known_winners: Optional list of successful strategies for this error class
        confidence: Optional confidence score
        reason: Optional human-readable explanation

    Returns:
        True if guidance was sent successfully
    """
    # Build the message payload
    payload = {
        "type": guidance_type,
        "action": action_type,
    }
    if strategy:
        payload["strategy"] = strategy
    if error_class:
        payload["error_class"] = error_class
    if known_winners:
        payload["known_winners"] = known_winners
    if confidence is not None:
        payload["confidence"] = confidence
    if reason:
        payload["reason"] = reason

    # Log guidance sent
    log_guidance_sent(session_id, guidance_type, payload)

    # Try to send via MCP mail
    try:
        import subprocess
        result = subprocess.run(
            [
                "openclaw", "mcp", "call", "mcp-agent-mail.send_message",
                "--project-key", get_default_project_key(),
                "--sender-name", "openclaw",
                "--to", session_id,
                "--subject", f"Guidance: {guidance_type}",
                "--body", json.dumps(payload),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            logger.info(f"Sent {guidance_type} guidance to {session_id}")
            return True
        else:
            logger.warning(f"Failed to send guidance to {session_id}: {result.stderr}")
            return False
    except Exception as e:
        logger.warning(f"Error sending guidance to {session_id}: {e}")
        return False


@dataclass
class ActionResult:
    """Result of executing an escalation action."""

    success: bool
    action_type: str
    details: dict


@dataclass
class ActionLogEntry:
    """Single entry in the action log."""

    timestamp: str
    action_type: str
    session_id: str
    success: bool
    details: dict
    reason: str | None = None


class SlackNotifier(Protocol):
    """Protocol for Slack notification - implemented by mocks and real notifier."""

    def send_dm(self, message: str, channel: str | None = None) -> bool:
        """Send a direct message to a channel or user."""
        ...


class AOCli(Protocol):
    """Protocol for AO CLI operations - implemented by mocks and real CLI."""

    def send(self, session_id: str, message: str) -> None:
        """Send a message to an AO session."""
        ...

    def kill(self, session_id: str) -> None:
        """Kill an AO session."""
        ...

    def spawn(self, project: str, issue: str, *, branch: str | None = None) -> str:
        """Spawn a new AO session."""
        ...


def _log_action(
    action_type: str,
    session_id: str,
    success: bool,
    details: dict,
    action_log_path: str,
    reason: str | None = None,
) -> None:
    """Append an action log entry to the JSONL file."""
    entry = ActionLogEntry(
        timestamp=datetime.now(timezone.utc).isoformat(),
        action_type=action_type,
        session_id=session_id,
        success=success,
        details=details,
        reason=reason,
    )
    path = Path(action_log_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "timestamp": entry.timestamp,
            "action_type": entry.action_type,
            "session_id": entry.session_id,
            "success": entry.success,
            "details": entry.details,
            "reason": entry.reason,
        }) + "\n")


def _parse_pr_url(pr_url: str | None) -> tuple[str, str, int] | None:
    """Parse PR URL to extract owner, repo, and PR number.

    Args:
        pr_url: PR URL (e.g., https://github.com/owner/repo/pull/123)

    Returns:
        Tuple of (owner, repo, pr_number) or None if parsing fails
    """
    if not pr_url:
        return None
    try:
        parts = pr_url.rstrip("/").split("/")
        pull_idx = parts.index("pull")
        owner_repo = "/".join(parts[pull_idx - 2 : pull_idx])
        owner, repo = owner_repo.split("/")
        pr_number = int(parts[pull_idx + 1])
        return (owner, repo, pr_number)
    except (ValueError, IndexError):
        return None


def _auto_resolve_if_pr(
    pr_url: str | None,
    session_id: str,
) -> dict | None:
    """Call auto_resolve_threads_for_pr if pr_url is available.

    Args:
        pr_url: PR URL to resolve threads for
        session_id: Session ID for logging context

    Returns:
        Result dict from auto_resolve_threads_for_pr or None if skipped
    """
    parsed = _parse_pr_url(pr_url)
    if not parsed:
        return None
    owner, repo, pr_number = parsed
    try:
        result = auto_resolve_threads_for_pr(owner, repo, pr_number)
        return result
    except Exception as e:
        # Log but don't fail the action
        logging.warning(
            f"auto_resolve_threads_for_pr failed for {owner}/{repo}#{pr_number}: {e}"
        )
        return None


def _execute_retry_action(
    action: RetryAction,
    cli: AOCli,
    notifier: SlackNotifier,
    action_log_path: str,
) -> ActionResult:
    """Execute a RetryAction: call ao_send, fall back to notify on failure."""
    # Build enriched message with reason
    enriched_message = f"{action.prompt}\n\nReason: {action.reason}"

    try:
        cli.send(action.session_id, enriched_message)
        details = {
            "session_id": action.session_id,
            "project_id": action.project_id,
            "reason": action.reason,
        }
        _log_action(
            action_type="RetryAction",
            session_id=action.session_id,
            success=True,
            details=details,
            action_log_path=action_log_path,
            reason=action.reason,
        )
        return ActionResult(
            success=True,
            action_type="RetryAction",
            details=details,
        )
    except AOCommandError as e:
        # Fall back to NotifyJeffreyAction on failure (but log as RetryAction failure)
        fallback_message = (
            f"RetryAction failed for session {action.session_id}.\n"
            f"Original reason: {action.reason}\n"
            f"Error: {e.stderr}"
        )

        # Send notification but don't log as separate action (we log as RetryAction failure)
        fallback_details = {
            "session_id": action.session_id,
            "message": fallback_message,
            "pr_url": None,
            "details": {"original_action": "RetryAction", "error": str(e)},
        }
        notifier.send_dm(
            f"Session: {action.session_id}\n{fallback_message}",
            channel=JEFFREY_DM_CHANNEL,
        )

        details = {
            "session_id": action.session_id,
            "reason": action.reason,
            "fallback_triggered": "fallback to NotifyJeffreyAction (ao_send_failed)",
        }
        _log_action(
            action_type="RetryAction",
            session_id=action.session_id,
            success=False,
            details=details,
            action_log_path=action_log_path,
            reason=action.reason,
        )
        return ActionResult(
            success=False,
            action_type="RetryAction",
            details=details,
        )


def _execute_kill_and_respawn_action(
    action: KillAndRespawnAction,
    cli: AOCli,
    notifier: SlackNotifier,
    action_log_path: str,
) -> ActionResult:
    """Execute a KillAndRespawnAction: kill session, spawn new one."""
    warning = None

    # Try to kill the session (don't fail if kill fails)
    try:
        cli.kill(action.session_to_kill)
    except AOCommandError:
        warning = "kill_failed"

    # Always attempt spawn
    try:
        new_session_id = cli.spawn(action.project_id, action.task)
    except AOCommandError as e:
        # If spawn fails, notify Jeffrey
        failure_message = (
            f"KillAndRespawnAction failed: spawn failed for project {action.project_id}.\n"
            f"Session killed: {action.session_to_kill}\n"
            f"Error: {e.stderr}"
        )
        failure_action = NotifyJeffreyAction(
            session_id=action.session_id,
            message=failure_message,
            details={
                "action": "KillAndRespawnAction",
                "session_killed": action.session_to_kill,
                "spawn_error": str(e),
            },
        )
        _execute_notify_jeffrey_action(
            failure_action, cli, notifier, action_log_path
        )
        details = {
            "session_killed": action.session_to_kill,
            "project_id": action.project_id,
            "reason": action.reason,
            "new_session_id": None,
            "warning": warning,
        }
        _log_action(
            action_type="KillAndRespawnAction",
            session_id=action.session_id,
            success=False,
            details=details,
            action_log_path=action_log_path,
            reason=action.reason,
        )
        return ActionResult(
            success=False,
            action_type="KillAndRespawnAction",
            details=details,
        )

    # Success
    details = {
        "session_killed": action.session_to_kill,
        "new_session_id": new_session_id,
        "project_id": action.project_id,
        "reason": action.reason,
        "warning": warning,
    }
    _log_action(
        action_type="KillAndRespawnAction",
        session_id=action.session_id,
        success=True,
        details=details,
        action_log_path=action_log_path,
        reason=action.reason,
    )
    return ActionResult(
        success=True,
        action_type="KillAndRespawnAction",
        details=details,
    )


def _execute_notify_jeffrey_action(
    action: NotifyJeffreyAction,
    cli: AOCli,
    notifier: SlackNotifier,
    action_log_path: str,
) -> ActionResult:
    """Execute a NotifyJeffreyAction: send Slack DM to Jeffrey."""
    # Build message with session ID, optional PR URL and details
    message_parts = [f"Session: {action.session_id}", action.message]

    if action.pr_url:
        pr_number = action.pr_url.split("/pull/")[-1] if "/pull/" in action.pr_url else "?"
        message_parts.append(f"PR #{pr_number}: {action.pr_url}")

    if action.details:
        details_str = ", ".join(f"{k}: {v}" for k, v in action.details.items())
        message_parts.append(f"Details: {details_str}")

    full_message = "\n".join(message_parts)

    # Send DM
    success = notifier.send_dm(full_message, channel=JEFFREY_DM_CHANNEL)

    details = {
        "session_id": action.session_id,
        "message": action.message,
        "pr_url": action.pr_url,
        "details": action.details or {},
    }
    _log_action(
        action_type="NotifyJeffreyAction",
        session_id=action.session_id,
        success=success,
        details=details,
        action_log_path=action_log_path,
    )
    return ActionResult(
        success=success,
        action_type="NotifyJeffreyAction",
        details=details,
    )


def _execute_needs_judgment_action(
    action: NeedsJudgmentAction,
    cli: AOCli,
    notifier: SlackNotifier,
    action_log_path: str,
) -> ActionResult:
    """Execute a NeedsJudgmentAction: return event for LLM (no side effects)."""
    details = {
        "event": action.event,
        "llm_context": action.context,
        "options": action.options,
    }
    _log_action(
        action_type="NeedsJudgmentAction",
        session_id=action.event.session_id,
        success=True,
        details={"event_type": action.event.event_type},
        action_log_path=action_log_path,
    )
    return ActionResult(
        success=True,
        action_type="NeedsJudgmentAction",
        details=details,
    )


def _execute_merge_action(
    action: MergeAction,
    cli: AOCli,
    notifier: SlackNotifier,
    action_log_path: str,
) -> ActionResult:
    """Execute a MergeAction: merge the PR and send confirmation."""
    # Parse PR URL to get owner/repo and PR number
    pr_url = action.pr_url
    if not pr_url:
        error_msg = "MergeAction.pr_url is None — cannot merge without a PR URL"
        _log_action(
            action_type="MergeAction",
            session_id=action.session_id,
            success=False,
            details={"pr_url": None, "error": error_msg},
            action_log_path=action_log_path,
        )
        notifier.send_dm(
            f"Merge failed for session {action.session_id}: {error_msg}",
            channel=JEFFREY_DM_CHANNEL,
        )
        return ActionResult(
            success=False,
            action_type="MergeAction",
            details={"pr_url": None, "error": error_msg},
        )
    try:
        # URL format: https://github.com/owner/repo/pull/123
        parts = pr_url.rstrip("/").split("/")
        # Find "pull" in the URL to get the index
        pull_idx = parts.index("pull")
        owner_repo = "/".join(parts[pull_idx - 2 : pull_idx])  # owner/repo
        pr_number = int(parts[pull_idx + 1])
        owner, repo = owner_repo.split("/")
    except (ValueError, IndexError) as e:
        # Failed to parse PR URL, log and notify
        error_msg = f"Failed to parse PR URL: {pr_url} ({e})"
        _log_action(
            action_type="MergeAction",
            session_id=action.session_id,
            success=False,
            details={"pr_url": pr_url, "error": str(e)},
            action_log_path=action_log_path,
        )
        notifier.send_dm(
            f"Merge failed for session {action.session_id}: {error_msg}",
            channel=JEFFREY_DM_CHANNEL,
        )
        return ActionResult(
            success=False,
            action_type="MergeAction",
            details={"pr_url": pr_url, "error": str(e)},
        )

    # Hard gate: refuse merge if MERGE_FREEZE sentinel is present
    import os as _os
    freeze_path = _os.path.expanduser("~/.openclaw/MERGE_FREEZE")
    if _os.path.exists(freeze_path):
        freeze_msg = open(freeze_path).read().strip()
        reason = f"MERGE_FREEZE active — {freeze_msg}"
        _log_action(
            action_type="MergeAction",
            session_id=action.session_id,
            success=False,
            details={"pr_url": pr_url, "error": reason},
            action_log_path=action_log_path,
        )
        notifier.send_dm(
            f"Merge BLOCKED for {pr_url}: {reason}",
            channel=JEFFREY_DM_CHANNEL,
        )
        return ActionResult(
            success=False,
            action_type="MergeAction",
            details={"pr_url": pr_url, "error": reason},
        )

    # Unified merge gate check - all 6 conditions
    from orchestration.merge_gate import check_merge_ready
    verdict = check_merge_ready(owner, repo, pr_number)
    if not verdict.can_merge:
        blocked_msg = "; ".join(verdict.blocked_reasons)
        _log_action(
            action_type="MergeAction",
            session_id=action.session_id,
            success=False,
            details={"pr_url": pr_url, "error": blocked_msg, "conditions": [c.details for c in verdict.conditions]},
            action_log_path=action_log_path,
        )
        notifier.send_dm(
            f"Merge BLOCKED for {pr_url}: {blocked_msg}",
            channel=JEFFREY_DM_CHANNEL,
        )
        return ActionResult(
            success=False,
            action_type="MergeAction",
            details={"pr_url": pr_url, "error": blocked_msg},
        )

    # Create PRInfo and execute merge
    pr_info = PRInfo(
        number=pr_number,
        url=pr_url,
        title="",  # Not needed for merge
        owner=owner,
        repo=repo,
        branch="",  # Not needed for merge
        base_branch="",  # Not needed for merge
        is_draft=False,  # Not needed for merge
    )

    try:
        merge_pr(pr_info, method=action.merge_method)
        merge_success = True
        merge_error = None
    except Exception as e:
        err_str = str(e)
        # Treat "already merged" as success — MergeAction is idempotent.
        # "pull request is not mergeable" is a real failure (e.g. merge conflicts)
        # and must NOT be mapped to success.
        if "already merged" in err_str.lower():
            merge_success = True
            merge_error = f"already merged (idempotent): {err_str}"
        else:
            merge_success = False
            merge_error = err_str

    # Log the action
    details = {
        "session_id": action.session_id,
        "pr_url": pr_url,
        "pr_number": pr_number,
        "owner": owner,
        "repo": repo,
        "merge_method": action.merge_method,
        "merge_error": merge_error,
    }
    _log_action(
        action_type="MergeAction",
        session_id=action.session_id,
        success=merge_success,
        details=details,
        action_log_path=action_log_path,
    )

    # Send Slack DM confirming the merge
    if merge_success:
        notifier.send_dm(
            f"PR merged: #{pr_number} {pr_url}\n"
            f"Method: {action.merge_method}\n"
            f"Session: {action.session_id}",
            channel=JEFFREY_DM_CHANNEL,
        )
    else:
        notifier.send_dm(
            f"Merge FAILED for PR #{pr_number}: {pr_url}\n"
            f"Error: {merge_error}\n"
            f"Session: {action.session_id}",
            channel=JEFFREY_DM_CHANNEL,
        )

    return ActionResult(
        success=merge_success,
        action_type="MergeAction",
        details=details,
    )


def _execute_parallel_retry_action(
    action: ParallelRetryAction,
    cli: AOCli,
    notifier: SlackNotifier,
    action_log_path: str,
) -> ActionResult:
    """Execute a ParallelRetryAction: spawn parallel sessions with different strategies."""
    # Generate fix strategies from CI failure and diff
    strategies = generate_fix_strategies(
        ci_failure=action.ci_failure,
        diff=action.diff,
        max_strategies=action.max_strategies,
    )

    # Derive error class for outcome recording
    from orchestration.parallel_retry import derive_error_class
    error_class = derive_error_class(action.ci_failure)

    # Execute parallel retry
    try:
        result = execute_parallel_retry(
            strategies=strategies,
            project=action.project_id,
            issue=action.reason,
            cli=cli,
            error_class=error_class,
            session_id=action.session_id,
        )

        details = {
            "session_id": action.session_id,
            "project_id": action.project_id,
            "strategies_tried": len(strategies),
            "sessions_spawned": result.sessions_spawned,
            "sessions_killed": result.sessions_killed,
            "winner": result.winner.approach_id if result.winner else None,
            "reason": action.reason,
        }
        _log_action(
            action_type="ParallelRetryAction",
            session_id=action.session_id,
            success=result.winner is not None,
            details=details,
            action_log_path=action_log_path,
            reason=action.reason,
        )
        return ActionResult(
            success=result.winner is not None,
            action_type="ParallelRetryAction",
            details=details,
        )
    except ParallelRetryError as e:
        details = {
            "session_id": action.session_id,
            "project_id": action.project_id,
            "reason": action.reason,
            "error": str(e),
        }
        _log_action(
            action_type="ParallelRetryAction",
            session_id=action.session_id,
            success=False,
            details=details,
            action_log_path=action_log_path,
            reason=action.reason,
        )
        # Fall back to notifying Jeffrey on error
        fallback_message = f"ParallelRetryAction failed for session {action.session_id}: {e}"
        notifier.send_dm(fallback_message, channel=JEFFREY_DM_CHANNEL)
        return ActionResult(
            success=False,
            action_type="ParallelRetryAction",
            details=details,
        )


def _execute_wait_for_ci_action(
    action: WaitForCIAction,
    cli: AOCli,
    notifier: SlackNotifier,
    action_log_path: str,
) -> ActionResult:
    """Execute a WaitForCIAction: log and wait for CI to complete.

    This action is returned when a session appears stuck but CI is still running.
    The webhook will re-trigger when CI completes, so we just log and return success.
    """
    # Log the action - CI is pending/in_progress so we wait
    details = {
        "session_id": action.session_id,
        "project_id": action.project_id,
        "ci_status": action.ci_status,
        "extended_timeout_minutes": action.extended_timeout_minutes,
        "reason": action.reason,
    }
    _log_action(
        action_type="WaitForCIAction",
        session_id=action.session_id,
        success=True,
        details=details,
        action_log_path=action_log_path,
        reason=action.reason,
    )
    return ActionResult(
        success=True,
        action_type="WaitForCIAction",
        details=details,
    )


def _send_slack_dm(message: str, channel: str | None = None) -> bool:
    """Send a Slack DM. Returns True on success."""
    token = os.environ.get("OPENCLAW_SLACK_BOT_TOKEN") or os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        return False
    target_channel = channel or JEFFREY_DM_CHANNEL
    try:
        body = json.dumps({"channel": target_channel, "text": message, "unfurl_links": False}).encode("utf-8")
        req = Request(
            "https://slack.com/api/chat.postMessage",
            data=body,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            method="POST",
        )
        with urlopen(req, timeout=5) as resp:
            return bool(json.loads(resp.read()).get("ok"))
    except Exception:
        return False


def _send_email_notification(message: str) -> bool:
    """Send an email notification. Returns True on success.

    Reads config from env vars:
      NOTIFY_EMAIL_TO    — recipient address (required)
      NOTIFY_EMAIL_FROM  — sender address (defaults to NOTIFY_EMAIL_TO)
      SMTP_HOST          — SMTP server hostname (defaults to localhost)
      SMTP_PORT          — SMTP port (defaults to 587; use 465 for SSL)
      SMTP_USER          — SMTP auth username (optional)
      SMTP_PASSWORD      — SMTP auth password (optional)
    """
    to_addr = os.environ.get("NOTIFY_EMAIL_TO")
    if not to_addr:
        return False
    from_addr = os.environ.get("NOTIFY_EMAIL_FROM", to_addr)
    smtp_host = os.environ.get("SMTP_HOST", "localhost")
    try:
        smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    except ValueError:
        return False
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_password = os.environ.get("SMTP_PASSWORD", "")

    msg = MIMEText(message)
    msg["Subject"] = "OpenClaw escalation alert"
    msg["From"] = from_addr
    msg["To"] = to_addr

    try:
        if smtp_port == 465:
            with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=10) as server:
                if smtp_user:
                    server.login(smtp_user, smtp_password)
                server.sendmail(from_addr, [to_addr], msg.as_string())
        else:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as server:
                server.ehlo()
                # Only STARTTLS for port 587; port 25 and local relays often use plain SMTP
                if smtp_port == 587:
                    server.starttls()
                    # RFC 3207: must re-issue EHLO after STARTTLS
                    server.ehlo()
                if smtp_user:
                    server.login(smtp_user, smtp_password)
                server.sendmail(from_addr, [to_addr], msg.as_string())
        return True
    except Exception:
        return False


def _make_real_notifier() -> SlackNotifier:
    """Return the production notifier that fires Slack + email in parallel."""
    class RealNotifier:
        def send_dm(self, message: str, channel: str | None = None) -> bool:
            """Send notification via Slack and email in parallel; return True if either succeeds."""
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                slack_fut = pool.submit(_send_slack_dm, message, channel)
                email_fut = pool.submit(_send_email_notification, message)
                for fut in concurrent.futures.as_completed((slack_fut, email_fut)):
                    if fut.result():
                        return True
            return False

    return RealNotifier()


def send_escalation_notification(message: str, channel: str | None = None) -> bool:
    """Send escalation notification via Slack and/or email. Returns True if either succeeds."""
    return _make_real_notifier().send_dm(message, channel)


def execute_action(
    action: EscalationAction,
    *,
    cli: AOCli | None = None,
    notifier: SlackNotifier | None = None,
    action_log_path: str = DEFAULT_ACTION_LOG_PATH,
) -> ActionResult:
    """Execute an escalation action.

    Args:
        action: The action to execute (RetryAction, KillAndRespawnAction,
                NotifyJeffreyAction, NeedsJudgmentAction, ParallelRetryAction,
                MergeAction, or WaitForCIAction)
        cli: AO CLI interface (defaults to real CLI functions if not provided)
        notifier: Slack notifier interface (required for NotifyJeffreyAction)
        action_log_path: Path to action log JSONL file

    Returns:
        ActionResult with success status and details
    """
    if cli is None:
        class RealCli:
            def send(self, session_id: str, message: str) -> None:
                ao_send(session_id, message)

            def kill(self, session_id: str) -> None:
                ao_kill(session_id)

            def spawn(self, project: str, issue: str, *, branch: str | None = None) -> str:
                return ao_spawn(project, issue, branch=branch)

        cli = RealCli()

    if notifier is None:
        notifier = _make_real_notifier()

    if isinstance(action, RetryAction):
        return _execute_retry_action(action, cli, notifier, action_log_path)
    elif isinstance(action, KillAndRespawnAction):
        return _execute_kill_and_respawn_action(action, cli, notifier, action_log_path)
    elif isinstance(action, NotifyJeffreyAction):
        return _execute_notify_jeffrey_action(action, cli, notifier, action_log_path)
    elif isinstance(action, NeedsJudgmentAction):
        return _execute_needs_judgment_action(action, cli, notifier, action_log_path)
    elif isinstance(action, ParallelRetryAction):
        return _execute_parallel_retry_action(action, cli, notifier, action_log_path)
    elif isinstance(action, MergeAction):
        return _execute_merge_action(action, cli, notifier, action_log_path)
    elif isinstance(action, WaitForCIAction):
        return _execute_wait_for_ci_action(action, cli, notifier, action_log_path)
    else:
        raise ValueError(f"Unknown action type: {type(action)}")
