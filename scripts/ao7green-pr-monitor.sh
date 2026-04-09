#!/usr/bin/env bash
set -euo pipefail

# AO 7-green PR monitor — keeps AO sessions driving PRs toward merge-ready state.
#
# 7-Green criteria (matching skeptic-cron.yml workflow):
#   [1] CI green       — all GH Actions checks pass (completed + success/skipped/neutral/cancelled)
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
LOG_FILE="${AO_MONITOR_LOG:-$HOME/.openclaw/logs/ao7green-pr-monitor.log}"
mkdir -p "$(dirname "$LOG_FILE")"

{
  # Set defaults before set -u takes effect (needed for ${var:-default} expansions)
  GH_TOKEN="${GH_TOKEN:-${GITHUB_TOKEN:-}}"
  GITHUB_TOKEN="${GITHUB_TOKEN:-}"
  REPO="${AO_MONITOR_REPO:-jleechanorg/agent-orchestrator}"
  AO_DIR="${AO_DIR:-$HOME/project_agento/agent-orchestrator}"
  AO_BIN="${AO_BIN:-ao}"
  AO_PROJECT="${AO_MONITOR_PROJECT:-${AO_PROJECT:-agent-orchestrator}}"
  CHANNEL_ID="${AO_MONITOR_CHANNEL:-C0AKALZ4CKW}"

  log() { echo "[$(date '+%Y-%m-%dT%H:%M:%S')] $*"; }

  # ---------------------------------------------------------------------------
  # Token resolution (no set -e inside)
  # ---------------------------------------------------------------------------
  resolve_token() {
    local tok=""
    tok="$(cat "$HOME/.openclaw/openclaw.json" 2>/dev/null | jq -r '.skills.entries["gh-issues"].apiKey // empty' 2>/dev/null)" || true
    tok="${tok:-$GH_TOKEN}"
    tok="${tok:-$GITHUB_TOKEN}"
    tok="${tok:-$(gh auth token 2>/dev/null)}"
    printf '%s' "$tok"
  }

  # ---------------------------------------------------------------------------
  # GH token — resolve once
  # ---------------------------------------------------------------------------
  GH_TOKEN="$(resolve_token)" || true
  [[ -n "$GH_TOKEN" ]] || { log "ERROR: No GH_TOKEN found"; exit 1; }
  export GH_TOKEN

  # ---------------------------------------------------------------------------
  # Pull AO session status once per run (skip on failure — not fatal)
  # Uses openclaw sessions list when ao binary is broken/unavailable.
  # ---------------------------------------------------------------------------
  AO_STATUS="[]"
  if [[ -d "$AO_DIR" ]] && command -v "$AO_BIN" >/dev/null 2>&1; then
    AO_STATUS="$(cd "$AO_DIR" && "$AO_BIN" status --project "$AO_PROJECT" --json 2>/dev/null)" || true
  fi
  # Fallback: use openclaw sessions if ao binary returned empty
  if [[ -z "$AO_STATUS" || "$AO_STATUS" == "[]" ]]; then
    AO_STATUS="$(openclaw sessions --all-agents --active 60 --json 2>/dev/null)" || AO_STATUS="[]"
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
      item_count="$(jq -r '.total_count' <<<"$raw" 2>/dev/null)" || item_count="0"
      log "Search API returned $item_count items (updated >= $week_ago)"
      # Extract PR fields from search results (items array)
      local first_num
      first_num="$(jq -r 'if .items[0].number then .items[0].number else empty end' <<<"$raw" 2>/dev/null)" || true
      if [[ -n "$first_num" && "$first_num" != "null" ]]; then
        jq -c '.items[] | {number, title, author:.user.login, state:.state, headRefName:.head.ref, isDraft:.draft, created_at, updatedAt:.updated_at}' <<<"$raw" || true
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
    first_num="$(jq -r 'if .[0].number then .[0].number else empty end' <<<"$raw" 2>/dev/null)" || true
    if [[ -z "$first_num" ]] || [[ "$first_num" == "null" ]]; then
      echo "[]"; return
    fi
    jq -c '.[] | {number, title, author:.user.login, state:.state, headRefName:.head.ref, isDraft:.draft, created_at, updatedAt:.updated_at}' <<<"$raw" || echo "[]"
  }

  # ---------------------------------------------------------------------------
  # Check: [1] CI green — all GH Actions checks pass (completed + success/skipped/neutral/cancelled)
  # ---------------------------------------------------------------------------
  check_ci() {
    local pr=$1
    local rollup
    rollup="$(gh pr view "$pr" --repo "$REPO" --json statusCheckRollup --jq ".statusCheckRollup" 2>/dev/null)" || return 1
    # Return 0 (true) only if the list of non-green items is empty.
    # Non-green = not COMPLETED, or CheckRun conclusion not SUCCESS/SKIPPED/NEUTRAL/CANCELLED, or StatusContext state not SUCCESS.
    printf '%s' "$rollup" | jq -e "[.[] | select((.__typename == \"CheckRun\" and (.status != \"COMPLETED\" or (.conclusion | . != \"SUCCESS\" and . != \"SKIPPED\" and . != \"NEUTRAL\" and . != \"CANCELLED\"))) or (.__typename == \"StatusContext\" and .state != \"SUCCESS\"))] | length == 0" >/dev/null
  }

  # ---------------------------------------------------------------------------
  # Check: [2] No conflicts — mergeable=true, mergeable_state=clean
  # ---------------------------------------------------------------------------
  check_no_conflicts() {
    local pr=$1
    local m ms
    m="$(gh api "repos/$REPO/pulls/$pr" --jq '.mergeable' 2>/dev/null)" || m="unknown"
    ms="$(gh api "repos/$REPO/pulls/$pr" --jq '.mergeable_state' 2>/dev/null)" || ms="unknown"
    # Use only 'clean' — 'unstable' means some checks failed (even if non-required)
    [[ "$m" == "true" && "$ms" == "clean" ]]
  }

  # ---------------------------------------------------------------------------
  # Check: [3] CR APPROVED — coderabbitai[bot] latest review is APPROVED
  # Also returns CR review state for routing
  # ---------------------------------------------------------------------------
  check_cr_approved() {
    local pr=$1
    local latest_cr state
    latest_cr="$(gh api "repos/$REPO/pulls/$pr/reviews" \
      --jq '[.[] | select(.user.login == "coderabbitai[bot]" and (.state == "APPROVED" or .state == "CHANGES_REQUESTED"))] | sort_by(.submitted_at) | reverse | .[0].state' 2>/dev/null)" || latest_cr="none"
    state="${latest_cr:-none}"
    [[ "$state" == "APPROVED" ]]
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
    local pr_author=$2
    local owner="${REPO%%/*}"
    local name="${REPO#*/}"
    local gql_query
    gql_query="query(\$owner: String!, \$name: String!, \$number: Int!) {
      repository(owner: \$owner, name: \$name) {
        pullRequest(number: \$number) {
          reviewThreads(first: 100) {
            nodes {
              isResolved
              comments(first: 50) {
                nodes {
                  author { login }
                  body
                }
              }
            }
          }
        }
      }
    }"

    local result
    result=$(gh api graphql -f query="$gql_query" -f owner="$owner" -f name="$name" -F number="$pr" 2>/dev/null) || return 1
    
    # Count threads that are NOT resolved AND have non-nit comments from someone other than PR author
    local unresolved
    unresolved=$(echo "$result" | jq -r "[.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved == false) | .comments.nodes[] | select(.author.login != null and (.author.login | ascii_downcase) != (\"$pr_author\" | ascii_downcase) and (.body | test(\"^\\\\s*(nit:|nitpick)\"; \"i\") | not))] | length" 2>/dev/null) || unresolved=0
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
  # Check: [8] Memory fetched — active AO/Claude Code workers in tmux are
  # calling memory tools (memory_search / memory_get) in recent conversation.
  # AO workers run in tmux sessions named like: bb5e6b7f8db3-ao-3311, ao-pr360-v2.
  # Tool invocations appear in tmux pane as "PreToolUse:memory_search" etc.
  # PASS if: no AO workers active (nothing to check), OR >=1 worker called memory.
  # FAIL if: workers exist but none show memory tool calls in visible pane.
  # ---------------------------------------------------------------------------
  check_memory_fetched() {
    local ao_sessions
    ao_sessions="$(tmux list-sessions -F '#{session_name}' 2>/dev/null | grep -E '^ao-|^bb5e6b7f8db3-ao-|^bb5e6b7f8db3-aub-' | grep -v 'orchestrator' | head -20)" || ao_sessions=""

    if [[ -z "$ao_sessions" ]]; then
      # No AO workers running — nothing to check, treat as pass
      return 0
    fi

    local memory_active=0
    while IFS= read -r sess; do
      [[ -z "$sess" ]] && continue
      # Check pane for PreToolUse:memory_search or PostToolUse:memory_get patterns
      if tmux capture-pane -t "$sess" -p 2>/dev/null | grep -qiE 'PreToolUse:memory_search|PreToolUse:memory_get|PostToolUse:memory_search|PostToolUse:memory_get|ToolUse:memory_search|ToolUse:memory_get'; then
        memory_active=1
        break
      fi
    done <<< "$ao_sessions"

    ((memory_active == 1))
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

  # Run memory-fetch check once (global AO worker health indicator, shared across all PRs)
  memory_ok=0
  if check_memory_fetched 2>/dev/null; then
    memory_ok=1
    log "Memory fetch check: PASS — active sessions are calling memory tools"
  else
    log "WARNING: Memory fetch check: FAIL — no active sessions calling memory tools"
  fi

  # Process each PR using jq to extract fields safely
  while IFS= read -r pr_json; do
    [[ -z "$pr_json" ]] && continue
    [[ "$pr_json" == "null" ]] && continue

    # Extract fields with jq using here-strings
    number="$(jq -r '.number' <<<"$pr_json" 2>/dev/null)" || number=""
    title="$(jq -r '.title' <<<"$pr_json" 2>/dev/null)" || title=""
    author="$(jq -r '.author' <<<"$pr_json" 2>/dev/null)" || author=""
    head_ref="$(jq -r '.headRefName' <<<"$pr_json" 2>/dev/null)" || head_ref=""
    draft="$(jq -r '.isDraft' <<<"$pr_json" 2>/dev/null)" || draft="null"
    pr_state="$(jq -r '.state' <<<"$pr_json" 2>/dev/null)" || pr_state="unknown"
    # Prefer updatedAt for age (reflects last modification), fall back to created_at
    modified_at="$(jq -r '.updatedAt' <<<"$pr_json" 2>/dev/null)" || modified_at=""
    created_at_fallback="$(jq -r '.created_at' <<<"$pr_json" 2>/dev/null)" || created_at_fallback=""

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

    # Run 7-green checks (skip on failure — don't exit)
    ci_ok=0 conflicts_ok=0 cr_ok=0 bugbot_ok=0 comments_ok=0 evidence_ok=0 skeptic_ok=0
    check_ci "$number" 2>/dev/null && ci_ok=1 || true
    check_no_conflicts "$number" 2>/dev/null && conflicts_ok=1 || true
    check_cr_approved "$number" 2>/dev/null && cr_ok=1 || true
    check_bugbot_clean "$number" 2>/dev/null && bugbot_ok=1 || true
    check_comments_resolved "$number" "$author" 2>/dev/null && comments_ok=1 || true
    check_evidence_pass "$number" 2>/dev/null && evidence_ok=1 || true
    check_skeptic_pass "$number" 2>/dev/null && skeptic_ok=1 || true

    green_count=$((ci_ok + conflicts_ok + cr_ok + bugbot_ok + comments_ok + evidence_ok + skeptic_ok))
    # Memory is a global AO-health signal; append it as a modifier, not a 8th gate
    if [[ "$memory_ok" -eq 1 ]]; then
      memory_flag="+memory"
    else
      memory_flag="-memory"
    fi
    status=""
    if [[ "$green_count" -ge 7 ]]; then
      status="✅ 7-green"
    elif [[ "$green_count" -ge 4 ]]; then
      status="🟡 ${green_count}/7 ${memory_flag}"
    else
      status="🔴 ${green_count}/7 ${memory_flag}"
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
    [[ "$memory_ok" -eq 0 ]] && blocker_items+=("memory")
    if [[ ${#blocker_items[@]} -gt 0 ]]; then
      IFS=','; blocker_summary="${blocker_items[*]}"; unset IFS
    else
      blocker_summary="none"
    fi

    # Trajectory: on-track means all 7-green AND age <= 3h (not stale) AND memory_ok
    if [[ "$green_count" -ge 7 ]] && [[ "${age_hours:-0}" -le 3 ]] && [[ "$memory_ok" -eq 1 ]]; then
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
