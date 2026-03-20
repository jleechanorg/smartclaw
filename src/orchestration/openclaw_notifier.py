from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.request import Request, urlopen

from orchestration.datetime_util import age_seconds_from_iso, utcnow_iso
from orchestration.slack_util import normalize_slack_channel, normalize_slack_trigger_ts

# DM channel and trigger channel — set via env vars
SLACK_DM_CHANNEL = os.environ.get("SMARTCLAW_DM_CHANNEL", "")
SLACK_TRIGGER_CHANNEL = os.environ.get("SMARTCLAW_TRIGGER_CHANNEL", "")

EventSender = Callable[[dict[str, Any]], bool]

DEFAULT_OUTBOX_PATH = ".messages/outbox.jsonl"
DEFAULT_DEAD_LETTER_PATH = ".messages/outbox_dead_letter.jsonl"
DEFAULT_RETRY_LIMIT = 3
_OPENCLAW_AGENT_TIMEOUT_SECONDS = 30
_OPENCLAW_MCP_TIMEOUT_SECONDS = 30
_SLACK_POST_TIMEOUT_SECONDS = 5
_MAX_SLACK_COMPLETION_POSTS = 2


def _coerce_retry_count(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def openclaw_notification_max_runtime_seconds() -> int:
    """Return max runtime budget for a single notify_openclaw call.

    notify_openclaw performs a single delivery attempt per call:
    OpenClaw agent path + MCP fallback path.
    """
    return _OPENCLAW_AGENT_TIMEOUT_SECONDS + _OPENCLAW_MCP_TIMEOUT_SECONDS


def completion_notification_max_runtime_seconds() -> int:
    """Return max runtime budget for completion notification.

    Completion notification first calls notify_slack_done (DM + thread posts),
    then notify_openclaw. Budget for both paths.
    """
    slack_budget = _SLACK_POST_TIMEOUT_SECONDS * _MAX_SLACK_COMPLETION_POSTS
    return slack_budget + openclaw_notification_max_runtime_seconds()


def notify_openclaw(
    payload: dict[str, Any],
    *,
    send_fn: EventSender | None = None,
    outbox_path: str = DEFAULT_OUTBOX_PATH,
) -> bool:
    """Send loopback payload to OpenClaw; fallback to JSONL outbox on failure."""
    sender = send_fn or _send_via_mcp_agent_mail
    try:
        delivered = sender(payload)
    except Exception:
        delivered = False

    if delivered:
        return True

    enqueue_outbox(payload, outbox_path=outbox_path)
    return False


def drain_outbox(
    *,
    send_fn: EventSender | None = None,
    outbox_path: str = DEFAULT_OUTBOX_PATH,
    dead_letter_path: str | None = None,
    retry_limit: int = DEFAULT_RETRY_LIMIT,
) -> int:
    """Attempt to deliver queued outbox events, returning count delivered.

    Uses an atomic snapshot rename so events enqueued concurrently by
    enqueue_outbox are never overwritten by the rewrite of remaining items.
    """
    sender = send_fn or _send_via_mcp_agent_mail
    path = Path(outbox_path)

    # Derive dead_letter_path if not specified
    if dead_letter_path is None:
        dead_letter_path = str(path.parent / "outbox_dead_letter.jsonl")

    # Atomically take a snapshot: rename the live file so new enqueue_outbox
    # calls write to a fresh file, while we drain from the snapshot only.
    drain_path = path.with_suffix(".drain")
    try:
        os.replace(path, drain_path)
    except FileNotFoundError:
        return 0

    # Legacy entries may not carry _first_queued_at; use snapshot file age as fallback.
    try:
        snapshot_mtime = drain_path.stat().st_mtime
    except OSError:
        snapshot_mtime = time.time()

    delivered = 0
    remaining: list[dict[str, Any]] = []
    dead_lettered = 0
    dead_letter_items: list[dict[str, Any]] = []
    oldest_failed_age_seconds: int | None = None
    existing_dead_letter_count = len(read_dead_letter(dead_letter_path=dead_letter_path))
    for payload in _parse_jsonl_lines(drain_path.read_text(encoding="utf-8")):
        retries = _coerce_retry_count(payload.get("_retry_count"))
        try:
            ok = sender(payload)
        except Exception:
            ok = False
        if ok:
            delivered += 1
        else:
            payload["_retry_count"] = retries + 1
            payload["_last_attempt_at"] = utcnow_iso()
            if "_first_queued_at" not in payload or not payload["_first_queued_at"]:
                # Use snapshot mtime as age signal for legacy entries
                payload["_first_queued_at"] = datetime.fromtimestamp(
                    snapshot_mtime, tz=timezone.utc
                ).isoformat()
            age = age_seconds_from_iso(payload.get("_first_queued_at"))
            if age is not None:
                oldest_failed_age_seconds = (
                    age if oldest_failed_age_seconds is None else max(oldest_failed_age_seconds, age)
                )
            if payload["_retry_count"] > max(0, retry_limit):
                dead_lettered += 1
                dead_letter_items.append(payload)
                enqueue_dead_letter(payload, dead_letter_path=dead_letter_path)
            else:
                remaining.append(payload)

    # Re-enqueue failed items via append so they merge with any new events.
    for item in remaining:
        enqueue_outbox(item, outbox_path=outbox_path)

    try:
        drain_path.unlink()
    except OSError:
        pass

    # Keep dead-lettering observable for operators in supervisor logs.
    if dead_lettered:
        # Compute oldest_age_seconds for the dead-letter alert.
        oldest_age: int | None = None
        for item in dead_letter_items:
            age = age_seconds_from_iso(item.get("_first_queued_at"))
            if age is None:
                continue
            oldest_age = age if oldest_age is None else max(oldest_age, age)
        payload = {
            "event": "outbox_dead_lettered",
            "dead_lettered": dead_lettered,
            "pending_count": len(remaining),
            "dead_letter_count": existing_dead_letter_count + dead_lettered,
            "oldest_age_seconds": oldest_age,
            "outbox_path": outbox_path,
            "dead_letter_path": dead_letter_path,
        }
        notify_slack_outbox_alert(payload)

    return delivered


def _parse_jsonl_lines(text: str) -> list[dict[str, Any]]:
    """Parse JSONL text, skipping blank and malformed lines."""
    items: list[dict[str, Any]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            items.append(json.loads(line))
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
    return items


def enqueue_outbox(payload: dict[str, Any], *, outbox_path: str = DEFAULT_OUTBOX_PATH) -> None:
    normalized_payload = dict(payload)
    if "slack_trigger_ts" in normalized_payload:
        normalized_payload["slack_trigger_ts"] = normalize_slack_trigger_ts(
            normalized_payload.get("slack_trigger_ts")
        )
    if "slack_trigger_channel" in normalized_payload:
        normalized_payload["slack_trigger_channel"] = normalize_slack_channel(
            normalized_payload.get("slack_trigger_channel")
        )
    if "_retry_count" not in normalized_payload:
        normalized_payload["_retry_count"] = 0
    if "_first_queued_at" not in normalized_payload:
        normalized_payload["_first_queued_at"] = utcnow_iso()
    path = Path(outbox_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(normalized_payload, sort_keys=True))
        fh.write("\n")


def read_outbox(*, outbox_path: str = DEFAULT_OUTBOX_PATH) -> list[dict[str, Any]]:
    path = Path(outbox_path)
    try:
        return _parse_jsonl_lines(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []


def enqueue_dead_letter(
    payload: dict[str, Any], *, dead_letter_path: str = DEFAULT_DEAD_LETTER_PATH
) -> None:
    path = Path(dead_letter_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, sort_keys=True))
        fh.write("\n")


def read_dead_letter(
    *, dead_letter_path: str = DEFAULT_DEAD_LETTER_PATH
) -> list[dict[str, Any]]:
    path = Path(dead_letter_path)
    try:
        return _parse_jsonl_lines(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []


def outbox_health_snapshot(
    *,
    outbox_path: str = DEFAULT_OUTBOX_PATH,
    dead_letter_path: str = DEFAULT_DEAD_LETTER_PATH,
) -> dict[str, Any]:
    outbox = read_outbox(outbox_path=outbox_path)
    # Only load dead-letter file if it exists (avoids eager I/O on every drain).
    dead_path = Path(dead_letter_path)
    dead = read_dead_letter(dead_letter_path=dead_letter_path) if dead_path.exists() else []

    retry_histogram: dict[str, int] = {}
    oldest_age_seconds: int | None = None
    for item in outbox:
        retry = _coerce_retry_count(item.get("_retry_count"))
        key = str(retry)
        retry_histogram[key] = retry_histogram.get(key, 0) + 1
        age = age_seconds_from_iso(item.get("_first_queued_at"))
        if age is None:
            continue
        oldest_age_seconds = age if oldest_age_seconds is None else max(oldest_age_seconds, age)

    # Legacy entries may not carry _first_queued_at; use file age as fallback.
    if oldest_age_seconds is None and outbox:
        try:
            mtime = Path(outbox_path).stat().st_mtime
            oldest_age_seconds = max(0, int(time.time() - mtime))
        except OSError:
            oldest_age_seconds = None

    return {
        "pending_count": len(outbox),
        "dead_letter_count": len(dead),
        "oldest_age_seconds": oldest_age_seconds,
        "retry_histogram": retry_histogram,
    }


def notify_slack_outbox_alert(payload: dict[str, Any]) -> bool:
    """Post outbox reliability alerts to Slack DM channel.

    If payload contains a 'message' field, it will be sent as the message text
    (used by anomaly_detector for custom anomaly summaries).
    """
    token = os.environ.get("OPENCLAW_SLACK_BOT_TOKEN") or os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        return False

    # If message is provided directly (e.g., from anomaly_detector), use it
    custom_message = payload.get("message")
    if custom_message:
        text = custom_message
    else:
        pending_count = int(payload.get("pending_count", 0))
        dead_letter_count = int(payload.get("dead_letter_count", 0))
        oldest_age = payload.get("oldest_age_seconds")
        dead_lettered = int(payload.get("dead_lettered", 0))

        if dead_letter_count:
            lines = [
                ":warning: *mctrl outbox dead-lettered events*\n",
            ]
            if dead_lettered:
                lines.append(f"Moved `{dead_lettered}` event(s) to dead-letter queue.")
            lines.append(f"Dead-letter queue count: `{dead_letter_count}`")
            lines.append(f"Pending: `{pending_count}`")
            lines.append(f"Oldest age (s): `{oldest_age if oldest_age is not None else 'unknown'}`")
            lines.append(f"Outbox: `{payload.get('outbox_path', DEFAULT_OUTBOX_PATH)}`")
            lines.append(f"Dead-letter: `{payload.get('dead_letter_path', DEFAULT_DEAD_LETTER_PATH)}`")
            text = "\n".join(lines)
        else:
            text = (
                ":warning: *mctrl outbox backlog alert*\n\n"
                f"Pending: `{pending_count}`\n"
                f"Dead-letter: `{dead_letter_count}`\n"
                f"Oldest age (s): `{oldest_age if oldest_age is not None else 'unknown'}`"
            )

    try:
        body = json.dumps({"channel": SLACK_DM_CHANNEL, "text": text}).encode("utf-8")
        req = Request(
            "https://slack.com/api/chat.postMessage",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
            method="POST",
        )
        with urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read())
            return bool(result.get("ok"))
    except Exception:
        return False


def notify_slack_started(payload: dict[str, Any]) -> bool:
    """Post task-started notification to Slack. Best-effort, never blocks.

    Posts a DM to jleechan. If slack_trigger_ts is set, also threads a reply
    under the original trigger message in #ai-slack-test.
    """
    token = os.environ.get("OPENCLAW_SLACK_BOT_TOKEN") or os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        return False

    bead_id = str(payload.get("bead_id", "unknown"))
    branch = str(payload.get("branch", "unknown"))
    worktree = str(payload.get("worktree_path", "unknown"))
    session = str(payload.get("session", "unknown"))
    agent_cli = str(payload.get("agent_cli", "unknown"))
    trigger_ts = normalize_slack_trigger_ts(payload.get("slack_trigger_ts"))
    # Key absent → use default channel; explicit None/""/"None" → no thread.
    _has_channel = "slack_trigger_channel" in payload
    _raw_channel = payload.get("slack_trigger_channel") if _has_channel else SLACK_TRIGGER_CHANNEL
    trigger_channel = normalize_slack_channel(_raw_channel)

    dm_text = (
        f":rocket: *Task started: {bead_id}*\n\n"
        f"Agent `{agent_cli}` running in session `{session}`.\n"
        f"Branch: `{branch}`\n"
        f"Worktree: `{worktree}`"
    )
    thread_text = f":rocket: Agent started — *{bead_id}* running in `{session}` on `{branch}`."

    posts: list[dict[str, Any]] = [{"channel": SLACK_DM_CHANNEL, "text": dm_text}]
    if trigger_ts and trigger_channel:
        posts.append({
            "channel": trigger_channel,
            "text": thread_text,
            "thread_ts": trigger_ts,
        })

    success = True
    for post_body in posts:
        try:
            body = json.dumps(post_body).encode("utf-8")
            req = Request(
                "https://slack.com/api/chat.postMessage",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {token}",
                },
                method="POST",
            )
            with urlopen(req, timeout=5) as resp:
                result = json.loads(resp.read())
                if not result.get("ok"):
                    success = False
        except Exception:
            success = False
    return success


def notify_slack_done(payload: dict[str, Any]) -> bool:
    """Post task-done notification to Slack. Best-effort, never blocks.

    Posts a DM to jleechan. If slack_trigger_ts is set, also threads a reply
    under the original trigger message in #ai-slack-test.
    Uses OPENCLAW_SLACK_BOT_TOKEN only — never falls back to user token.
    """
    # Use bot token only — never fall back to SLACK_USER_TOKEN (that posts as
    # jleechan, not the openclaw bot, which is the wrong sender for completions).
    token = os.environ.get("OPENCLAW_SLACK_BOT_TOKEN") or os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        return False

    bead_id = str(payload.get("bead_id", "unknown"))
    branch = str(payload.get("branch", "unknown"))
    worktree = str(payload.get("worktree_path", "unknown"))
    event = payload.get("event", "task_needs_human")
    session = str(payload.get("session", "unknown"))
    trigger_ts = normalize_slack_trigger_ts(payload.get("slack_trigger_ts"))
    _has_channel = "slack_trigger_channel" in payload
    _raw_channel = payload.get("slack_trigger_channel") if _has_channel else SLACK_TRIGGER_CHANNEL
    trigger_channel = normalize_slack_channel(_raw_channel)

    if event == "task_finished":
        dm_text = (
            f":white_check_mark: *Task done: {bead_id}*\n\n"
            f"Agent committed work to `{branch}`.\n"
            f"Worktree: `{worktree}`\n"
            f"Review and merge when ready."
        )
        thread_text = f":done: Agent done — *{bead_id}* committed to `{branch}`. Ready for review."
    else:
        # task_needs_human: agent exited without committing (stall/crash/timeout)
        action_required = payload.get("action_required", "")
        if action_required == "push_or_salvage":
            detail = f"Agent session `{session}` did not push to a configured remote.\nBranch: `{branch}` may have local-only commits."
        else:
            detail = f"Agent session `{session}` exited without committing.\nBranch: `{branch}`"
        dm_text = (
            f":warning: *Task stalled: {bead_id}*\n\n"
            f"{detail}\n"
            f"Worktree: `{worktree}`\n"
            f"Investigate and relaunch if needed."
        )
        thread_text = f":warning: Agent for *{bead_id}* exited without commits — may need relaunch. Branch: `{branch}`."

    posts: list[dict[str, Any]] = [{"channel": SLACK_DM_CHANNEL, "text": dm_text}]
    # Thread the completion reply under the original trigger message if we have its ts.
    if trigger_ts and trigger_channel:
        posts.append({
            "channel": trigger_channel,
            "text": thread_text,
            "thread_ts": trigger_ts,
        })

    success = True
    for post_body in posts:
        try:
            body = json.dumps(post_body).encode("utf-8")
            req = Request(
                "https://slack.com/api/chat.postMessage",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {token}",
                },
                method="POST",
            )
            with urlopen(req, timeout=5) as resp:
                result = json.loads(resp.read())
                if not result.get("ok"):
                    success = False
        except Exception:
            success = False
    return success


