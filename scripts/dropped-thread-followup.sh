#!/usr/bin/env bash
# dropped-thread-followup.sh
# Audits Slack channels for dropped agent threads and takes action.
# Runs every 4 hours via launchd.
#
# DROP DETECTION — strict criteria (avoid false positives / spam):
#   1. Agent explicitly admitted: "I did not execute it yet", "I only sent an acknowledgment"
#   2. Agent acknowledged but no AO dispatch / PR / commit / action was taken
#   3. Thread is unresolved AND > 2h old AND user asked agent to do work
#   4. Agent message indicates timeout / gateway overload / deadline — counts as dropped (kind: timeout-failure)
#
# Idempotency: tracks last-nudged ts per (channel, thread_ts) in a JSON state file.
# Only re-nudges if > DROP_NUDGE_INTERVAL_SECS (default: 30m) have passed.
#
# Guardrails:
#   - DRY_RUN=1: prints actions without executing
#   - IS_SOURCED=1: allows source for test coverage without running main
#   - Overlap lock prevents concurrent runs
#
# Env: DROP_EXCLUDE_CHANNELS — unset → default skip ${SLACK_CHANNEL_ID}; set to "" → skip nothing;
#   set to space-separated IDs to exclude only those. (Do not use bash :- for "empty means none".)
#   DROP_THREAD_REPLY_LIMIT — conversations.replies limit (default 200).
#   DROP_JEFFREY_ONLY_CHANNELS — unset → default ${SLACK_CHANNEL_ID}; set to "" → no jeffrey-only gating.

set -euo pipefail

# Guard: ensure basic commands are always available even in restricted launchd PATH
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin${PATH:+:$PATH}"

# Suppress SIGPIPE — python3 heredoc can exit before echo finishes piping,
# producing "echo: write error: Broken pipe" under set -euo pipefail
trap '' PIPE

# ── Config ────────────────────────────────────────────────────────────────────
LOCK_DIR="${DROP_LOCK_DIR:-${TMPDIR:-/tmp}/hermes-dropped-thread.lock}"
LOG_DIR="${DROP_LOG_DIR:-${HOME}/.hermes_prod/logs}"
STATE_FILE="${DROP_STATE_FILE:-$HOME/.hermes_prod/logs/dropped-thread-state.json}"
NUDGE_INTERVAL_SECS="${DROP_NUDGE_INTERVAL_SECS:-1800}"   # 30 minutes default
LOOKBACK_HOURS="${DROP_LOOKBACK_HOURS:-8}"               # scan last N hours
PROGRESS_STALE_MINUTES="${DROP_PROGRESS_STALE_MINUTES:-5}"  # dispatched task with no progress
# conversations.replies fetch size (Slack allows up to 1000; default 200 for long threads)
DROP_THREAD_REPLY_LIMIT="${DROP_THREAD_REPLY_LIMIT:-200}"
# Space-separated channel IDs: cold/stale/followup nudges apply only if Jeffrey posted in-thread.
# Unset → default ${SLACK_CHANNEL_ID} (#all-jleechan-ai). Set to "" to disable jeffrey-only gating everywhere.
if [[ "${DROP_JEFFREY_ONLY_CHANNELS+x}" = x ]]; then
  JEFFREY_ONLY_CHANNELS="$DROP_JEFFREY_ONLY_CHANNELS"
else
  JEFFREY_ONLY_CHANNELS="${SLACK_CHANNEL_ID}"
fi
POST_AS_BOT="${DROP_POST_AS_BOT:-1}"                      # 0 = post as user
AGENT_USER_ID="${HERMES_BOT_USER_ID:-${OPENCLAW_BOT_USER_ID:-U0AEZC7RX1Q}}"  # bot user ID for classification
JEFFREY_USER_ID="${JLEECHAN_USER_ID:-U09GH5BR3QU}"        # Jeffrey's Slack user ID (standalone msg detection)

mkdir -p "$LOG_DIR"
mkdir -p "$(dirname "$STATE_FILE")"

# ── Helpers ───────────────────────────────────────────────────────────────────

log() { echo "[$(date '+%Y-%m-%dT%H:%M:%S')] $*"; }

# Overlap lock (skip when sourced for tests)
if [[ "${IS_SOURCED:-0}" != "1" ]]; then
  if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    log "SKIP: another instance running"
    exit 0
  fi
  trap 'rmdir "$LOCK_DIR" >/dev/null 2>&1 || true' EXIT
fi

# Load persisted state
load_state() {
  if [[ -f "$STATE_FILE" ]]; then
    cat "$STATE_FILE" 2>/dev/null || echo '{}'
  else
    echo '{}'
  fi
}

# Save persisted state (atomically)
save_state() {
  local tmp
  tmp="$(mktemp "$STATE_FILE.XXXXXX")"
  cat > "$tmp" < /dev/stdin
  mv "$tmp" "$STATE_FILE"
}

