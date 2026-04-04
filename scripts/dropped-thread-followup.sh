#!/usr/bin/env bash
# dropped-thread-followup.sh
# Audits Slack channels for dropped agent threads and takes action.
# Runs every 4 hours via launchd.
#
# DROP DETECTION — strict criteria (avoid false positives / spam):
#   1. Agent explicitly admitted: "I did not execute it yet", "I only sent an acknowledgment"
#   2. Agent acknowledged but no AO dispatch / PR / commit / action was taken
#   3. Thread is unresolved AND > 2h old AND user asked agent to do work
#
# Idempotency: tracks last-nudged ts per (channel, thread_ts) in a JSON state file.
# Only re-nudges if > DROP_NUDGE_INTERVAL_SECS (default: 30m) have passed.
#
# Guardrails:
#   - DRY_RUN=1: prints actions without executing
#   - IS_SOURCED=1: allows source for test coverage without running main
#   - Overlap lock prevents concurrent runs

set -euo pipefail

# Guard: ensure basic commands are always available even in restricted launchd PATH
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin${PATH:+:$PATH}"

# Suppress SIGPIPE — python3 heredoc can exit before echo finishes piping,
# producing "echo: write error: Broken pipe" under set -euo pipefail
trap '' PIPE

# ── Config ────────────────────────────────────────────────────────────────────
LOCK_DIR="${DROP_LOCK_DIR:-${TMPDIR:-/tmp}/openclaw-dropped-thread.lock}"
LOG_DIR="${DROP_LOG_DIR:-${HOME}/.smartclaw/logs}"
STATE_FILE="${DROP_STATE_FILE:-$HOME/.smartclaw/logs/dropped-thread-state.json}"
NUDGE_INTERVAL_SECS="${DROP_NUDGE_INTERVAL_SECS:-1800}"   # 30 minutes default
LOOKBACK_HOURS="${DROP_LOOKBACK_HOURS:-8}"               # scan last N hours
PROGRESS_STALE_MINUTES="${DROP_PROGRESS_STALE_MINUTES:-5}"  # dispatched task with no progress
POST_AS_BOT="${DROP_POST_AS_BOT:-1}"                      # 0 = post as user
AGENT_USER_ID="${OPENCLAW_BOT_USER_ID:-U0AEZC7RX1Q}"     # bot user ID for classification

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
  ts_sec="$(date -j -f "%Y-%m-%dT%H:%M:%SZ" "$last_ts" '+%s' 2>/dev/null)" || return 0
  [[ $((now_sec - ts_sec)) -lt NUDGE_INTERVAL_SECS ]] && return 0
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

  python3 - "$user_msgs" "$agent_msgs" "$hours_old" "$messages_json" "$agent_user_id" "$PROGRESS_STALE_MINUTES" <<'PYEOF'
import sys, json

user_msgs   = int(sys.argv[1]) if sys.argv[1] else 0
agent_msgs  = int(sys.argv[2]) if sys.argv[2] else 0
hours_old   = float(sys.argv[3]) if sys.argv[3] else 999
messages    = json.loads(sys.argv[4]) if sys.argv[4] else []
AGENT_ID    = sys.argv[5]
progress_stale_minutes = int(sys.argv[6]) if sys.argv[6] else 10
progress_stale_h = progress_stale_minutes / 60.0
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

# Result indicators — agent actually completed something
RESULT_PHRASES = [
    "pr #", "pull/", "commit ", "pushed", "posted result",
    "merged", "created a ", "file://", "http://", "https://",
    "task complete", "done", "finished", "completed",
]
PROGRESS_PHRASES = [
    "still working", "in progress", "currently working", "blocked",
    "investigating", "working on", "waiting on", "follow up", "follow-up",
]

admitted = any(phrase in agent_text for phrase in ADMISSION_PHRASES)
has_result = any(phrase in agent_text for phrase in RESULT_PHRASES)
has_progress_reply = any(phrase in agent_text for phrase in PROGRESS_PHRASES)
dispatched_ao = "spawning agent for" in agent_text or "session " in agent_text and " created" in agent_text