def _send_via_mcp_agent_mail(payload: dict[str, Any]) -> bool:
    """Best-effort OpenClaw delivery.

    Preferred env vars:
    - OPENCLAW_NOTIFY_AGENT

    Legacy env vars:
    - OPENCLAW_PROJECT_KEY
    - OPENCLAW_SENDER_NAME
    - OPENCLAW_TO

    We try the live OpenClaw agent route first because current OpenClaw CLI
    builds do not expose an `mcp` subcommand consistently.
    """
    try:
        if _send_via_openclaw_agent(payload):
            return True
    except Exception:
        pass

    project_key = os.environ.get("OPENCLAW_PROJECT_KEY", "").strip()
    sender_name = os.environ.get("OPENCLAW_SENDER_NAME", "").strip()
    to = os.environ.get("OPENCLAW_TO", "").strip()
    if not project_key or not sender_name or not to:
        return False

    event_type = str(payload.get("event", "task_update"))
    bead_id = str(payload.get("bead_id", "unknown"))
    subject = f"{event_type}: {bead_id}"
    body = json.dumps(payload, indent=2, sort_keys=True)

    try:
        result = subprocess.run(
            [
                "openclaw",
                "mcp",
                "call",
                "mcp-agent-mail",
                "send_message",
                "--project_key",
                project_key,
                "--sender_name",
                sender_name,
                "--to",
                to,
                "--subject",
                subject,
                "--body_md",
                f"```json\n{body}\n```",
            ],
            capture_output=True,
            text=True,
            timeout=_OPENCLAW_MCP_TIMEOUT_SECONDS,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def _send_via_openclaw_agent(payload: dict[str, Any]) -> bool:
    """Deliver the notification to a configured OpenClaw agent."""
    agent_name = os.environ.get("OPENCLAW_NOTIFY_AGENT", "main").strip() or "main"

    event_type = str(payload.get("event", "task_update"))
    bead_id = str(payload.get("bead_id", "unknown"))
    body = json.dumps(payload, indent=2, sort_keys=True)
    message = (
        f"Notification from mctrl.\n"
        f"Event: {event_type}\n"
        f"Bead: {bead_id}\n\n"
        f"```json\n{body}\n```"
    )
    try:
        result = subprocess.run(
            [
                "openclaw",
                "agent",
                "--agent",
                agent_name,
                "--message",
                message,
                "--json",
            ],
            capture_output=True,
            text=True,
            timeout=_OPENCLAW_AGENT_TIMEOUT_SECONDS,  # Prevent indefinite hang on OpenClaw CLI issues
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False
    if result.returncode != 0:
        return False
    # Validate the JSON response indicates successful delivery, not just clean exit.
    # openclaw --json returns {"success": true} or {"error": "..."} on failure.
    try:
        response = json.loads(result.stdout)
        # Accept if response explicitly says success, or has no error field
        if isinstance(response, dict):
            if response.get("error"):
                return False
            if "success" in response:
                return bool(response["success"])
        # Non-dict or unknown shape but exit 0 — treat as delivered
        return True
    except (json.JSONDecodeError, ValueError, TypeError):
        # stdout wasn't JSON or was None — exit 0 still means delivery command ran
        return True
