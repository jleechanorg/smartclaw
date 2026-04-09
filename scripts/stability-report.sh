#!/opt/homebrew/bin/bash
# stability-report.sh
# Analyzes PR and Slack problems in the last 24 hours and posts a stability report.
# Runs every 12 hours via launchd.

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
REPOS=(
  "jleechanorg/jleechanclaw"
  "jleechanorg/worldarchitect.ai"
  "jleechanorg/agent-orchestrator"
  "jleechanorg/ai_universe"
)
LOOKBACK_HOURS=24
REPORT_CHANNEL="${STABILITY_REPORT_CHANNEL:-C09GRLXF9GR}"
AGENT_USER_ID="${OPENCLAW_BOT_USER_ID:-U0AEZC7RX1Q}"
SLACK_TOKEN="${OPENCLAW_SLACK_BOT_TOKEN:-}"

LOG_DIR="${STABILITY_LOG_DIR:-${HOME}/.openclaw/logs}"
mkdir -p "$LOG_DIR"
LOG_FILE="${LOG_DIR}/stability-report.log"
exec 2>> "$LOG_FILE"

log() { echo "[$(date '+%Y-%m-%dT%H:%M:%S')] $*"; }

# GH token
resolve_token() {
  local tok=""
  tok="$(cat "$HOME/.openclaw/openclaw.json" 2>/dev/null | jq -r '.skills.entries["gh-issues"].apiKey // empty' 2>/dev/null)" || true
  tok="${tok:-$GH_TOKEN}"
  tok="${tok:-$GITHUB_TOKEN}"
  printf '%s' "$tok"
}

GH_TOKEN="${GH_TOKEN:-$(resolve_token)}" || true

# ── PR Analysis ───────────────────────────────────────────────────────────────

fetch_recent_prs() {
  local repo=$1
  local oldest_ts now_sec
  now_sec="$(date +%s)"
  oldest_ts=$((now_sec - LOOKBACK_HOURS * 3600))
  gh api "repos/$repo/pulls?state=all&per_page=100" 2>/dev/null | \
    jq --argjson oldest "$oldest_ts" \
       '[.[] | select(.updated_at | sub("Z"; "") | sub("\\.[0-9]+"; "") | strptime("%Y-%m-%dT%H:%M:%S") | mktime >= $oldest)]' 2>/dev/null || echo "[]"
}

get_cr_review() {
  local repo=$1 pr=$2
  gh api "repos/$repo/pulls/$pr/reviews" \
    --jq '[.[] | select(.user.login == "coderabbitai[bot]")] | sort_by(.submitted_at) | reverse | .[0].state' 2>/dev/null || echo "none"
}

get_bugbot_errors() {
  local repo=$1 pr=$2
  gh api "repos/$repo/pulls/$pr/comments" \
    --jq '[.[] | select(.user.login == "cursor[bot]" and (.body | test("error"; "i")))] | length' 2>/dev/null || echo 0
}