# Check if thread was nudged recently (idempotency guard)
was_nudged_recently() {
  local channel_id=$1 thread_ts=$2
  local state last_ts now_sec ts_sec
  state="$(load_state)"
  last_ts="$(jq -rn "$state | .nudged.\"${channel_id}_${thread_ts}\" // empty" 2>/dev/null)" || last_ts=""
  [[ -z "$last_ts" || "$last_ts" == "null" ]] && return 1
  now_sec="$(date +%s)"
  ts_sec="$(date -u -j -f "%Y-%m-%dT%H:%M:%SZ" "$last_ts" '+%s' 2>/dev/null)" || return 0
  [[ $((now_sec - ts_sec)) -lt $NUDGE_INTERVAL_SECS ]] && return 0
  return 1
}

# Record nudge timestamp
record_nudge() {
  local channel_id=$1 thread_ts=$2
  local state now_iso
  state="$(load_state)"
  now_iso="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  echo "$state" | jq --arg k "${channel_id}_${thread_ts}" --arg v "$now_iso" \
    '.nudged[$k] = $v' | save_state
}

# ── Thread analysis ─────────────────────────────────────────────────────────────
#
# Returns 0 (drop detected) if thread meets ALL of:
#   - At least one user message asking agent to do / investigate / build / fix something
#   - Agent replied with an acknowledgment but no real action taken
#   - Agent explicitly admitted not executing ("did not execute", "only sent an ack", etc.)
#   - Thread is > 2 hours old OR last agent reply is > 1h old
#
# Also returns 0 if thread:
#   - Has > 2 user messages in last LOOKBACK_HOURS
#   - Last reply was from agent > 2h ago
#   - No recent agent reply
#
# Returns 1 (no drop) if:
#   - Thread has a recent agent reply (last 30 min)
#   - Agent completed work (PR/commit/posted result)
#   - Thread is purely informational / human-only
#
# Output: JSON with {admitted, user_asked, last_agent_reply, hours_old, action_needed, reason}
analyze_thread() {
  local channel_id=$1 thread_ts=$2 user_msgs=$3 agent_msgs=$4 last_reply_ts=$5 messages_json=$6 agent_user_id=$7

  local hours_old
  hours_old="$(echo "$user_msgs $agent_msgs" | python3 - "$channel_id" "$thread_ts" "$last_reply_ts" "${LOOKBACK_HOURS}" "${agent_user_id}" <<'PYEOF'
import sys, json
channel_id = sys.argv[1]
thread_ts  = sys.argv[2]
last_reply = sys.argv[3]  # ISO timestamp of most recent reply
lookback_h = int(sys.argv[4])

from datetime import datetime, timezone

now_sec = __import__('time').time()
try:
    last_sec = datetime.strptime(last_reply, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
except ValueError:
    last_sec = 0

h_age = (now_sec - last_sec) / 3600 if last_sec else 999
print(round(h_age, 2))
PYEOF
)" 2>/dev/null || hours_old=999

  python3 - "$user_msgs" "$agent_msgs" "$hours_old" "$messages_json" "$agent_user_id" "$PROGRESS_STALE_MINUTES" "$channel_id" "$JEFFREY_USER_ID" "$JEFFREY_ONLY_CHANNELS" <<'PYEOF'
import re
import sys, json

user_msgs   = int(sys.argv[1]) if sys.argv[1] else 0
agent_msgs  = int(sys.argv[2]) if sys.argv[2] else 0
hours_old   = float(sys.argv[3]) if sys.argv[3] else 999
messages    = json.loads(sys.argv[4]) if sys.argv[4] else []
AGENT_ID    = sys.argv[5]
progress_stale_minutes = int(sys.argv[6]) if sys.argv[6] else 10
progress_stale_h = progress_stale_minutes / 60.0
CHANNEL_ID  = sys.argv[7] if len(sys.argv) > 7 else ""
JEFFREY_ID  = sys.argv[8] if len(sys.argv) > 8 else ""
JEFFREY_ONLY_CHANS = [x for x in (sys.argv[9] if len(sys.argv) > 9 else "").split() if x]
now_sec     = __import__('time').time()

# Build a combined text of all agent replies for phrase scanning
agent_texts = [m.get("text", "") for m in messages if m.get("user") == AGENT_ID]
agent_text  = " ".join(agent_texts).lower()

# Basic temporal ordering (Slack ts are numeric strings)
def ts_float(m):
    try:
        return float(m.get("ts", 0) or 0)
    except Exception:
        return 0.0

agent_msgs_list = [m for m in messages if m.get("user") == AGENT_ID]
user_msgs_list = [m for m in messages if m.get("user") and m.get("user") != AGENT_ID]
last_agent = max(agent_msgs_list, key=ts_float) if agent_msgs_list else None
last_user = max(user_msgs_list, key=ts_float) if user_msgs_list else None
last_agent_ts = ts_float(last_agent) if last_agent else 0.0
last_user_ts = ts_float(last_user) if last_user else 0.0
last_user_text = (last_user.get("text", "").lower() if last_user else "")
user_after_agent = bool(last_user_ts and (last_user_ts > last_agent_ts))
minutes_since_last_user = ((now_sec - last_user_ts) / 60.0) if last_user_ts else 999.0

# Admission phrases — agent explicitly said it didn't act
ADMISSION_PHRASES = [
    "did not execute", "not execute", "only sent an acknowledgment",
    "have not started", "have not done", "have not yet done",
    "i only", "i haven't", "i did not", "i am not currently working on",
    "stalled", "dropped", "forgot to", "missed this",
]

# Strong result indicators — agent actually completed something
STRONG_RESULT_PHRASES = [
    "pr #", "pull/", "commit ", "pushed", "posted result",
    "merged", "created a ", "file://", "http://", "https://",
    "task complete", "completed the", "finished the", "all done", "shipped", "deployed",
]
PROGRESS_PHRASES = [
    "still working", "in progress", "currently working", "blocked",
    "investigating", "working on", "waiting on", "follow up", "follow-up",
]

admitted = any(phrase in agent_text for phrase in ADMISSION_PHRASES)


def _has_weak_completion_word(text: str) -> bool:
    """Bare 'done'/'finished'/'completed' only count if reply is substantive (avoid 'done' as sole token)."""
    t = text.lower()
    if len(t) < 80:
        return False
    return bool(re.search(r"\b(done|finished|completed)\b", t))


has_result = (
    any(phrase in agent_text for phrase in STRONG_RESULT_PHRASES)
    or _has_weak_completion_word(agent_text)
)
has_progress_reply = any(phrase in agent_text for phrase in PROGRESS_PHRASES)
dispatched_ao = "spawning agent for" in agent_text or "session " in agent_text and " created" in agent_text


def _agent_reported_timeout(text: str) -> bool:
    """OpenClaw/LLM timeout or gateway overload — user-visible failure, not a completed task."""
    return bool(
        re.search(
            r"request timed out|timed out before|timeout before a response|"
            r"gateway timeout|error:\s*timeout|deadline exceeded|\brequest timeout\b|"
            r"\btimed out\b|cluster is under high load|server cluster is under high load|"
            r"2064.*high load|high load.*2064",
            text,
            re.I,
        )
    )


agent_timeout_observed = _agent_reported_timeout(agent_text)

# Check if bot gave a substantive informational answer (not just an ack)
# Total chars across all bot messages — substantive = >200 chars (filters out short acks)
# Timeout/error walls must NOT count as Q&A (they often exceed 200 chars).
total_bot_chars = sum(len(m.get("text", "")) for m in messages if m.get("user") == AGENT_ID)
bot_gave_substantive_answer = (
    total_bot_chars > 200
    and not has_result
    and not admitted
    and not agent_timeout_observed
)

# No user messages → nothing to have dropped
if user_msgs == 0:
    print(json.dumps({"admitted": False, "action_needed": False, "reason": "no user asks", "kind": "none"}))
    sys.exit(0)


def _is_assistant_boilerplate(text: str) -> bool:
    """Skip template intros pasted as the only 'user' message (false-positive cold threads)."""
    t = (text or "").strip().lower()
    if not t:
        return False
    if "hello! i'm claude" in t and "anthropic" in t:
        return True
    if "i'm claude" in t and "ai assistant" in t and "anthropic" in t:
        return True
    if t.startswith("hello! i'm an ai assistant"):
        return True
    return False


def _first_user_msg():
    return min(user_msgs_list, key=ts_float) if user_msgs_list else None


if len(user_msgs_list) == 1 and _is_assistant_boilerplate(user_msgs_list[0].get("text", "")):
    print(json.dumps({
        "admitted": False,
        "action_needed": False,
        "reason": "single user message is assistant boilerplate, not a task",
        "kind": "none",
    }))
    sys.exit(0)


def _is_automated_report(text: str) -> bool:
    """Cron/automation posts (bug hunt, scan summaries, monitor-e2e, canary) — not operator tasks."""
    t = (text or "").strip().lower()
    if not t:
        return False
    if "*daily bug hunt report*" in t or "daily bug hunt report" in t[:800]:
        return True
    if "*repos scanned:*" in t or "*repos scanned*" in t:
        return True
    if "*period:*" in t and "*prs reviewed*" in t:
        return True
    if t.startswith("*weekly") and "report" in t[:120]:
        return True
    # Monitor E2E and canary test messages — automated infrastructure probes
    if "[monitor-e2e]" in t or "[canary" in t:
        return True
    if "canary thread test" in t:
        return True
    # Monitor ping/status reports — automated health checks, not operator tasks
    if "*openclaw monitor*" in t or "*hermes monitor*" in t:
        return True
    if t.startswith("status=") or ("status=" in t[:80] and ("pass=" in t or "fail=" in t)):
        return True
    return False


def _root_automated_report() -> bool:
    if not user_msgs_list:
        return False
    first_u = min(user_msgs_list, key=ts_float)
    return _is_automated_report(first_u.get("text", ""))


def _thread_has_actionable_user_request() -> bool:
    """Cold-thread nudge only if at least one human line looks like a real ask (not boilerplate/report)."""
    min_chars = 4 if CHANNEL_ID.startswith("D") else 40
    for m in user_msgs_list:
        t = (m.get("text") or "").strip()
        if not t:
            continue
        if _is_assistant_boilerplate(t):
            continue
        if _is_automated_report(t):
            continue
        if "<@" in t:
            return True
        if len(t) >= min_chars:
            return True
    return False


def _jeffrey_participates() -> bool:
    return bool(JEFFREY_ID and any(m.get("user") == JEFFREY_ID for m in user_msgs_list))


def _jeffrey_only_skip() -> bool:
    return bool(
        JEFFREY_ONLY_CHANS
        and CHANNEL_ID in JEFFREY_ONLY_CHANS
        and JEFFREY_ID
        and not _jeffrey_participates()
    )


def _emit_jeffrey_only_skip():
    print(json.dumps({
        "admitted": False,
        "action_needed": False,
        "reason": "jeffrey-only channel: no message from operator in thread",
        "kind": "none",
    }))
    sys.exit(0)

# User followed up after the last agent reply and has waited long enough.
# This catches "new ask in old thread" cases that were previously missed.
ACTION_VERBS = [
    "fix", "update", "check", "verify", "merge", "drive", "make sure", "follow up",
    "please", "retry", "status", "why", "can you", "do ", "run ", "ship", "review",
]
looks_actionable = any(v in last_user_text for v in ACTION_VERBS) or ("<@" in last_user_text)
if user_after_agent and minutes_since_last_user >= 5 and looks_actionable:
    if _root_automated_report():
        print(json.dumps({"admitted": False, "action_needed": False,
                           "reason": "thread root is automated report (monitor/canary)", "kind": "none"}))
        sys.exit(0)
    if _jeffrey_only_skip():
        _emit_jeffrey_only_skip()
    print(json.dumps({
        "admitted": admitted,
        "action_needed": True,
        "reason": f"user follow-up pending ({minutes_since_last_user:.0f}m) after last agent reply",
        "kind": "followup-pending",
    }))
    sys.exit(0)

# Agent posted timeout / overload / deadline failure — treat as dropped work (not resolved Q&A)
if agent_timeout_observed and not has_result and _thread_has_actionable_user_request():
    if _jeffrey_only_skip():
        _emit_jeffrey_only_skip()
    print(json.dumps({
        "admitted": admitted,
        "action_needed": True,
        "reason": "agent reply indicates timeout or overload — counts as dropped thread until retried or explained",
        "kind": "timeout-failure",
    }))
    sys.exit(0)

# Agent replied recently AND completed work → not a drop
if hours_old < 0.5 and has_result and not user_after_agent:
    print(json.dumps({"admitted": False, "action_needed": False, "reason": "recent agent reply with result", "kind": "none"}))
    sys.exit(0)

# Agent replied recently but didn't complete work AND admission present → nudge
if hours_old < 0.5 and admitted:
    if _root_automated_report():
        print(json.dumps({"admitted": False, "action_needed": False,
                           "reason": "thread root is automated report (monitor/canary)", "kind": "none"}))
        sys.exit(0)
    if _jeffrey_only_skip():
        _emit_jeffrey_only_skip()
    print(json.dumps({"admitted": True, "action_needed": True,
                       "reason": f"recent reply with admission, {hours_old:.1f}h old", "kind": "admission"}))
    sys.exit(0)

# Long-running dispatched task with no progress update.
if dispatched_ao and hours_old >= progress_stale_h and not has_result and not has_progress_reply:
    if _root_automated_report():
        print(json.dumps({"admitted": False, "action_needed": False,
                           "reason": "thread root is automated report, not an AO task", "kind": "none"}))
        sys.exit(0)
    if _jeffrey_only_skip():
        _emit_jeffrey_only_skip()
    print(json.dumps({
        "admitted": admitted,
        "action_needed": True,
        "reason": f"dispatched task stale ({hours_old:.1f}h) without progress update",
        "kind": "stale-dispatch",
    }))
    sys.exit(0)

# Cold thread (>2h) AND user asked AND no recent result → nudge
# But skip if bot gave a substantive informational answer (Q&A thread — not a dropped task)
if user_msgs > 0 and hours_old > 2.0 and not has_result:
    if bot_gave_substantive_answer:
        print(json.dumps({"admitted": admitted, "action_needed": False,
                           "reason": f"bot answered substantively ({total_bot_chars} chars), treating as resolved Q&A", "kind": "none"}))
        sys.exit(0)
    if _root_automated_report():
        print(json.dumps({"admitted": False, "action_needed": False,
                           "reason": "thread root is automated report, not a task thread", "kind": "none"}))
        sys.exit(0)
    # First message is assistant boilerplate and no later human follow-up → not a task thread
    _fu = _first_user_msg()
    if _fu and _is_assistant_boilerplate(_fu.get("text", "")):
        _later = [m for m in user_msgs_list if ts_float(m) > ts_float(_fu)]
        if not _later:
            print(json.dumps({"admitted": False, "action_needed": False,
                               "reason": "assistant boilerplate root with no follow-up, not a task", "kind": "none"}))
            sys.exit(0)
    if not _thread_has_actionable_user_request():
        print(json.dumps({"admitted": False, "action_needed": False,
                           "reason": "no actionable user request (boilerplate/automation only)", "kind": "none"}))
        sys.exit(0)
    if _jeffrey_only_skip():
        _emit_jeffrey_only_skip()
    print(json.dumps({"admitted": admitted, "action_needed": True,
                       "reason": f"cold thread {hours_old:.1f}h, no result found", "kind": "cold-thread"}))
    sys.exit(0)

print(json.dumps({"admitted": admitted, "action_needed": False,
                   "reason": "thread active, recent reply, or result present", "kind": "none"}))
PYEOF
}

