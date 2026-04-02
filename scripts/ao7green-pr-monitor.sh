#!/usr/bin/env bash
set -euo pipefail

# AO 7-green PR monitor — keeps AO sessions driving PRs toward merge-ready state.
#
# 7-Green criteria (matching skeptic-cron.yml workflow):
#   [1] CI green       — all GH Actions checks pass (success state)
#   [2] No conflicts   — mergeable=true, mergeState=CLEAN
#   [3] CR APPROVED    — CodeRabbit (coderabbitai[bot]) latest review is APPROVED
#   [4] Bugbot clean   — cursor[bot] has zero error-severity comments
#   [5] Comments resolved — zero unresolved non-nit inline review comments
#   [6] Evidence pass  — evidence-review-bot approved, OR evidence-gate CI passed
#   [7] Skeptic PASS   — github-actions[bot] posted VERDICT: PASS on the PR
#
# PRs are flagged as ⚠️ if older than 3 hours.

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
REPO="${AO_MONITOR_REPO:-jleechanorg/agent-orchestrator}"
AO_DIR="${AO_DIR:-$HOME/project_agento/agent-orchestrator}"
AO_BIN="${AO_BIN:-ao}"
AO_PROJECT="${AO_PROJECT:-agent-orchestrator}"
CHANNEL_ID="${AO_MONITOR_CHANNEL:-${SLACK_CHANNEL_ID}}"
LOG_FILE="${AO_MONITOR_LOG:-$HOME/.smartclaw/logs/ao7green-pr-monitor.log}"

mkdir -p "$(dirname "$LOG_FILE")"
exec 2>> "$LOG_FILE"

log() { echo "[$(date '+%Y-%m-%dT%H:%M:%S')] $*"; }

# ---------------------------------------------------------------------------
# Token resolution (no set -e inside)
# ---------------------------------------------------------------------------
resolve_token() {
  local tok=""
  tok="$(cat "$HOME/.smartclaw/openclaw.json" 2>/dev/null | jq -r '.skills.entries["gh-issues"].apiKey // empty' 2>/dev/null)" || true
  tok="${tok:-$GH_TOKEN}"
  tok="${tok:-$GITHUB_TOKEN}"
  printf '%s' "$tok"
}

# ---------------------------------------------------------------------------
# GH token — resolve once
# ---------------------------------------------------------------------------
GH_TOKEN="$(resolve_token)" || true
[[ -n "$GH_TOKEN" ]] || { log "ERROR: No GH_TOKEN found"; exit 1; }

# ---------------------------------------------------------------------------
# Pull AO session status once per run (skip on failure — not fatal)
# ---------------------------------------------------------------------------
AO_STATUS="[]"
if [[ -d "$AO_DIR" ]] && command -v "$AO_BIN" >/dev/null 2>&1; then
  AO_STATUS="$(cd "$AO_DIR" && "$AO_BIN" status --project "$AO_PROJECT" --json 2>/dev/null)" || true
fi

# ---------------------------------------------------------------------------
# Fetch PRs modified in the last 7 days via GitHub Search API.
# Includes open, closed, merged — any PR with updated_at >= 7 days ago.
# Falls back to plain open-PRs list on failure (e.g. search not available).
# Returns compact JSON objects, one per line.
# ---------------------------------------------------------------------------
fetch_open_prs() {
  local raw
  local week_ago
  week_ago="$(TZ=UTC date -u -v-7d '+%Y-%m-%d' 2>/dev/null)" || week_ago="$(date -u -d '7 days ago' '+%Y-%m-%d' 2>/dev/null)"

  # Try search API first (includes all states, filtered by updated date)
  raw="$(gh api "search/issues?q=repo:$REPO+is:pr+updated:>=$week_ago&per_page=100&sort=updated&order=desc" 2>/dev/null)" || raw=""

  if [[ -n "$raw" ]]; then
    local item_count
    item_count="$(jq -rn "$raw | .total_count" 2>/dev/null)" || item_count="0"
    log "Search API returned $item_count items (updated >= $week_ago)"
    # Extract PR fields from search results (items array)
    local first_num
    first_num="$(jq -rn "$raw | if .items[0].number then .items[0].number else empty end" 2>/dev/null)" || true
    if [[ -n "$first_num" && "$first_num" != "null" ]]; then
      jq -c '.items[] | {number, title, state:.state, headRefName:.head.ref, isDraft:.draft, created_at, updatedAt:.updated_at}' <<<"$raw" || true
      return
    fi
  fi

  # Fallback: plain open PRs list
  log "Search API empty/failed — falling back to open-PRs list"
  raw="$(gh api "repos/$REPO/pulls?state=open&per_page=100" 2>/dev/null)" || true
  if [[ -z "$raw" ]]; then
    echo "[]"; return
  fi
  local first_num
  first_num="$(jq -rn "$raw | if .[0].number then .[0].number else empty end" 2>/dev/null)" || true
  if [[ -z "$first_num" ]] || [[ "$first_num" == "null" ]]; then
    echo "[]"; return
  fi
  jq -c '.[] | {number, title, state:.state, headRefName:.head.ref, isDraft:.draft, created_at, updatedAt:.updated_at}' <<<"$raw" || echo "[]"
}