analyze_prs() {
  local repo=$1
  local prs_json
  prs_json=$(fetch_recent_prs "$repo")
  local total_prs
  total_prs=$(echo "$prs_json" | jq 'length' 2>/dev/null || echo 0)

  if [[ "$total_prs" -eq 0 ]]; then
    return 0
  fi

  local failed_ci=0 conflicts=0 cr_changes=0 bugbot_errors=0 stale=0
  local report_lines=""

  local pr_count
  pr_count=$(echo "$prs_json" | jq '. | length' 2>/dev/null || echo 0)
  local i=0
  while [[ $i -lt $pr_count ]]; do
    local pr_line
    pr_line=$(echo "$prs_json" | jq -c ".[$i]" 2>/dev/null) || true
    if [[ -z "$pr_line" ]] || [[ "$pr_line" == "null" ]]; then
      ((i++)) || true
      continue
    fi

    local pr_num title state url head_sha created_at
    pr_num=$(echo "$pr_line" | jq -r '.number' 2>/dev/null || echo "")
    title=$(echo "$pr_line" | jq -r '.title' 2>/dev/null || echo "")
    state=$(echo "$pr_line" | jq -r '.state' 2>/dev/null || echo "")
    url=$(echo "$pr_line" | jq -r '.html_url' 2>/dev/null || echo "")
    head_sha=$(echo "$pr_line" | jq -r '.head.sha' 2>/dev/null || echo "")
    created_at=$(echo "$pr_line" | jq -r '.created_at' 2>/dev/null || echo "")

    if [[ -z "$pr_num" ]] || [[ "$pr_num" == "null" ]]; then
      ((i++)) || true
      continue
    fi

    local issues=""
    local green_count=0

    # CI status
    local ci_state
    ci_state=$(gh api "repos/$repo/commits/$head_sha/status" --jq '.state' 2>/dev/null) || ci_state="error"
    if [[ "$ci_state" == "success" ]]; then
      green_count=$((green_count + 1))
    elif [[ "$ci_state" == "failure" || "$ci_state" == "error" ]]; then
      issues="${issues}CI:${ci_state} "
      failed_ci=$((failed_ci + 1))
    fi

    # Merge status
    local mergeable merge_state
    mergeable=$(gh api "repos/$repo/pulls/$pr_num" --jq '.mergeable' 2>/dev/null) || mergeable="unknown"
    merge_state=$(gh api "repos/$repo/pulls/$pr_num" --jq '.mergeable_state' 2>/dev/null) || merge_state="unknown"
    if [[ "$mergeable" == "false" ]] || [[ "$merge_state" == "blocked" ]] || [[ "$merge_state" == "dirty" ]]; then
      issues="${issues}merge:${merge_state} "
      conflicts=$((conflicts + 1))
    else
      green_count=$((green_count + 1))
    fi

    # CR review
    local cr_state
    cr_state=$(get_cr_review "$repo" "$pr_num")
    if [[ "$cr_state" == "CHANGES_REQUESTED" ]]; then
      issues="${issues}CR:CHANGES_REQUESTED "
      cr_changes=$((cr_changes + 1))
    elif [[ "$cr_state" == "APPROVED" ]]; then
      green_count=$((green_count + 1))
    fi

    # Bugbot
    local bugbot_err
    bugbot_err=$(get_bugbot_errors "$repo" "$pr_num")
    if [[ "$bugbot_err" -gt 0 ]]; then
      issues="${issues}bugbot:${bugbot_err}err "
      bugbot_errors=$((bugbot_errors + 1))
    fi

    # Staleness
    local created_sec now_sec age_h
    created_sec=$(date -j -f "%Y-%m-%dT%H:%M:%SZ" "${created_at%Z}" '+%s' 2>/dev/null) || created_sec=0
    now_sec=$(date -u '+%s') || now_sec=0
    age_h=$(( (now_sec - created_sec) / 3600 ))
    if [[ "$age_h" -gt 24 ]] && [[ "$state" == "open" ]]; then
      issues="${issues}stale:${age_h}h "
      stale=$((stale + 1))
    fi

    if [[ -n "${issues// }" ]]; then
      report_lines="${report_lines}• [${repo} PR #${pr_num}](${url}): ${title}\n  Issues: ${issues}\n"
    fi

    ((i++)) || true
  done

  echo "REPO_STATS:${repo}:${total_prs}:${failed_ci}:${conflicts}:${cr_changes}:${bugbot_errors}:${stale}"
  if [[ -n "$report_lines" ]]; then
    echo -e "$report_lines"
  fi
}

# ── Slack Analysis ─────────────────────────────────────────────────────────────