# ── Slack token resolution ─────────────────────────────────────────────────────
# SLACK_TOKEN (reads): OpenClaw bot primary (broader channel access).
# post_reply (writes): MCP mail bot (U0A4G7LDJ4R) primary, OpenClaw fallback.
resolve_mcp_mail_token() {
  local creds="${HOME}/.mcp_mail/credentials.json"
  if [[ -f "$creds" ]] && command -v python3 >/dev/null 2>&1; then
    python3 - "$creds" <<'PYEOF'
import json, sys
try:
    with open(sys.argv[1]) as f:
        d = json.load(f)
    print(d.get("SLACK_BOT_TOKEN", ""))
except Exception:
    pass
PYEOF
  fi
}

# MCP mail bot token (from env or credentials file)
MCP_MAIL_BOT_TOKEN="${MCP_MAIL_SLACK_TOKEN:-$(resolve_mcp_mail_token)}"

# ── Slack API via curl ──────────────────────────────────────────────────────────
# Uses MCP mail bot (U0A4G7LDJ4R) for posting nudge messages.
# Falls back to OpenClaw bot (U0AEZC7RX1Q) only if MCP mail bot unavailable.
SLACK_TOKEN="${SLACK_BOT_TOKEN:-${SLACK_BOT_TOKEN:-${MCP_MAIL_BOT_TOKEN:-}}}"