# ---------------------------------------------------------------------------
# Check: [1] CI green — all GH Actions checks pass (success state)
# ---------------------------------------------------------------------------
check_ci() {
  local pr=$1 sha=$2
  local state
  state="$(gh api "repos/$REPO/commits/$sha/status" --jq '.state' 2>/dev/null)" || state="error"
  [[ "$state" == "success" ]]
}

# ---------------------------------------------------------------------------
# Check: [2] No conflicts — mergeable=true, mergeable_state=clean
# ---------------------------------------------------------------------------
check_no_conflicts() {
  local pr=$1
  local m ms
  m="$(gh api "repos/$REPO/pulls/$pr" --jq '.mergeable' 2>/dev/null)" || m="unknown"
  ms="$(gh api "repos/$REPO/pulls/$pr" --jq '.mergeable_state' 2>/dev/null)" || ms="unknown"
  [[ "$m" == "true" && ("$ms" == "clean" || "$ms" == "unstable") ]]
}

# ---------------------------------------------------------------------------
# Check: [3] CR APPROVED — coderabbitai[bot] latest review is APPROVED
# Also returns CR review state for routing
# ---------------------------------------------------------------------------
check_cr_approved() {
  local pr=$1
  local latest_cr state
  latest_cr="$(gh api "repos/$REPO/pulls/$pr/reviews" \
    --jq '[.[] | select(.user.login == "coderabbitai[bot]")
          | {state: .state, submitted_at: .submitted_at}]
          | sort_by(.submitted_at) | reverse | .[0].state' 2>/dev/null)" || latest_cr="none"
  state="${latest_cr:-none}"
  [[ "$state" == "APPROVED" ]]
}

get_cr_review_decision() {
  local pr=$1
  gh api "repos/$REPO/pulls/$pr/reviews" \
    --jq '[.[] | select(.user.login == "coderabbitai[bot]")]
          | sort_by(.submitted_at) | reverse | .[0].state' 2>/dev/null || echo "none"
}

# ---------------------------------------------------------------------------
# Check: [4] Bugbot clean — cursor[bot] has zero error-severity comments
# ---------------------------------------------------------------------------
check_bugbot_clean() {
  local pr=$1
  local err_count
  err_count="$(gh api "repos/$REPO/pulls/$pr/comments" \
    --jq '[.[] | select(.user.login == "cursor[bot]" and (.body | test("error"; "i")))] | length' 2>/dev/null)" || err_count=1
  ((err_count == 0))
}

# ---------------------------------------------------------------------------
# Check: [5] Comments resolved — zero unresolved non-nit inline review comments
# ---------------------------------------------------------------------------
check_comments_resolved() {
  local pr=$1
  local unresolved
  unresolved="$(gh api "repos/$REPO/pulls/$pr/comments" \
    --jq '[.[] | select(.body | test("nit"; "i") | not) and (.state != "RESOLVED")] | length' 2>/dev/null)" || unresolved=0
  ((unresolved == 0))
}