check_dropped_threads() {
  local channel=$1
  local oldest_ts now_sec
  now_sec=$(date +%s)
  oldest_ts=$((now_sec - LOOKBACK_HOURS * 3600))

  local response
  response=$(curl --silent --show-error \
    --connect-timeout 10 --max-time 30 \
    -X POST "https://slack.com/api/conversations.history" \
    -H "Authorization: Bearer $SLACK_TOKEN" \
    -H "Content-Type: application/json" \
    --data "{\"channel\":\"$channel\",\"oldest\":\"$oldest_ts\",\"limit\":200}" 2>/dev/null) || return 0

  local thread_count
  thread_count=$(echo "$response" | jq '.messages | length' 2>/dev/null || echo 0)
  if [[ "$thread_count" -eq 0 ]]; then
    echo "SLACK_DROPPED:0"
    return 0
  fi

  local dropped=0
  local thread_report=""
  local thread_ts_arr
  thread_ts_arr=$(echo "$response" | jq -r '.messages[] | select(.reply_count > 0) | .ts' 2>/dev/null | head -20)

  local thread_ts
  while IFS= read -r thread_ts; do
    [[ -z "$thread_ts" ]] && continue

    local thread_response
    thread_response=$(curl --silent --show-error \
      --connect-timeout 10 --max-time 30 \
      -X POST "https://slack.com/api/conversations.replies" \
      -H "Authorization: Bearer $SLACK_TOKEN" \
      -H "Content-Type: application/json" \
      --data "{\"channel\":\"$channel\",\"ts\":\"$thread_ts\",\"limit\":20}" 2>/dev/null) || continue

    local agent_texts
    agent_texts=$(echo "$thread_response" | jq -r "[.messages[] | select(.user == \"${AGENT_USER_ID}\") | .text] | join(\" \")" 2>/dev/null | tr '[:upper:]' '[:lower:]')

    local thread_age_h
    thread_age_h=$(python3 - "$thread_ts" 2>/dev/null <<'PYEOF'
import sys, time
try:
    sec = float(sys.argv[1])
except:
    sec = 0
age_h = (time.time() - sec) / 3600
print(round(age_h, 1))
PYEOF
)

    if echo "$agent_texts" | grep -qiE "did not execute|only sent an acknowledgment|have not started|have not done|have not yet done|i only|i haven't|i did not|i am not currently working on|stalled|forgot to|missed this"; then
      dropped=$((dropped + 1))
      local thread_url="https://jleechanai.slack.com/archives/${channel}/p${thread_ts%.*}"
      thread_report="${thread_report}• [Thread](${thread_url}) (${thread_age_h}h ago): agent admitted not executing\n"
    fi
  done <<< "$thread_ts_arr"

  echo "SLACK_DROPPED:${dropped}"
  if [[ -n "$thread_report" ]]; then
    echo -e "$thread_report"
  fi
}

# ── Main ───────────────────────────────────────────────────────────────────────
log "Starting stability report (lookback: ${LOOKBACK_HOURS}h)"

if [[ -z "$SLACK_TOKEN" ]]; then
  log "WARN: OPENCLAW_SLACK_BOT_TOKEN not set — skipping Slack analysis"
fi

# Aggregate PR stats
total_failed_ci=0 total_conflicts=0 total_cr=0 total_bugbot=0 total_stale=0 total_prs=0
pr_report=""
output="" l="" stats="" t_val=0 f_val=0 c_val=0 cr_val=0 b_val=0 s_val=0
slack_output="" d_val=0 err="" resp=""

for repo in "${REPOS[@]}"; do
  log "Analyzing PRs for $repo..."
  output=$(analyze_prs "$repo" 2>/dev/null) || output=""
  if [[ -z "$output" ]]; then
    continue
  fi

  echo "$output" | { 
    while IFS= read -r l; do
      if [[ "$l" =~ ^REPO_STATS: ]]; then
        stats=$(echo "$l" | sed 's/^REPO_STATS://')
        t_val=$(echo "$stats" | cut -d: -f2) || t_val=0
        f_val=$(echo "$stats" | cut -d: -f3) || f_val=0
        c_val=$(echo "$stats" | cut -d: -f4) || c_val=0
        cr_val=$(echo "$stats" | cut -d: -f5) || cr_val=0
        b_val=$(echo "$stats" | cut -d: -f6) || b_val=0
        s_val=$(echo "$stats" | cut -d: -f7) || s_val=0
        total_prs=$((total_prs + t_val))
        total_failed_ci=$((total_failed_ci + f_val))
        total_conflicts=$((total_conflicts + c_val))
        total_cr=$((total_cr + cr_val))
        total_bugbot=$((total_bugbot + b_val))
        total_stale=$((total_stale + s_val))
      else
        pr_report="${pr_report}${l}"$'\n'
      fi
    done
  }