resolve_channels() {
  local config="${HERMES_CONFIG_FILE:-${OPENCLAW_CONFIG_FILE:-${HOME}/.smartclaw/openclaw.json}}"
  if [[ -f "$config" ]] && command -v python3 >/dev/null 2>&1; then
    python3 - "$config" <<'PYEOF'
import json, sys
try:
    with open(sys.argv[1]) as f:
        d = json.load(f)
    ch = d.get("channels", {}).get("slack", {}).get("channels", {})
    ids = [k for k in ch if k != "*"]
    print(" ".join(ids))
except Exception:
    pass
PYEOF
  fi
}

DEFAULT_CHANNELS="${DROP_CHANNELS:-$(resolve_channels)}"
DEFAULT_CHANNELS="${DEFAULT_CHANNELS:-${SLACK_CHANNEL_ID} C0AJQ5M0A0Y}"

# Channels excluded from dropped-thread scanning (#all-jleechan-ai is high-churn).
# Semantics: unset → default exclude ${SLACK_CHANNEL_ID} | explicitly set (incl. empty) → use that list
# (empty = exclude nothing). Using :- would treat "" as unset and wrongly re-apply default.
if [[ "${DROP_EXCLUDE_CHANNELS+x}" = x ]]; then
  EXCLUDE_CHANNELS="$DROP_EXCLUDE_CHANNELS"
