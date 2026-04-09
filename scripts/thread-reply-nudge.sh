#!/usr/bin/env bash
set -euo pipefail

LOCK_DIR="${NUDGE_LOCK_DIR:-${TMPDIR:-/tmp}/openclaw-thread-reply-nudge.lock}"
LOG_DIR="${NUDGE_LOG_DIR:-/tmp/openclaw}"
mkdir -p "$LOG_DIR"

# ── Channel resolution ────────────────────────────────────────────────────────
# resolve_nudge_channels: returns a space-separated list of Slack channel IDs
# Priority:
#   1. THREAD_REPLY_CHANNEL env var (comma- or space-separated list)
#   2. All explicit (non-wildcard) channels from openclaw.json
#   3. OPENCLAW_MONITOR_THREAD_REPLY_CHANNEL env var (legacy single-channel)
#   4. Hardcoded fallback C0AKYEY48GM
resolve_nudge_channels() {
  # 1. Explicit env var override
  if [[ -n "${THREAD_REPLY_CHANNEL:-}" ]]; then
    echo "${THREAD_REPLY_CHANNEL//,/ }"
    return 0
  fi

  # 2. Parse openclaw.json
  local config="${OPENCLAW_CONFIG_FILE:-${HOME}/.openclaw/openclaw.json}"
  if [[ -f "$config" ]] && command -v python3 >/dev/null 2>&1; then
    local channels
    channels="$(python3 - "$config" <<'PYEOF' 2>/dev/null
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
)"
    if [[ -n "$channels" ]]; then
      echo "$channels"
      return 0
    fi
  fi

  # 3. Legacy single-channel env var
  if [[ -n "${OPENCLAW_MONITOR_THREAD_REPLY_CHANNEL:-}" ]]; then
    echo "$OPENCLAW_MONITOR_THREAD_REPLY_CHANNEL"
    return 0
  fi

  # 4. Hardcoded fallback
  echo "C0AKYEY48GM"
}

# ── Guard: sourcing for tests stops here ──────────────────────────────────────
[[ "${IS_SOURCED:-0}" == "1" ]] && return 0

# ── Main body ─────────────────────────────────────────────────────────────────

# Prevent overlap if launchd fires while a prior run is still in-flight.
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  exit 0
fi
trap 'rmdir "$LOCK_DIR" >/dev/null 2>&1 || true' EXIT

LAST_RUN_FILE="$LOG_DIR/thread-reply-nudge.last"
NOW_EPOCH="$(date +%s)"
if [[ -f "$LAST_RUN_FILE" ]]; then
  LAST_RUN="$(cat "$LAST_RUN_FILE" 2>/dev/null || echo 0)"
  if [[ $((NOW_EPOCH - LAST_RUN)) -lt 90 ]]; then
    exit 0
  fi
fi
printf '%s' "$NOW_EPOCH" >"$LAST_RUN_FILE"

# Build the channel list and prompt
CHANNELS="$(resolve_nudge_channels)"
PROMPT="check channels ${CHANNELS} for (1) unanswered human messages older than 90 seconds and (2) dispatched AO tasks older than 5 minutes without any progress update since dispatch; reply in-thread only when needed; do not post if already answered"

# DRY_RUN=1: print prompt and exit (used by tests)
if [[ "${DRY_RUN:-0}" == "1" ]]; then
  echo "DRY_RUN prompt: $PROMPT"
  exit 0
fi

# Fire-and-return quickly; run the wake ping in background so this script stays fast.
if command -v openclaw >/dev/null 2>&1; then
  nohup openclaw agent --agent main -m "$PROMPT" >>"$LOG_DIR/thread-reply-nudge.log" 2>&1 &
fi