done

# Slack analysis
slack_dropped=0
slack_report=""
for channel in $REPORT_CHANNEL; do
  log "Analyzing Slack for $channel..."
  slack_output=$(check_dropped_threads "$channel" 2>/dev/null) || slack_output=""
  if [[ -z "$slack_output" ]]; then
    continue
  fi

  echo "$slack_output" | {
    while IFS= read -r l; do
      if [[ "$l" =~ ^SLACK_DROPPED: ]]; then
        d_val=$(echo "$l" | sed 's/^SLACK_DROPPED://') || d_val=0
        slack_dropped=$((slack_dropped + d_val))
      else
        slack_report="${slack_report}${l}"$'\n'
      fi
    done
  }
done

# Build and post Slack message
TIMESTAMP=$(date '+%Y-%m-%d %H:%M %Z')
total_issues=$((total_failed_ci + total_conflicts + total_cr + total_bugbot + total_stale))
if [[ $total_issues -eq 0 ]]; then
  SUMMARY_EMOJI="🟢"
elif [[ $((total_failed_ci + total_conflicts)) -gt 2 ]]; then
  SUMMARY_EMOJI="🔴"
else
  SUMMARY_EMOJI="🟡"
fi

pr_issues_text=""
if [[ ${#pr_report} -gt 0 ]]; then
  pr_issues_text=$'\n'"*PR Issues (${total_prs} reviewed):*"$'\n'"${pr_report}"
else
  pr_issues_text=$'\n'"*PR Issues:* No issues detected in ${total_prs} PRs reviewed"
fi

slack_issues_text=""
if [[ ${#slack_report} -gt 0 ]]; then
  slack_issues_text=$'\n'"*Slack Issues:*"$'\n'"${slack_report}"
else
  slack_issues_text=$'\n'"*Slack Issues:* No dropped threads detected"
fi

SLACK_MESSAGE="*Stability Report — ${TIMESTAMP}*
${SUMMARY_EMOJI} *${LOOKBACK_HOURS}h Summary:*
• PRs reviewed: ${total_prs}
• Failed CI: ${total_failed_ci} | Merge conflicts: ${total_conflicts} | CR changes: ${total_cr} | Bugbot errors: ${total_bugbot} | Stale: ${total_stale}
• Slack dropped threads: ${slack_dropped}
${pr_issues_text}${slack_issues_text}
_

_Report generated by stability-report.sh (runs every 12h)_"

if [[ -n "$SLACK_TOKEN" ]]; then
  log "Posting to Slack channel $REPORT_CHANNEL..."
  resp=$(curl --silent --show-error --fail \
    --connect-timeout 10 --max-time 30 \
    -X POST "https://slack.com/api/chat.postMessage" \
    -H "Authorization: Bearer $SLACK_TOKEN" \
    -H "Content-Type: application/json" \
    --data "{\"channel\":\"$REPORT_CHANNEL\",\"text\":\"$SLACK_MESSAGE\"}" 2>/dev/null)

  if echo "$resp" | jq -e '.ok == true' > /dev/null 2>&1; then
    log "Slack post successful"
  else
    err=$(echo "$resp" | jq -r '.error // "unknown"' 2>/dev/null || echo "unknown")
    log "Slack post failed: $err"
  fi
else
  log "SKIP: No SLACK_TOKEN — printing report to stdout"
  echo "$SLACK_MESSAGE"
fi

log "Stability report done"
echo ""
echo "============================================"
echo "         STABILITY REPORT SUMMARY"
echo "============================================"
echo "PRs reviewed:     $total_prs"
echo "Failed CI:       $total_failed_ci"
echo "Merge conflicts: $total_conflicts"
echo "CR changes:      $total_cr"
echo "Bugbot errors:   $total_bugbot"
echo "Stale PRs:       $total_stale"
echo "Slack dropped:   $slack_dropped"
echo "============================================"

exit 0