else
  EXCLUDE_CHANNELS="${SLACK_CHANNEL_ID}"
fi
filter_channels() {
  local result=""
  for ch in $DEFAULT_CHANNELS; do
    local excluded=0
    for ex in $EXCLUDE_CHANNELS; do
      [[ "$ch" == "$ex" ]] && excluded=1 && break
    done
    [[ "$excluded" == "0" ]] && result="${result}${result:+ }$ch"
  done
  echo "$result"
}

# Always include DM channel — resolve_channels() only returns C-prefixed IDs from config.
# DM channels (D-prefix) are never in that list, so we add it unless already present.
DM_CHANNEL="${JLEECHAN_DM_CHANNEL:-${SLACK_CHANNEL_ID}}"
case " ${DEFAULT_CHANNELS} " in
  *" ${DM_CHANNEL} "*) : ;;  # already present
  *) DEFAULT_CHANNELS="${DEFAULT_CHANNELS} ${DM_CHANNEL}" ;;
esac

# Must run after DM is merged into DEFAULT_CHANNELS (otherwise DMs are never scanned).
SCAN_CHANNELS="$(filter_channels)"

fetch_thread_messages() {
  local channel_id=$1 thread_ts=$2
  local response
  response="$(curl --silent --show-error \
    --connect-timeout 10 --max-time 30 \
    --get "https://slack.com/api/conversations.replies" \
    --data-urlencode "channel=${channel_id}" \
    --data-urlencode "ts=${thread_ts}" \
    --data-urlencode "limit=${DROP_THREAD_REPLY_LIMIT}" \
    -H "Authorization: Bearer $SLACK_TOKEN" 2>/dev/null)" || return 1
  echo "$response" | jq -ce '
    if .ok == true then (.messages // [])
    else error(.error // "slack_api_error")
    end
  ' 2>/dev/null || return 1
}

fetch_recent_threads() {
  local channel_id=$1
  local oldest_ts now_sec
  now_sec="$(date +%s)"
  oldest_ts=$((now_sec - LOOKBACK_HOURS * 3600))

  local response
  response="$(curl --silent --show-error \
    --connect-timeout 10 --max-time 30 \
    -X POST "https://slack.com/api/conversations.history" \
    -H "Authorization: Bearer $SLACK_TOKEN" \
    -H "Content-Type: application/json" \
    -d "$(jq -n --arg ch "$channel_id" --arg oldest "$oldest_ts" \
      --argjson limit 200 \
      '{channel: $ch, oldest: $oldest, limit: $limit}')" 2>/dev/null)" || return 1

  # Extract thread_ts values (roots of threads)
  echo "$response" | jq -r '.messages[] | select(.reply_count > 0) | .ts' 2>/dev/null || return 1
}