# ---------------------------------------------------------------------------
# Check: [6] Evidence pass — evidence-review-bot approved
# ---------------------------------------------------------------------------
check_evidence_pass() {
  local pr=$1
  local pass_count
  pass_count="$(gh api "repos/$REPO/issues/$pr/comments" \
    --jq '[.[] | select(.user.login == "evidence-review-bot" and (.body | contains("APPROVED")))] | length' 2>/dev/null)" || pass_count=0
  ((pass_count > 0))
}

# ---------------------------------------------------------------------------
# Check: [7] Skeptic PASS — github-actions[bot] posted VERDICT: PASS
# ---------------------------------------------------------------------------
check_skeptic_pass() {
  local pr=$1
  local verdict
  verdict="$(gh api "repos/$REPO/issues/$pr/comments" \
    --jq '[.[] | select(.user.login == "github-actions[bot]" and (.body | contains("VERDICT: PASS")))] | length' 2>/dev/null)" || verdict=0
  ((verdict > 0))
}

# ---------------------------------------------------------------------------
# Compute PR age in hours and minutes
# Returns "Xh Ym" format
# ---------------------------------------------------------------------------
compute_age_hm() {
  local created_at=$1
  # Use TZ=UTC so date -j parses the input as UTC (avoids local-tz offset)
  local created_sec now_sec total_min
  created_sec="$(TZ=UTC date -j -f "%Y-%m-%dT%H:%M:%S" "${created_at%Z}" "+%s" 2>/dev/null)" || created_sec=0
  now_sec="$(TZ=UTC date -u "+%s" 2>/dev/null)" || now_sec=0
  total_min=$(( (now_sec - created_sec) / 60 ))
  local hours=$(( total_min / 60 ))
  local mins=$(( total_min % 60 ))
  echo "${hours}h ${mins}m"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
{
  log "Starting AO 7-green monitor for $REPO"

  # Fetch and process all PRs
  pr_lines="$(fetch_open_prs)" || pr_lines=""

  if [[ -z "$pr_lines" ]] || [[ "$pr_lines" == "[]" ]]; then
    log "No open PRs found"
    exit 0
  fi

  pr_count="$(echo "$pr_lines" | wc -l | tr -d ' ')"
  log "Found $pr_count open PRs"

  # Track stats
  total=0 ready=0 concerned=0

  # Process each PR using jq to extract fields safely
  while IFS= read -r pr_json; do
    [[ -z "$pr_json" ]] && continue
    [[ "$pr_json" == "null" ]] && continue

    # Extract fields with jq
    number="$(jq -rn "$pr_json | .number" 2>/dev/null)" || number=""
    title="$(jq -rn "$pr_json | .title" 2>/dev/null)" || title=""
    head_ref="$(jq -rn "$pr_json | .headRefName" 2>/dev/null)" || head_ref=""
    draft="$(jq -rn "$pr_json | .isDraft" 2>/dev/null)" || draft="null"
    pr_state="$(jq -rn "$pr_json | .state" 2>/dev/null)" || pr_state="unknown"
    # Prefer updatedAt for age (reflects last modification), fall back to created_at
    modified_at="$(jq -rn "$pr_json | .updatedAt" 2>/dev/null)" || modified_at=""
    created_at_fallback="$(jq -rn "$pr_json | .created_at" 2>/dev/null)" || created_at_fallback=""

    [[ -z "$number" || "$number" == "null" ]] && continue

    # Use updatedAt for age if available, else created_at
    age_ts="${modified_at:-$created_at_fallback}"
    age_hm="$(compute_age_hm "$age_ts")"
    # Extract numeric hours for trajectory threshold check
    age_hours="${age_hm%%h*}"
    [[ -z "${age_hours:-}" ]] && age_hours=0

    # For closed/merged PRs: skip 7-green checks, show final state
    if [[ "$pr_state" == "closed" || "$pr_state" == "merged" ]]; then
      if [[ "$pr_state" == "merged" ]]; then
        echo "PR #$number | age: ${age_hm} | ✅ MERGED — ${title}"
      else
        echo "PR #$number | age: ${age_hm} | ⚫ CLOSED — ${title}"
      fi
      ((total++)) || true
      continue
    fi

    # Skip drafts
    if [[ "$draft" == "true" ]]; then
      echo "PR #$number | age: ${age_hm} | SKIP (draft)"
      continue
    fi

    # Get per-PR mergeable state and head SHA
    mergeable="$(gh api "repos/$REPO/pulls/$number" --jq '.mergeable' 2>/dev/null)" || mergeable="unknown"
    merge_state="$(gh api "repos/$REPO/pulls/$number" --jq '.mergeable_state' 2>/dev/null)" || merge_state="unknown"
    head_sha="$(gh api "repos/$REPO/pulls/$number" --jq '.head.sha' 2>/dev/null)" || head_sha=""

    # Get CR review decision
    review="$(get_cr_review_decision "$number")"

    # Run 7-green checks (skip on failure — don't exit)
    ci_ok=0 conflicts_ok=0 cr_ok=0 bugbot_ok=0 comments_ok=0 evidence_ok=0 skeptic_ok=0
    check_ci "$number" "$head_sha" 2>/dev/null && ci_ok=1 || true
    check_no_conflicts "$number" 2>/dev/null && conflicts_ok=1 || true
    check_cr_approved "$number" 2>/dev/null && cr_ok=1 || true
    check_bugbot_clean "$number" 2>/dev/null && bugbot_ok=1 || true
    check_comments_resolved "$number" 2>/dev/null && comments_ok=1 || true
    check_evidence_pass "$number" 2>/dev/null && evidence_ok=1 || true
    check_skeptic_pass "$number" 2>/dev/null && skeptic_ok=1 || true

    green_count=$((ci_ok + conflicts_ok + cr_ok + bugbot_ok + comments_ok + evidence_ok + skeptic_ok))
    status=""
    if [[ "$green_count" -ge 7 ]]; then
      status="✅ 7-green"
    elif [[ "$green_count" -ge 4 ]]; then
      status="🟡 ${green_count}/7"
    else
      status="🔴 ${green_count}/7"
    fi

    # Build blocker_summary: list checks that are not ok
    blocker_items=()
    [[ "$ci_ok" -eq 0 ]] && blocker_items+=("CI")
    [[ "$conflicts_ok" -eq 0 ]] && blocker_items+=("conflicts")
    [[ "$cr_ok" -eq 0 ]] && blocker_items+=("CR")
    [[ "$bugbot_ok" -eq 0 ]] && blocker_items+=("Bugbot")
    [[ "$comments_ok" -eq 0 ]] && blocker_items+=("comments")
    [[ "$evidence_ok" -eq 0 ]] && blocker_items+=("evidence")
    [[ "$skeptic_ok" -eq 0 ]] && blocker_items+=("skeptic")
    if [[ ${#blocker_items[@]} -gt 0 ]]; then
      IFS=','; blocker_summary="${blocker_items[*]}"; unset IFS
    else
      blocker_summary="none"
    fi

    # Trajectory: on-track means all 7-green AND age <= 3h (not stale)
    if [[ "$green_count" -ge 7 ]] && [[ "${age_hours:-0}" -le 3 ]]; then
      trajectory="on-track"
    else
      trajectory="at-risk"
    fi

    # Check AO session assignment
    session_name="" session_status="none"
    session_name="$(jq -rn --argjson ao "$AO_STATUS" \
      --argjson n "$number" \
      '$ao | map(select(.prNumber == $n)) | .[0].name // empty' 2>/dev/null)" || session_name=""
    if [[ -n "$session_name" ]]; then
      session_status="$(jq -rn --argjson ao "$AO_STATUS" \
        --argjson n "$number" \
        '$ao | map(select(.prNumber == $n)) | .[0].status // "idle"' 2>/dev/null)" || session_status="none"
    fi

    # Map trajectory to standard status: on-track → ok; at-risk/off-track → concerning
    if [[ "$trajectory" == "on-track" ]]; then
      pr_status="ok"
    else
      pr_status="concerning"
    fi
    echo "PR #$number — age: ${age_hm} — status: ${pr_status}"

    ((total++)) || true

  done <<< "$pr_lines"

  log "AO 7-green monitor done — $total PRs checked"
} 2>&1 | tee -a "$LOG_FILE"
