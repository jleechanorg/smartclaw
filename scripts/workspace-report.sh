#!/bin/bash
#
# Workspace Observability Report
# Weekly job that audits smartclaw workspace health across all dimensions:
# worktrees, workspace parity, backups, launchd services, sessions, and evidence.
#
# Output: structured report written to:
#   ${OPENCLAW_WORKSPACE_REPORT_OUTDIR:-$HOME/.smartclaw/logs/workspace-report}/YYYY-WW.md
#
# Slack notification (optional):
#   Set OPENCLAW_WORKSPACE_REPORT_CHANNEL to a channel ID to post exec summary.
#
# Rerunnable: writes dated snapshots; never overwrites the latest report.
# Versioned: script is in git; config is in ~/.smartclaw/config/workspace-report.json.
#
set -euo pipefail

REPO_DIR="${OPENCLAW_WORKSPACE_REPORT_REPO_DIR:-$HOME/.smartclaw}"
WORK_DIR="${OPENCLAW_WORKSPACE_REPORT_WORK_DIR:-/tmp/workspace-report-$$}"
OUT_DIR="${OPENCLAW_WORKSPACE_REPORT_OUTDIR:-$HOME/.smartclaw/logs/workspace-report}"
SLACK_CHANNEL="${OPENCLAW_WORKSPACE_REPORT_CHANNEL:-}"
GITHUB_TOKEN_SOURCE="${GITHUB_TOKEN:-$HOME/.github_token}"
TZ="${TZ:-America/Los_Angeles}"; export TZ
NOW="$(date '+%Y-%m-%dT%H:%M:%S%z')"
WEEK="$(date '+%G-W%V')"
REPORT_FILE=""

REPORT_FAIL_COUNT=0
REPORT_WARN_COUNT=0
REPORT_PASS_COUNT=0
ISSUES=()
WORKTREES=()

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1"
  [[ -d "$WORK_DIR" ]] && echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$WORK_DIR/report.log"
}
warn_log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] WARN: $1"
  [[ -d "$WORK_DIR" ]] && echo "[$(date '+%Y-%m-%d %H:%M:%S')] WARN: $1" >> "$WORK_DIR/report.log"
}
error_log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: $1" >&2
  [[ -d "$WORK_DIR" ]] && echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: $1" >> "$WORK_DIR/report.log"
}

section() {
  echo "" >> "$SECTIONS_FILE"
  echo "---" >> "$SECTIONS_FILE"
  echo "" >> "$SECTIONS_FILE"
  echo "## $1" >> "$SECTIONS_FILE"
  echo "" >> "$SECTIONS_FILE"
}

# Run a command with a timeout. Uses GNU timeout if available;
# on macOS falls back to gtimeout (brew install coreutils) or runs directly.
run_with_timeout() {
  local secs="$1"; shift
  if command -v timeout >/dev/null 2>&1; then
    timeout "$secs" "$@"
  elif command -v gtimeout >/dev/null 2>&1; then
    gtimeout "$secs" "$@"
  else
    # No timeout utility — run directly (may hang)
    "$@"
  fi
}

count_issue() { REPORT_FAIL_COUNT=$((REPORT_FAIL_COUNT + 1)); }
count_warn() { REPORT_WARN_COUNT=$((REPORT_WARN_COUNT + 1)); }
count_pass() { REPORT_PASS_COUNT=$((REPORT_PASS_COUNT + 1)); }

resolve_gh_token() {
  if [[ -n "${GH_TOKEN:-}" ]]; then printf '%s' "$GH_TOKEN"; return 0; fi
  if [[ -n "${GITHUB_TOKEN:-}" ]]; then
    if [[ ! -f "$GITHUB_TOKEN" ]]; then printf '%s' "$GITHUB_TOKEN"; return 0; fi
    if [[ -f "$GITHUB_TOKEN" ]] && [[ -r "$GITHUB_TOKEN" ]]; then
      tr -d '\r\n' < "$GITHUB_TOKEN"; return 0
    fi
  fi
  if [[ -f "$GITHUB_TOKEN_SOURCE" ]] && [[ -r "$GITHUB_TOKEN_SOURCE" ]]; then
    tr -d '\r\n' < "$GITHUB_TOKEN_SOURCE"; return 0
  fi
  return 1
}

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
mkdir -p "$OUT_DIR" "$WORK_DIR"
RUN_ID="$(date '+%Y-%m-%d_%H%M%S')"  # unique per invocation
REPORT_FILE="$OUT_DIR/workspace-report-$WEEK-$RUN_ID.md"
SECTIONS_FILE="$WORK_DIR/sections.md"
log "Starting workspace observability report — week $WEEK"
log "Output: $REPORT_FILE"

# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------
{
  echo "# Workspace Observability Report"
  echo ""
  echo "_Generated: $NOW ($TZ)_"
  echo ""
} > "$REPORT_FILE"

# ---------------------------------------------------------------------------
# 1. Worktree Inventory
# ---------------------------------------------------------------------------
section "1. Worktree Inventory"

# Fetch PR list once (used for both worktree PR lookup and section 7)
WORKTREE_PR_LIST=""
if GH_TOKEN_VALUE="$(resolve_gh_token)"; then
  export GH_TOKEN="$GH_TOKEN_VALUE"
  WORKTREE_PR_LIST=$(run_with_timeout 10 gh api repos/jleechanorg/smartclaw/pulls 2>/dev/null || echo "")
fi

if [[ ! -d "$HOME/.worktrees" ]]; then
  echo "No worktrees directory found at $HOME/.worktrees" >> "$SECTIONS_FILE"
  count_warn; ISSUES+=("No worktrees directory found")
else
  # Table header (suppress if no worktrees)
  echo "| Name | Branch | Revision | Ahead | Behind | Status | Last Commit | PR |" >> "$SECTIONS_FILE"
  echo "|---|---|---|---|---|---|---|---|" >> "$SECTIONS_FILE"

  for wt in "$HOME/.worktrees"/*; do
    [[ -f "$wt/.git" ]] || continue  # skip non-git directories
    wt_name="$(basename "$wt")"
    wt_branch="$(git -C "$wt" branch --show-current 2>/dev/null || echo 'detached')"
    wt_rev="$(git -C "$wt" rev-parse --short HEAD 2>/dev/null || echo '?')"
    wt_main_diff="$(git -C "$wt" rev-list --count HEAD ^main 2>/dev/null || echo '?')"
    wt_main_ahead="$(git -C "$wt" rev-list --count main ^HEAD 2>/dev/null || echo '?')"
    wt_status="$(git -C "$wt" status --porcelain 2>/dev/null | wc -l | tr -d ' ')"
    wt_last_commit="$(git -C "$wt" log -1 --format='%ci %s' 2>/dev/null || echo 'no commits')"

    # Look up PR URL from cached PR list
    pr_url=""
    if [[ -n "$WORKTREE_PR_LIST" ]]; then
      pr_url=$(printf '%s' "$WORKTREE_PR_LIST" | jq -r ".[] | select(.head.ref == \"$wt_branch\") | .url" 2>/dev/null | head -1 || echo "")
    fi

    echo "| \`$wt_name\` | \`$wt_branch\` | \`$wt_rev\` | $wt_main_diff ahead main | $wt_main_ahead behind main | $wt_status dirty | $wt_last_commit | ${pr_url:-no PR} |" >> "$SECTIONS_FILE"

    WORKTREES+=("$wt_name:$wt_branch:$wt_status")

    if [[ "$wt_status" -gt 0 ]]; then
      count_warn; ISSUES+=("Worktree \`$wt_name\` has $wt_status uncommitted file(s)")
    fi
    if [[ "$wt_main_ahead" != "?" && "$wt_main_diff" != "?" ]]; then
      if [[ "$wt_main_ahead" == "0" && "$wt_main_diff" == "0" ]]; then
        count_pass
      elif [[ "$wt_main_diff" -gt 10 ]]; then
        count_warn; ISSUES+=("Worktree \`$wt_name\` is $wt_main_diff commits ahead main — consider merging or closing")
      fi
    fi
  done
fi

# ---------------------------------------------------------------------------
# 2. Workspace File Parity
# ---------------------------------------------------------------------------
section "2. Workspace File Parity"

PARITY_FILES=(AGENTS.md SOUL.md TOOLS.md USER.md IDENTITY.md HEARTBEAT.md)
PARITY_OK=1
for file in "${PARITY_FILES[@]}"; do
  repo_file="$REPO_DIR/$file"
  workspace_file="$HOME/.smartclaw/workspace/$file"
  if [[ ! -f "$repo_file" ]]; then
    echo "- \`$file\`: **MISSING in repo**" >> "$SECTIONS_FILE"
    count_issue; ISSUES+=("Policy file $file missing in repo")
    PARITY_OK=0
    continue
  fi
  if [[ ! -f "$workspace_file" ]]; then
    echo "- \`$file\`: **MISSING in ~/.smartclaw/workspace/**" >> "$SECTIONS_FILE"
    count_issue; ISSUES+=("Policy file $file missing in workspace")
    PARITY_OK=0
    continue
  fi
  if ! cmp -s "$repo_file" "$workspace_file"; then
    echo "- \`$file\`: **DIRTY — repo and workspace differ**" >> "$SECTIONS_FILE"
    count_issue; ISSUES+=("Policy file $file differs between repo and workspace")
    PARITY_OK=0
  else
    echo "- \`$file\`: OK (matches workspace)" >> "$SECTIONS_FILE"
    count_pass
  fi
done

if [[ "$PARITY_OK" -eq 1 ]]; then
  echo "" >> "$SECTIONS_FILE"
  echo "All policy files in sync between repo and live workspace." >> "$SECTIONS_FILE"
fi

# ---------------------------------------------------------------------------
# 3. Backup Snapshots
# ---------------------------------------------------------------------------
section "3. Backup Snapshots"

BACKUP_BASE="$REPO_DIR/.smartclaw-backups"
if [[ ! -d "$BACKUP_BASE" ]]; then
  echo "No backup directory found at $BACKUP_BASE" >> "$SECTIONS_FILE"
  count_warn; ISSUES+=("No .smartclaw-backups/ directory found in repo")
else
  shopt -s nullglob 2>/dev/null || true
  SNAP_COUNT="$(ls -1d "$BACKUP_BASE"/[0-9]* 2>/dev/null | wc -l | tr -d ' ')"
  echo "Total snapshots: **$SNAP_COUNT**" >> "$SECTIONS_FILE"
  echo "" >> "$SECTIONS_FILE"
  echo "| Snapshot | Timestamp | Age |" >> "$SECTIONS_FILE"
  echo "|---|---|---|" >> "$SECTIONS_FILE"

  # Show last 10 snapshots
  for snap in $(ls -dt "$BACKUP_BASE"/[0-9]* 2>/dev/null | head -10); do
    [[ -d "$snap" ]] || continue
    snap_name="$(basename "$snap")"
    snap_ts=$(stat -f '%Sm' -t '%Y-%m-%d %H:%M' "$snap" 2>/dev/null || echo "unknown")
    snap_epoch=$(stat -f '%m' "$snap" 2>/dev/null || stat -c '%Y' "$snap" 2>/dev/null || date +%s)
    snap_age_days=$(( ($(date +%s) - snap_epoch) / 86400 ))
    echo "| \`$snap_name\` | $snap_ts | ${snap_age_days}d ago |" >> "$SECTIONS_FILE"
  done

  shopt -u nullglob 2>/dev/null || true

  if [[ "$SNAP_COUNT" -eq 0 ]]; then
    count_warn; ISSUES+=("No backup snapshots found in .smartclaw-backups/")
  elif [[ "$SNAP_COUNT" -lt 3 ]]; then
    count_warn; ISSUES+=("Only $SNAP_COUNT backup snapshot(s) found — may indicate backup job issues")
  else
    count_pass
  fi
fi

# Also check workspace backup consolidation
WORKSPACE_BACKUP="$HOME/.smartclaw/workspace/openclaw/.smartclaw-backups"
if [[ -d "$WORKSPACE_BACKUP" ]]; then
  WS_SNAP_COUNT="$(ls -1d "$WORKSPACE_BACKUP"/[0-9]* 2>/dev/null | wc -l | tr -d ' ')"
  if [[ "$WS_SNAP_COUNT" -gt 0 ]]; then
    echo "" >> "$SECTIONS_FILE"
    echo "**Note:** Workspace backup dir ($WORKSPACE_BACKUP) has $WS_SNAP_COUNT snapshot(s) not yet consolidated. Run \`scripts/consolidate-workspace-snapshots.sh\` to migrate." >> "$SECTIONS_FILE"
    count_warn; ISSUES+=("$WS_SNAP_COUNT snapshot(s) in workspace backup dir not yet consolidated")
  fi
fi

# ---------------------------------------------------------------------------
# 4. Launchd Job Health
# ---------------------------------------------------------------------------
section "4. Launchd Job Health"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "launchd checks skipped (non-macOS)" >> "$SECTIONS_FILE"
else
  echo "| Job | Registered | State |" >> "$SECTIONS_FILE"
  echo "|---|---|---|" >> "$SECTIONS_FILE"

  LAUNCHD_LABELS=(
    "ai.smartclaw.schedule.morning-log-review"
    "ai.smartclaw.schedule.docs-drift-review"
    "ai.smartclaw.schedule.cron-backup-sync"
    "ai.smartclaw.schedule.weekly-error-trends"
    "ai.smartclaw.schedule.daily-research"
    "ai.smartclaw.schedule.harness-analyzer-9am"
    "ai.smartclaw.schedule.orch-health-weekly"
    "ai.smartclaw.schedule.bug-hunt-9am"
    "ai.smartclaw.schedule.workspace-report-weekly"
  )
  LAUNCHD_RUNNING_LABELS=(
    "com.smartclaw.gateway"
    "ai.agento.dashboard"
  )

  # Check continuous-running jobs (gateway, dashboard)
  for label in "${LAUNCHD_RUNNING_LABELS[@]}"; do
    if launchctl print "gui/$(id -u)/$label" >"$WORK_DIR/launchctl-$label.txt" 2>&1; then
      state=$(grep 'state = ' "$WORK_DIR/launchctl-$label.txt" | head -1 | sed 's/.*state = //; s/;//')
      echo "| \`$label\` | :white_check_mark: | \`$state\` |" >> "$SECTIONS_FILE"
      if [[ "$state" == "running" ]]; then
        count_pass
      else
        count_warn; ISSUES+=("Launchd job \`$label\` is \`$state\` (should be running)")
      fi
    else
      echo "| \`$label\` | :x: missing | — |" >> "$SECTIONS_FILE"
      count_issue; ISSUES+=("Launchd job \`$label\` not registered")
    fi
  done

  # Check scheduled jobs (registered-only; idle between scheduled times is normal)
  for label in "${LAUNCHD_LABELS[@]}"; do
    if launchctl print "gui/$(id -u)/$label" >"$WORK_DIR/launchctl-$label.txt" 2>&1; then
      state=$(grep 'state = ' "$WORK_DIR/launchctl-$label.txt" | head -1 | sed 's/.*state = //; s/;//')
      echo "| \`$label\` | :white_check_mark: | registered (\`$state\`) |" >> "$SECTIONS_FILE"
      count_pass
    else
      echo "| \`$label\` | :x: missing | — |" >> "$SECTIONS_FILE"
      count_issue; ISSUES+=("Scheduled launchd job \`$label\` not registered")
    fi
  done
fi

# ---------------------------------------------------------------------------
# 5. Session State
# ---------------------------------------------------------------------------
section "5. Active Sessions"

if command -v ao >/dev/null 2>&1; then
  # Capture output and return code; || true guards set -e since ao may fail
  ao_sessions_output=$(ao list sessions 2>&1) && ao_rc=0 || ao_rc=1
  echo '```' >> "$SECTIONS_FILE"
  echo "$ao_sessions_output" >> "$SECTIONS_FILE"
  echo '```' >> "$SECTIONS_FILE"

  if [[ $ao_rc -ne 0 ]]; then
    count_warn; ISSUES+=("ao list sessions failed (rc=$ao_rc)")
  else
    active_sessions=$(printf '%s' "$ao_sessions_output" | grep -c 'running\|pending' || true)
    if [[ "$active_sessions" -gt 0 ]]; then
      count_warn; ISSUES+=("$active_sessions active session(s) in AO")
    else
      count_pass
    fi
  fi
else
  echo "\`ao\` CLI not available — skipping session check" >> "$SECTIONS_FILE"
fi

# Check for stale tmux sessions
STALE_TMUX=0
if command -v tmux >/dev/null 2>&1; then
  for sess in $(tmux list-sessions -F '#{session_name}' 2>/dev/null || true); do
    last_act=$(tmux display-message -t "$sess" -p '#{session_activity}' 2>/dev/null || echo 0)
    now_ts=$(date +%s)
    # If no activity in >24h, flag as stale
    if [[ "$last_act" -lt $((now_ts - 86400)) ]]; then
      echo "- Session \`$sess\` has no activity for >24h — consider closing" >> "$SECTIONS_FILE"
      count_warn; ISSUES+=("Stale tmux session: $sess (>24h inactive)")
      STALE_TMUX=$((STALE_TMUX + 1))
    fi
  done
  if [[ "$STALE_TMUX" -eq 0 ]]; then
    count_pass
  fi
fi

# ---------------------------------------------------------------------------
# 6. Gateway & Health Probe
# ---------------------------------------------------------------------------
section "6. Gateway & Health Probe"

GATEWAY_PORT="${OPENCLAW_GATEWAY_PORT:-18789}"
health_body="$WORK_DIR/health-probe.json"
health_code=$(curl -sS --max-time 5 -o "$health_body" -w '%{http_code}' "http://127.0.0.1:${GATEWAY_PORT}/health" 2>/dev/null || echo "000")

if [[ "$health_code" == "200" ]]; then
  echo ":white_check_mark: Gateway HTTP /health returned \`200\` on port $GATEWAY_PORT" >> "$SECTIONS_FILE"
  count_pass
else
  echo ":x: Gateway HTTP /health returned \`$health_code\` on port $GATEWAY_PORT" >> "$SECTIONS_FILE"
  count_issue; ISSUES+=("Gateway health probe failed (HTTP $health_code)")
fi

gateway_status=$(OPENCLAW_GATEWAY_TOKEN="${OPENCLAW_GATEWAY_TOKEN:-}" openclaw gateway status 2>&1 || true)
if echo "$gateway_status" | grep -qE 'Runtime: running|Slack: ok|^Agents:'; then
  echo ":white_check_mark: \`openclaw gateway status\` reports running" >> "$SECTIONS_FILE"
  count_pass
else
  echo ":x: \`openclaw gateway status\` does not confirm runtime running" >> "$SECTIONS_FILE"
  count_warn; ISSUES+=("Gateway status does not confirm runtime running")
fi

# ---------------------------------------------------------------------------
# 7. Open PR Health
# ---------------------------------------------------------------------------
section "7. Open Pull Requests"

if ! GH_TOKEN_VALUE="$(resolve_gh_token)"; then
  echo "GitHub token not available — skipping PR check" >> "$SECTIONS_FILE"
else
  export GH_TOKEN="$GH_TOKEN_VALUE"

  PR_LIST=$(run_with_timeout 15 gh api repos/jleechanorg/smartclaw/pulls 2>/dev/null || echo "[]")
  PR_COUNT=$(echo "$PR_LIST" | jq length 2>/dev/null || echo 0)
  echo "Open PRs: **$PR_COUNT**" >> "$SECTIONS_FILE"
  echo "" >> "$SECTIONS_FILE"

  if [[ "$PR_COUNT" -gt 0 ]]; then
    echo "| # | Title | Branch |" >> "$SECTIONS_FILE"
    echo "|---|---|---|" >> "$SECTIONS_FILE"
    # Escape pipes in titles to avoid markdown table conflicts, then build table rows
    echo "$PR_LIST" | jq -r '.[] | "\(.number)\t\(.title | gsub("\\|"; "\\\\\\|"))\t\(.head.ref)"' | while IFS=$'\t' read -r num title branch; do
      echo "| $num | $title | $branch |"
    done >> "$SECTIONS_FILE"
  fi

  # Check for PRs with CI failures
  FAILED_CI=""
  for num in $(echo "$PR_LIST" | jq -r '.[].number' 2>/dev/null); do
    pr_sha=$(echo "$PR_LIST" | jq -r ".[] | select(.number == $num) | .head.sha" 2>/dev/null || echo "")
    if [[ -z "$pr_sha" ]]; then continue; fi
    status=$(run_with_timeout 10 gh api "repos/jleechanorg/smartclaw/commits/$pr_sha/status" 2>/dev/null | jq -r ".state" || echo "unknown")
    if [[ "$status" == "failure" ]]; then
      FAILED_CI="${FAILED_CI}${num} "
    fi
  done
  FAILED_CI="${FAILED_CI% }"

  if [[ -n "$FAILED_CI" && "$FAILED_CI" != " " ]]; then
    count_issue; ISSUES+=("PR(s) with CI failures: $FAILED_CI")
  else
    count_pass
  fi
fi

# ---------------------------------------------------------------------------
# 8. Evidence Review Metrics
# ---------------------------------------------------------------------------
section "8. Evidence Review Metrics"

EVIDENCE_DIR="$REPO_DIR/docs/evidence"
if [[ ! -d "$EVIDENCE_DIR" ]]; then
  echo "No \`docs/evidence/\` directory found — no evidence bundles recorded" >> "$SECTIONS_FILE"
  count_warn; ISSUES+=("No evidence bundles in docs/evidence/")
else
  BUNDLE_COUNT="$(find "$EVIDENCE_DIR" -name 'verdict.json' 2>/dev/null | wc -l | tr -d ' ')"
  RECENT_BUNDLES="$(find "$EVIDENCE_DIR" -name 'verdict.json' -mtime -14 2>/dev/null | wc -l | tr -d ' ')"

  echo "Total evidence bundles: **$BUNDLE_COUNT**" >> "$SECTIONS_FILE"
  echo "Bundles in last 14 days: **$RECENT_BUNDLES**" >> "$SECTIONS_FILE"
  echo "" >> "$SECTIONS_FILE"

  if [[ "$RECENT_BUNDLES" -gt 0 ]]; then
    echo "| Bundle | Verdict | Date |" >> "$SECTIONS_FILE"
    echo "|---|---|---|" >> "$SECTIONS_FILE"
    find "$EVIDENCE_DIR" -name 'verdict.json' -mtime -14 | sort | while read -r v; do
      bundle_dir="$(dirname "$v")"
      bundle_name="$(basename "$bundle_dir")"
      verdict=$(jq -r '.verdict // "unknown"' "$v" 2>/dev/null || echo "unknown")
      bundle_ts=$(stat -f '%Sm' -t '%Y-%m-%d %H:%M' "$v" 2>/dev/null || echo "unknown")
      bundle_date="${bundle_ts:-unknown}"
      echo "| \`$bundle_name\` | \`$verdict\` | $bundle_date |" >> "$SECTIONS_FILE"
    done
    count_pass
  else
    count_warn; ISSUES+=("No evidence bundles in last 14 days")
  fi
fi

# ---------------------------------------------------------------------------
# Compute health score (used for both Slack and report)
# ---------------------------------------------------------------------------
TOTAL_CHECKS=$(( REPORT_PASS_COUNT + REPORT_WARN_COUNT + REPORT_FAIL_COUNT ))
if [[ "$TOTAL_CHECKS" -gt 0 ]]; then
  HEALTH_SCORE=$(( REPORT_PASS_COUNT * 100 / TOTAL_CHECKS ))
else
  HEALTH_SCORE=100
fi
HEALTH_LABEL="healthy"
HEALTH_EMOJI=":white_check_mark:"
if [[ "$REPORT_FAIL_COUNT" -gt 3 ]]; then
  HEALTH_LABEL="degraded"; HEALTH_EMOJI=":warning:"
elif [[ "$REPORT_FAIL_COUNT" -gt 0 ]]; then
  HEALTH_LABEL="needs attention"; HEALTH_EMOJI=":warning:"
elif [[ "$REPORT_WARN_COUNT" -gt 5 ]]; then
  HEALTH_LABEL="needs attention"; HEALTH_EMOJI=":warning:"
fi

# ---------------------------------------------------------------------------
# Slack notification (optional)
# ---------------------------------------------------------------------------
if [[ -n "$SLACK_CHANNEL" ]]; then
  log "Posting exec summary to Slack channel $SLACK_CHANNEL"
  SLACK_MSG_JSON=""
  SLACK_MSG_BODY=""
  if [[ ${#ISSUES[@]} -gt 0 ]]; then
    SLACK_MSG_BODY="\n\n*Top Issues:*"
    while IFS= read -r issue; do
      SLACK_MSG_BODY="$SLACK_MSG_BODY\n• $issue"
    done < <(printf '%s\n' "${ISSUES[@]}" | head -3)
  fi
  SLACK_MSG_BODY="$SLACK_MSG_BODY\n\n_Full report: ${REPORT_FILE}_"
  SLACK_TEXT="*Workspace Observability Report — Week $WEEK* ${HEALTH_EMOJI} $HEALTH_LABEL ($HEALTH_SCORE%)$SLACK_MSG_BODY"
  SLACK_MSG_JSON="$(printf '%s' "$SLACK_TEXT" | jq -Rs .)"
  # shellcheck disable=SC2086
  curl -sS -X POST "https://slack.com/api/chat.postMessage" \
    -H "Authorization: Bearer ${SLACK_BOT_TOKEN:-}" \
    -H "Content-Type: application/json" \
    -d "{\"channel\": \"$SLACK_CHANNEL\", \"text\": $SLACK_MSG_JSON, \"unfurl_links\": false}" \
    > "$WORK_DIR/slack-response.json" 2>&1 || true

  if grep -qF '"ok": true' "$WORK_DIR/slack-response.json" 2>/dev/null; then
    log "Slack notification posted successfully"
  else
    warn_log "Slack notification failed: $(cat "$WORK_DIR/slack-response.json")"
  fi
fi

# ---------------------------------------------------------------------------
# Cleanup & Summary
# ---------------------------------------------------------------------------
# Build exec summary (top of report), then append sections below it
EXEC_SUMMARY="$WORK_DIR/exec-summary.md"
{
  echo ""
  echo "---"
  echo ""
  echo "## Executive Summary"
  echo ""
  echo "**Workspace Health: $HEALTH_EMOJI $HEALTH_LABEL** ($HEALTH_SCORE% — $REPORT_PASS_COUNT pass, $REPORT_WARN_COUNT warn, $REPORT_FAIL_COUNT fail)"
  echo ""
  if [[ ${#ISSUES[@]} -gt 0 ]]; then
    echo "### Top 3 Recommendations"
    echo ""
    printf '%s\n' "${ISSUES[@]}" | grep -v '^$' | head -3 | nl -w1 -s'. ' | sed 's/^/  /' || true
    echo ""
  fi
} > "$EXEC_SUMMARY"

# Assemble: header + exec summary + sections
TMP_REPORT="$WORK_DIR/report-assembled.md"
cat "$REPORT_FILE" "$EXEC_SUMMARY" "$SECTIONS_FILE" > "$TMP_REPORT"
{
  echo ""
  echo "---"
  echo ""
  echo "### Issues This Week"
  echo ""
  echo "| # | Issue |"
  echo "|---|---|"
  i=1
  for issue in "${ISSUES[@]}"; do
    echo "| $i | $issue |"
    i=$((i + 1))
  done
  echo ""
  echo "_Report generated by \`scripts/workspace-report.sh\` — week ${WEEK}_"
} >> "$TMP_REPORT"

mv "$TMP_REPORT" "$REPORT_FILE"

rm -rf "$WORK_DIR"
echo "Report written: $REPORT_FILE"
log "Done. PASS=$REPORT_PASS_COUNT WARN=$REPORT_WARN_COUNT FAIL=$REPORT_FAIL_COUNT"
log "Report: $REPORT_FILE"

if [[ "$REPORT_FAIL_COUNT" -gt 0 ]]; then
  exit 1
fi
exit 0