# Fetch standalone (non-threaded) messages from a user that have no agent reply.
# These are missed by fetch_recent_threads because reply_count == 0.
# Returns: one ts per line (Jeffrey messages with no bot follow-up within 30 min).
fetch_standalone_user_messages() {
  local channel_id=$1
  local oldest_ts now_sec cutoff_ts
  now_sec="$(date +%s)"
  oldest_ts=$((now_sec - LOOKBACK_HOURS * 3600))
  cutoff_ts=$((now_sec - NUDGE_INTERVAL_SECS))  # must be > 30 min old

  local response
  response="$(curl --silent --show-error \
    --connect-timeout 10 --max-time 30 \
    -X POST "https://slack.com/api/conversations.history" \
    -H "Authorization: Bearer $SLACK_TOKEN" \
    -H "Content-Type: application/json" \
    -d "$(jq -n --arg ch "$channel_id" --arg oldest "$oldest_ts" \
      --argjson limit 200 \
      '{channel: $ch, oldest: $oldest, limit: $limit}')" 2>/dev/null)" || return 1

  # Find Jeffrey's standalone roots (reply_count==0) older than NUDGE_INTERVAL_SECS.
  # Also check that no agent message appeared within 30 min after the Jeffrey message.
  # Use a temp file to pass response — pipe+heredoc conflict (both claim stdin).
  local _tmpf
  _tmpf="$(mktemp /tmp/slack-standalone.XXXXXX)"
  echo "$response" > "$_tmpf"
  python3 - "$JEFFREY_USER_ID" "$AGENT_USER_ID" "$cutoff_ts" "$_tmpf" <<'PYEOF'
import sys, json, os
jeffrey_id = sys.argv[1]
agent_id   = sys.argv[2]
cutoff     = float(sys.argv[3])
tmpf       = sys.argv[4]