# Check if bot gave a substantive informational answer (not just an ack)
# Total chars across all bot messages — substantive = >200 chars (filters out short acks)
total_bot_chars = sum(len(m.get("text", "")) for m in messages if m.get("user") == AGENT_ID)
bot_gave_substantive_answer = (
    total_bot_chars > 200 and not has_result and not admitted
)

# No user messages → nothing to have dropped
if user_msgs == 0:
    print(json.dumps({"admitted": False, "action_needed": False, "reason": "no user asks", "kind": "none"}))
    sys.exit(0)

# User followed up after the last agent reply and has waited long enough.
# This catches "new ask in old thread" cases that were previously missed.
ACTION_VERBS = [
    "fix", "update", "check", "verify", "merge", "drive", "make sure", "follow up",
    "please", "retry", "status", "why", "can you", "do ", "run ", "ship", "review",
]
looks_actionable = any(v in last_user_text for v in ACTION_VERBS) or ("<@" in last_user_text)
if user_after_agent and minutes_since_last_user >= 5 and looks_actionable:
    print(json.dumps({
        "admitted": admitted,
        "action_needed": True,
        "reason": f"user follow-up pending ({minutes_since_last_user:.0f}m) after last agent reply",
        "kind": "followup-pending",
    }))
    sys.exit(0)

# Agent replied recently AND completed work → not a drop
if hours_old < 0.5 and has_result and not user_after_agent:
    print(json.dumps({"admitted": False, "action_needed": False, "reason": "recent agent reply with result", "kind": "none"}))
    sys.exit(0)

# Agent replied recently but didn't complete work AND admission present → nudge
if hours_old < 0.5 and admitted:
    print(json.dumps({"admitted": True, "action_needed": True,
                       "reason": f"recent reply with admission, {hours_old:.1f}h old", "kind": "admission"}))
    sys.exit(0)

# Long-running dispatched task with no progress update.
if dispatched_ao and hours_old >= progress_stale_h and not has_result and not has_progress_reply:
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
SLACK_TOKEN="${SLACK_BOT_TOKEN:-${MCP_MAIL_BOT_TOKEN:-}}"

resolve_channels() {
  local config="${OPENCLAW_CONFIG_FILE:-${HOME}/.smartclaw/openclaw.json}"
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

fetch_thread_messages() {
  local channel_id=$1 thread_ts=$2
  local response
  response="$(curl --silent --show-error \
    --connect-timeout 10 --max-time 30 \
    --get "https://slack.com/api/conversations.replies" \
    --data-urlencode "channel=${channel_id}" \
    --data-urlencode "ts=${thread_ts}" \
    --data-urlencode "limit=20" \
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

post_reply() {
  local channel_id=$1 thread_ts=$2 text=$3
  local as_user=${POST_AS_BOT:-1}
  local token response

  if [[ "$as_user" == "0" ]]; then
    token="${SLACK_USER_TOKEN:-}"
  else
    # Prefer MCP mail bot token for dropped-thread nudges (not OpenClaw bot)
    token="${MCP_MAIL_BOT_TOKEN:-${SLACK_BOT_TOKEN:-}}"
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

[[ -z "$SLACK_TOKEN" ]] && { log "ERROR: SLACK_BOT_TOKEN not set"; exit 1; }

actioned=0 skipped=0

for channel in $DEFAULT_CHANNELS; do
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

    # Build nudge message
    if [[ "$kind" == "stale-dispatch" ]]; then
      nudge_text="[Dropped-thread followup] This dispatched AO task has been running with no progress update. "
      nudge_text+="Please post a concise status update in-thread now (current step, blocker if any, and next checkpoint). "
      nudge_text+="If work is complete, post proof links (PR/commit/artifact) instead."
    else
      nudge_text="[Dropped-thread followup] This thread appears to have gone cold. "
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

log "Done — actioned=$actioned skipped=$skipped"
