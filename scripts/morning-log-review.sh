#!/usr/bin/env bash
#
# Morning Log Review — 8:00 AM PT Mon–Fri
# Parses last night's gateway + agent logs, extracts errors, and posts
# an actionable fixes summary to Slack.
#
# Do NOT post as reminder relay text — post real findings only.
# If no errors found, post a brief "all clear" confirmation.

set -euo pipefail

ROOT="${OPENCLAW_ROOT:-$HOME/.openclaw}"
LOG_DIR="$ROOT/logs"
OUT_DIR="$ROOT/logs/morning-log-review"
REPORT="$OUT_DIR/report-$(date +%Y%m%d).txt"

mkdir -p "$OUT_DIR"

# ── helpers ──────────────────────────────────────────────────────────────────

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "[$(ts)] $*"; }

# ── collect log files ─────────────────────────────────────────────────────────

GW_LOG="$LOG_DIR/gateway.log"
GW_ERR="$LOG_DIR/gateway.err.log"
HC_LOG="$LOG_DIR/health-check.log"
AGENT_LOG="$LOG_DIR/monitor-agent.log"

collect_logs() {
  local out="$1"
  : > "$out"

  for logfile in "$GW_LOG" "$GW_ERR" "$HC_LOG" "$AGENT_LOG"; do
    if [[ -f "$logfile" ]]; then
      echo "===== $(basename "$logfile") =====" >> "$out"
      # Extract all error/warning lines from the log file
      grep -E '(ERROR|FATAL|WARN|CRITICAL|failed|exception)' "$logfile" 2>/dev/null >> "$out" || true
      echo "" >> "$out"
    fi
  done
}

# ── build report ──────────────────────────────────────────────────────────────

collect_logs "$OUT_DIR/raw-$(date +%Y%m%d).log"

ERROR_LINES=$(grep -cE '(ERROR|FATAL|CRITICAL)' "$OUT_DIR/raw-$(date +%Y%m%d).log" 2>/dev/null || echo 0)
# Warn bucket: exclude error-level lines to avoid double-counting
WARN_LINES=$(grep -vE '(ERROR|FATAL|CRITICAL)' "$OUT_DIR/raw-$(date +%Y%m%d).log" 2>/dev/null | grep -cE '(WARN|failed|exception)' || echo 0)
TOTAL_ERRORS=$((ERROR_LINES + WARN_LINES))

{
  echo "Morning Log Review — $(date '+%Y-%m-%d')"
  echo "========================================="
  echo "Gateway log: $GW_LOG"
  echo "Errors found: $ERROR_LINES | Warnings: $WARN_LINES"
  echo ""
  echo "=== Errors ==="
  grep -E '(ERROR|FATAL|CRITICAL)' "$OUT_DIR/raw-$(date +%Y%m%d).log" 2>/dev/null | head -30 || echo "(none)"
  echo ""
  echo "=== Warnings / Failed Operations ==="
  grep -E '(WARN|failed|exception)' "$OUT_DIR/raw-$(date +%Y%m%d).log" 2>/dev/null | grep -vE '(ERROR|FATAL|CRITICAL)' | head -30 || echo "(none)"
  echo ""
  echo "=== Actionable Items ==="
  # Heuristic: errors that mention specific files or modules
  grep -E '(ERROR|FATAL|CRITICAL)' "$OUT_DIR/raw-$(date +%Y%m%d).log" 2>/dev/null \
    | grep -oE '(tools?|gateway|agent|launchd|plutil|script|orchestration|health)' \
    | sort | uniq -c | sort -rn | head -10 \
    | awk '{print "- ["$1" occurrences] "$2" — review related module"}' || echo "(none)"
} > "$REPORT"

# ── Slack notification ───────────────────────────────────────────────────────

post_slack() {
  local msg="$1"

  if command -v openclaw >/dev/null 2>&1 && [[ -n "${OPENCLAW_ALERT_SLACK_TARGET:-}" ]]; then
    openclaw message send \
      --channel slack \
      --target "$OPENCLAW_ALERT_SLACK_TARGET" \
      --message "$msg" 2>/dev/null && return 0
  fi

  # Fallback: direct curl as jleechan (triggers OpenClaw gateway)
  if [[ -f "$HOME/.profile" ]]; then source "$HOME/.profile" 2>/dev/null || true; fi
  if [[ -z "${SLACK_USER_TOKEN:-}" ]]; then
    log "SLACK_USER_TOKEN not set — skipping Slack notification"
    return 0
  fi

  local channel_id
  channel_id="${SLACK_REVIEW_CHANNEL_ID:-C0AJQ5M0A0Y}"  # default #openclaw

  curl -s -X POST "https://slack.com/api/chat.postMessage" \
    -H "Authorization: Bearer $SLACK_USER_TOKEN" \
    -H "Content-Type: application/json" \
    -d "$(python3 -c "import json,sys; print(json.dumps({'channel': '$channel_id', 'text': sys.stdin.read().strip()}))" <<< "$msg")" \
    >> "$OUT_DIR/slack-$(date +%Y%m%d).log" 2>&1 || true
}

if [[ "$TOTAL_ERRORS" -eq 0 ]]; then
  SUMMARY="Morning Log Review ✅ — No errors in last night's gateway/agent logs."
  post_slack "$SUMMARY"
  log "All clear. Posted: $SUMMARY"
else
  SUMMARY="Morning Log Review ⚠️ — $ERROR_LINES error(s), $WARN_LINES warning(s). See $REPORT"
  # Post just the top 5 actionable items to Slack (don't dump full log)
  TOP_ITEMS=$(grep -E '(ERROR|FATAL|CRITICAL)' "$OUT_DIR/raw-$(date +%Y%m%d).log" 2>/dev/null | head -5)
  SLACK_MSG="Morning Log Review ⚠️ — $ERROR_LINES error(s), $WARN_LINES warning(s) in last night's logs.

*Top errors:*
$(echo "$TOP_ITEMS" | sed 's/.*ERROR.*/**ERROR**/; s/.*FATAL.*/**FATAL**/; s/.*CRITICAL.*/**CRITICAL**/')

Full report: $REPORT"

  post_slack "$SLACK_MSG"
  log "Errors found. Posted summary. Report: $REPORT"
fi

log "Done. Total issues: $TOTAL_ERRORS"
exit 0