def _is_automated_report(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return False
    if "*daily bug hunt report*" in t or "daily bug hunt report" in t[:800]:
        return True
    if "*repos scanned:*" in t or "*repos scanned*" in t:
        return True
    if "*period:*" in t and "*prs reviewed*" in t:
        return True
    if t.startswith("*weekly") and "report" in t[:120]:
        return True
    # Monitor E2E and canary test messages — automated infrastructure probes
    if "[monitor-e2e]" in t or "[canary" in t:
        return True
    if "canary thread test" in t:
        return True
    # Monitor ping/status reports — automated health checks, not operator tasks
    if "*openclaw monitor*" in t or "*hermes monitor*" in t:
        return True
    if t.startswith("status=") or ("status=" in t[:80] and ("pass=" in t or "fail=" in t)):
        return True
    return False

try:
    with open(tmpf) as f:
        data = json.load(f)
    msgs = data.get("messages", [])
except Exception:
    sys.exit(0)
finally:
    try:
        os.unlink(tmpf)
    except Exception:
        pass

# Build a sorted list with timestamps as floats
for m in msgs:
    try:
        m["_ts"] = float(m.get("ts", 0))
    except Exception:
        m["_ts"] = 0.0

msgs_sorted = sorted(msgs, key=lambda m: m["_ts"])

for i, m in enumerate(msgs_sorted):
    if m.get("user") != jeffrey_id:
        continue
    if m.get("subtype"):  # skip joins, leaves, bot_messages, etc.
        continue
    if (m.get("reply_count") or 0) > 0:
        continue  # has thread replies — already handled by fetch_recent_threads
    if m.get("thread_ts") and m["thread_ts"] != m.get("ts"):
        continue  # it's a reply inside another thread, not a root
    if m["_ts"] > cutoff:
        continue  # too recent — not a drop yet

    # Check if the agent replied in the channel within 30 min after this message
    window_end = m["_ts"] + 1800  # 30 min
    agent_replied = any(
        n.get("user") == agent_id and n["_ts"] > m["_ts"] and n["_ts"] <= window_end
        for n in msgs_sorted[i+1:]
    )
    if not agent_replied:
        if _is_automated_report(m.get("text", "")):
            continue
        print(m["ts"])
PYEOF
}

post_reply() {
  local channel_id=$1 thread_ts=$2 text=$3
  local as_user=${POST_AS_BOT:-1}
  local token response

  # DM channels (D-prefix) require user identity — bots can't write to DMs they didn't open.
  # Source ~/.profile to pick up SLACK_USER_TOKEN if not already in env.
  if [[ "$channel_id" == D* ]]; then
    if [[ -z "${SLACK_USER_TOKEN:-}" ]]; then
      # shellcheck source=/dev/null
      source "${HOME}/.profile" 2>/dev/null || true
    fi
    token="${SLACK_USER_TOKEN:-}"
  elif [[ "$as_user" == "0" ]]; then
    token="${SLACK_USER_TOKEN:-}"
  else
    # Prefer MCP mail bot token for dropped-thread nudges
    token="${MCP_MAIL_BOT_TOKEN:-${SLACK_BOT_TOKEN:-${SLACK_BOT_TOKEN:-}}}"
  fi

  response="$(curl --silent --show-error --fail \
    --connect-timeout 10 --max-time 30 \
    -X POST "https://slack.com/api/chat.postMessage" \
    -H "Authorization: Bearer $token" \
    -H "Content-Type: application/json" \
    -d "$(jq -n --arg ch "$channel_id" --arg ts "$thread_ts" --arg txt "$text" \
      '{channel: $ch, text: $txt, thread_ts: $ts}')")" || return 1

  echo "$response" | jq -e '.ok == true' > /dev/null 2>&1 || return 1
  return 0
}

# Source guard — after functions are defined so tests can source and call them
[[ "${IS_SOURCED:-0}" == "1" ]] && return 0

# ── Main ───────────────────────────────────────────────────────────────────────

log "Starting dropped-thread-followup (lookback: ${LOOKBACK_HOURS}h)"

[[ -z "$SLACK_TOKEN" ]] && { log "ERROR: SLACK_BOT_TOKEN (or SLACK_BOT_TOKEN) not set"; exit 1; }

actioned=0 skipped=0

for channel in $SCAN_CHANNELS; do
  log "Checking channel $channel..."

  threads=$(fetch_recent_threads "$channel" 2>/dev/null) || { log "  Failed to fetch threads for $channel"; continue; }

  while IFS= read -r thread_ts; do
    [[ -z "$thread_ts" ]] && continue

    # Idempotency guard
    if was_nudged_recently "$channel" "$thread_ts"; then
      ((skipped++)) || true
      log "  SKIP (nudged recently): $channel $thread_ts"
      continue
    fi

    # Fetch thread messages
    messages=$(fetch_thread_messages "$channel" "$thread_ts" 2>&1) || {
      log "  WARN: conversations.replies failed for $channel $thread_ts: $(echo "$messages" | head -1)"
      continue
    }

    # Count messages by type
    user_msg_count=$(echo "$messages" | jq --arg agent "$AGENT_USER_ID" '[.[] | select(.user != null and .user != $agent)] | length' 2>/dev/null || echo 0)
    agent_msg_count=$(echo "$messages" | jq --arg agent "$AGENT_USER_ID" '[.[] | select(.user == $agent)] | length' 2>/dev/null || echo 0)
    last_reply_ts=$(echo "$messages" | jq -r '.[-1].ts' 2>/dev/null || echo "")

    # Convert Slack ts to ISO
    last_reply_iso=""
    if [[ -n "$last_reply_ts" ]]; then
      last_reply_iso=$(date -r "${last_reply_ts%.*}" -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || echo "")
    fi

    # Analyze
    analysis=$(analyze_thread "$channel" "$thread_ts" "$user_msg_count" "$agent_msg_count" "$last_reply_iso" "$messages" "$AGENT_USER_ID")
    needs_action=$(echo "$analysis" | jq -r '.action_needed' 2>/dev/null || echo "false")

    if [[ "$needs_action" != "true" ]]; then
      reason=$(echo "$analysis" | jq -r '.reason' 2>/dev/null || echo "unknown")
      log "  OK ($reason): $channel $thread_ts"
      continue
    fi

    reason=$(echo "$analysis" | jq -r '.reason' 2>/dev/null || echo "unknown")
    kind=$(echo "$analysis" | jq -r '.kind // "cold-thread"' 2>/dev/null || echo "cold-thread")

    # Context for nudge: prefer Jeffrey's latest message, else latest non-agent (not thread root — may be noise)
    original_msg=$(echo "$messages" | jq -r --arg agent "$AGENT_USER_ID" --arg j "$JEFFREY_USER_ID" '
      [ .[] | select(.user != null and .user != $agent) ] as $all
      | ($all | map(select(.user == $j)) | sort_by(.ts | tonumber)) as $jl
      | if ($jl | length) > 0 then $jl[-1] else ($all | sort_by(.ts | tonumber) | last) end
      | .text // empty
    ' 2>/dev/null | tr '\n' ' ' | sed 's/[[:space:]]\+/ /g' | cut -c1-500)

    # Build nudge message
    if [[ "$kind" == "stale-dispatch" ]]; then
      nudge_text="[Dropped-thread followup] This dispatched AO task has been running with no progress update. "
      nudge_text+="Please post a concise status update in-thread now (current step, blocker if any, and next checkpoint). "
      nudge_text+="If work is complete, post proof links (PR/commit/artifact) instead."
    elif [[ "$kind" == "timeout-failure" ]]; then
      nudge_text="[Dropped-thread followup] This thread shows a gateway/model timeout or overload — that counts as a dropped run. "
      nudge_text+="Please retry with a smaller step, lower concurrency, or post the blocker. "
      nudge_text+="Original ask: \"${original_msg:-[could not retrieve]}\"."
    else
      nudge_text="[Dropped-thread followup] This thread appears to have gone cold. "
      nudge_text+="Original request: \"${original_msg:-[could not retrieve]}\". "
      nudge_text+="Please provide a status update on the requested action, or confirm if work is complete. "
      nudge_text+="If you admitted to not executing something, please do so now and either complete the work "
      nudge_text+="or explain the blocker."
    fi

    if [[ "${DRY_RUN:-0}" == "1" ]]; then
      log "DRY_RUN: would nudge $channel $thread_ts ($reason): $nudge_text"
      ((actioned++)) || true
      continue
    fi

    if post_reply "$channel" "$thread_ts" "$nudge_text"; then
      record_nudge "$channel" "$thread_ts"
      log "  NUDGED: $channel $thread_ts"
    else
      log "  ERROR: failed to nudge $channel $thread_ts"
      continue
    fi
    ((actioned++)) || true

  done <<< "$threads"
done

# ── Standalone message scan ────────────────────────────────────────────────────
# Catches Jeffrey messages with reply_count==0 that never got a bot reply.
# These are invisible to fetch_recent_threads.

log "Scanning for standalone unanswered messages..."

for channel in $SCAN_CHANNELS; do
  log "  Standalone scan: $channel"

  standalone_msgs=$(fetch_standalone_user_messages "$channel" 2>/dev/null) || {
    log "  WARN: standalone scan failed for $channel"
    continue
  }

  while IFS= read -r msg_ts; do
    [[ -z "$msg_ts" ]] && continue

    if was_nudged_recently "$channel" "$msg_ts"; then
      ((skipped++)) || true
      log "  SKIP standalone (nudged recently): $channel $msg_ts"
      continue
    fi

    nudge_text="[Dropped-thread followup] You sent a message in this channel that never received a reply. "
    nudge_text+="Please respond to Jeffrey's message (ts: ${msg_ts}) now."

    if [[ "${DRY_RUN:-0}" == "1" ]]; then
      log "DRY_RUN: would nudge standalone $channel $msg_ts"
      ((actioned++)) || true
      continue
    fi

    if post_reply "$channel" "$msg_ts" "$nudge_text"; then
      record_nudge "$channel" "$msg_ts"
      log "  NUDGED standalone: $channel $msg_ts"
    else
      log "  ERROR: failed to nudge standalone $channel $msg_ts"
      continue
    fi
    ((actioned++)) || true

  done <<< "$standalone_msgs"
done

log "Done — actioned=$actioned skipped=$skipped"
