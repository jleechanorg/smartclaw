#!/usr/bin/env bash
# ao-progress-reporter.sh
# Every 30 min: check all AO worker sessions across all projects,
# report remote commit URLs of work done to Slack, and fix/nudge stalled sessions.
#
# Output goes to Slack thread: original trigger message ts in #agent-orchestrator
# Idempotency: tracks last-reported per (project, session) to avoid spam.
# Guardrails:
#   - DRY_RUN=1: prints actions without executing or posting to Slack
#   - IS_SOURCED=1: allows source for test coverage without running main
#   - Overlap lock prevents concurrent runs

set -euo pipefail

export PATH="$HOME/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin${PATH:+:$PATH}"
trap '' PIPE

# ── Config ────────────────────────────────────────────────────────────────────
LOCK_DIR="${AOPR_LOCK_DIR:-${TMPDIR:-/tmp}/openclaw-ao-progress.lock}"
LOG_DIR="${AOPR_LOG_DIR:-${HOME}/.smartclaw/logs}"
STATE_FILE="${AOPR_STATE_FILE:-$HOME/.smartclaw/logs/ao-progress-state.json}"
REPORT_INTERVAL_SECS="${AOPR_INTERVAL_SECS:-1800}"   # 30 min
SLACK_CHANNEL="${AOPR_SLACK_CHANNEL:-C0ALSKLU9KM}"   # #agent-orchestrator
# Root thread for progress replies (#agent-orchestrator) — update when starting a new AO thread
SLACK_THREAD_TS="${AOPR_SLACK_THREAD_TS:-1775197044.035089}"
AO_DIR="${AO_DIR:-$HOME/project_agento/agent-orchestrator}"
AO_BIN="${AO_BIN:-ao}"

mkdir -p "$LOG_DIR"

log() { echo "[$(date '+%Y-%m-%dT%H:%M:%S')] $*"; }

# Overlap lock
if [[ "${IS_SOURCED:-0}" != "1" ]]; then
  if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    log "SKIP: another instance running"
    exit 0
  fi
  trap 'rmdir "$LOCK_DIR" >/dev/null 2>&1 || true' EXIT
fi

# ── Slack helper ──────────────────────────────────────────────────────────────
post_slack() {
  local text="$1"
  if [[ "${DRY_RUN:-0}" == "1" ]]; then
    log "[DRY_RUN] Slack: $text"
    return
  fi
  local token="${SLACK_BOT_TOKEN:-}"
  if [[ -z "$token" ]]; then
    log "ERROR: SLACK_BOT_TOKEN not set"
    return
  fi
  export SLACK_TEXT="$text"
  python3 -c "
import urllib.request, json, os, sys
text = os.environ.get('SLACK_TEXT', '')
payload = json.dumps({
  'channel': '${SLACK_CHANNEL}',
  'text': text,
  'thread_ts': '${SLACK_THREAD_TS}'
})
req = urllib.request.Request(
  'https://slack.com/api/chat.postMessage',
  data=payload.encode(),
  headers={'Authorization': 'Bearer ${token}', 'Content-Type': 'application/json'},
  method='POST'
)
with urllib.request.urlopen(req) as resp:
  result = json.load(resp)
  sys.exit(0 if result.get('ok') else 1)
" || log "Slack post failed"
}

# ── GH token ─────────────────────────────────────────────────────────────────
resolve_token() {
  local tok=""
  # Try openclaw config(s) for embedded gh token (prod may use ~/.smartclaw_prod)
  for cfg in "$HOME/.smartclaw/openclaw.json" "$HOME/.smartclaw_prod/openclaw.prod.json"; do
    [[ -f "$cfg" ]] || continue
    tok="$(jq -r 'try .skills.entries["gh-issues"].apiKey catch empty' "$cfg" 2>/dev/null)" || tok=""
    [[ -n "$tok" && "$tok" != "null" ]] && break
  done
  # Skip if null or empty
  if [[ -z "$tok" || "$tok" == "null" ]]; then
    tok="${GH_TOKEN:-}"
  fi
  if [[ -z "$tok" || "$tok" == "null" ]]; then
    tok="${GITHUB_TOKEN:-}"
  fi
  # gh CLI is authenticated — use it directly for REST calls via gh api
  if [[ -z "$tok" || "$tok" == "null" ]] && command -v gh >/dev/null 2>&1; then
    tok="$(gh auth token 2>/dev/null)" || tok=""
  fi
  printf '%s' "$tok"
}
GH_TOKEN="$(resolve_token)" || true
[[ -n "$GH_TOKEN" && "$GH_TOKEN" != "null" ]] || { log "ERROR: No GH_TOKEN found"; exit 1; }

# ── Load state ───────────────────────────────────────────────────────────────
load_state() {
  if [[ -f "$STATE_FILE" ]]; then
    cat "$STATE_FILE" 2>/dev/null || echo '{}'
  else
    echo '{}'
  fi
}

save_state() {
  local json=$1
  cat > "$STATE_FILE" <<< "$json"
}

# ── Get AO sessions JSON (all projects) ───────────────────────────────────────
fetch_ao_sessions() {
  local all_sessions="[]"
  if [[ ! -d "$AO_DIR" ]] || ! command -v "$AO_BIN" >/dev/null 2>&1; then
    echo "$all_sessions"
    return
  fi
  # Try JSON status first, fall back to parsing text
  all_sessions="$(cd "$AO_DIR" && "$AO_BIN" status --json 2>/dev/null)" || echo "[]"
  echo "$all_sessions"
}

# ── Get remote commit URL for a branch ───────────────────────────────────────
get_commit_urls() {
  local repo=$1   # owner/repo
  local branch=$2
  local max_commits=${3:-3}
  local commits=""
  commits="$(gh api "repos/$repo/commits/$branch" \
    --jq "[.sha, (.parents[].sha // empty)[] | select(. != null)] | .[0:$max_commits] | .[]" \
    2>/dev/null)" || commits=""
  if [[ -z "$commits" ]]; then
    echo ""
    return
  fi
  local urls=""
  while IFS= read -r sha; do
    [[ -z "$sha" || "$sha" == "null" ]] && continue
    urls+="https://github.com/$repo/commit/$sha
"
  done <<< "$commits"
  echo -n "$urls"
}

# ── Check if session has made progress since last report ─────────────────────
session_has_progress() {
  local session_name=$1
  local state_json=$2
  local last_sha
  last_sha="$(echo "$state_json" | jq -r ".\"$session_name\".last_sha // \"\"" 2>/dev/null)" || last_sha=""
  echo "$last_sha"
}

# ── Get session info from AO status JSON ─────────────────────────────────────
get_session_info() {
  local ao_json=$1
  local session_name=$2
  local info
  info="$(echo "$ao_json" | jq -c "map(select(.name == \"$session_name\")) | .[0]" 2>/dev/null)" || info=""
  echo "$info"
}

# ── Main ──────────────────────────────────────────────────────────────────────
{
  log "Starting AO progress reporter"

  ao_sessions_json="$(fetch_ao_sessions)" || ao_sessions_json="[]"
  current_state="$(load_state)" || current_state="{}"

  if [[ "$ao_sessions_json" == "[]" ]] || [[ -z "$ao_sessions_json" ]]; then
    log "No AO sessions found or AO unavailable"
    post_slack ":zzz: *AO Progress Report* — no active sessions detected"
    exit 0
  fi

  session_count="$(echo "$ao_sessions_json" | jq 'length' 2>/dev/null)" || session_count=0
  log "Found $session_count AO sessions"

  # Collect report lines
  report_blocks=()
  healthy=0
  stalled=0
  no_pr=0

  # Iterate over sessions
  while IFS= read -r session_json; do
    [[ -z "$session_json" || "$session_json" == "null" ]] && continue

    session_name="$(echo "$session_json" | jq -r '.name // empty' 2>/dev/null)" || session_name=""
    project_id="$(echo "$session_json" | jq -r '.projectId // .project // empty' 2>/dev/null)" || project_id=""
    branch="$(echo "$session_json" | jq -r '.branch // empty' 2>/dev/null)" || branch=""
    status="$(echo "$session_json" | jq -r '.status // empty' 2>/dev/null)" || status=""
    pr_url="$(echo "$session_json" | jq -r '.prUrl // .prUrl // empty' 2>/dev/null)" || pr_url=""
    pr_number="$(echo "$session_json" | jq -r '.prNumber // empty' 2>/dev/null)" || pr_number=""

    [[ -z "$session_name" ]] && continue

    # Get repo from project
    repo=""
    case "$project_id" in
      agent-orchestrator) repo="jleechanorg/agent-orchestrator" ;;
      worldarchitect)     repo="jleechanorg/worldarchitect.ai" ;;
      worldai-claw)       repo="jleechanorg/worldai_claw" ;;
      claude-commands)    repo="jleechanorg/claude-commands" ;;
      ralph)              repo="jleechanorg/ralph" ;;
      smartclaw)       repo="jleechanorg/smartclaw" ;;
      ai-universe-living-blog) repo="jleechanorg/ai_universe_living_blog" ;;
      *)                  repo="" ;;
    esac

    # Get last known SHA from state
    last_sha="$(echo "$current_state" | jq -r ".\"$session_name\".last_sha // \"\" " 2>/dev/null)" || last_sha=""

    # Fetch current head SHA (if branch known)
    current_sha=""
    if [[ -n "$branch" && -n "$repo" ]]; then
      current_sha="$(gh api "repos/$repo/commits/$branch" --jq '.sha' 2>/dev/null)" || current_sha=""
    fi

    # Determine if session is making progress
    has_new_commits="no"
    commit_urls=""
    if [[ -n "$current_sha" && "$current_sha" != "$last_sha" ]]; then
      has_new_commits="yes"
      # Get 3 most recent commit URLs
      commit_urls="$(get_commit_urls "$repo" "$branch" 3)"
      # Update state
      current_state="$(echo "$current_state" | jq \
        --arg name "$session_name" \
        --arg sha "$current_sha" \
        --argjson now "$(date +%s)" \
        'setpath([$name]; {last_sha: $sha, last_report: $now})')" || true
    elif [[ -n "$current_sha" ]]; then
      # Same HEAD as last report — refresh last_report only, preserve last_sha
      current_state="$(echo "$current_state" | jq \
        --arg name "$session_name" \
        --argjson now "$(date +%s)" \
        '.[$name] |= (. // {}) + {last_report: $now}' 2>/dev/null)" || true
    fi

    # Classify session health
    case "$status" in
      killed)   label=":skull: killed" ;;
      pr_open)  label=":white_check_mark: PR open" ;;
      working)  label=":hourglass: working" ;;
      spawning) label=":rocket: spawning" ;;
      stuck)    label=":warning: STUCK" ;;
      ci_failed) label=":red_circle: CI failed" ;;
      needs_input) label=":thinking_face: needs input" ;;
      idle)     label=":zzz: idle" ;;
      "")       label=":grey_question: unknown" ;;
      *)        label=":$status:" ;;
    esac

    # Build session report block
    if [[ -n "$repo" && -n "$current_sha" ]]; then
      if [[ -n "$commit_urls" ]]; then
        session_report="• \`$session_name\` ($project_id) $label
  $commit_urls"
      else
        session_report="• \`$session_name\` ($project_id) $label
  $repo @ \`${current_sha:0:7}\`"
      fi
    else
      session_report="• \`$session_name\` ($project_id) $label"
    fi

    # Add PR off-track diagnostics (zero-touch smooth proxy: inactivity > 60m)
    if [[ -n "$repo" && -n "$pr_number" && "$pr_number" != "null" ]]; then
      pr_updated_at="$(gh pr view "$pr_number" --repo "$repo" --json updatedAt --jq '.updatedAt' 2>/dev/null)" || pr_updated_at=""
      if [[ -n "$pr_updated_at" && "$pr_updated_at" != "null" ]]; then
        pr_updated_epoch="$(TZ=UTC date -j -f "%Y-%m-%dT%H:%M:%SZ" "$pr_updated_at" "+%s" 2>/dev/null)" || pr_updated_epoch="0"
        now_epoch="$(date +%s)"
        idle_min=$(( (now_epoch - pr_updated_epoch) / 60 ))
        idle_h=$(( idle_min / 60 ))
        idle_m=$(( idle_min % 60 ))
        if [[ "$idle_min" -gt 60 ]]; then
          offtrack_status=":red_circle: off-track"
        else
          offtrack_status=":green_circle: on-track"
        fi

        fail_summary="$(gh pr checks "$pr_number" --repo "$repo" --json name,state 2>/dev/null | jq -r '[.[] | select(.state != "SUCCESS") | "\(.name):\(.state)"] | .[:3] | join(", ")' 2>/dev/null)" || fail_summary=""
        if [[ -z "$fail_summary" || "$fail_summary" == "null" ]]; then
          fail_summary="none"
        fi

        session_report="$session_report
  PR #$pr_number $offtrack_status | idle ${idle_h}h${idle_m}m | blockers: $fail_summary"
      fi
    fi

    # Detect stalled sessions (working but no commits pushed for >1h or stuck/ci_failed/needs_input)
    if [[ "$status" == "stuck" || "$status" == "ci_failed" || "$status" == "killed" ]]; then
      stalled=$((stalled + 1))
      session_report="$session_report :fire:"
    elif [[ "$status" == "working" && -z "$commit_urls" ]]; then
      # working but no new commits since last report
      stalled=$((stalled + 1))
      session_report="$session_report :warning: (no new commits since last report)"
    elif [[ "$status" == "needs_input" || "$status" == "idle" ]]; then
      no_pr=$((no_pr + 1))
      session_report="$session_report"
    else
      healthy=$((healthy + 1))
    fi

    report_blocks+=("$session_report")

  done <<< "$(echo "$ao_sessions_json" | jq -c '.[]' 2>/dev/null)"

  # Save updated state
  save_state "$current_state"

  # Build and post Slack report
  header="*AO Progress Report* | $(date '+%H:%M PDT') | $session_count sessions"
  if [[ $healthy -gt 0 ]]; then
    header="$header | :white_check_mark: $healthy healthy"
  fi
  if [[ $stalled -gt 0 ]]; then
    header="$header | :warning: $stalled stalled"
  fi
  if [[ $no_pr -gt 0 ]]; then
    header="$header | :grey_question: $no_pr no PR yet"
  fi

  if [[ ${#report_blocks[@]} -eq 0 ]]; then
    post_slack "$header — no active sessions"
  else
    body="$(printf '%s\n' "${report_blocks[@]}")"
    post_slack "$header
$body"
  fi

  log "AO progress reporter done — healthy:$healthy stalled:$stalled no_pr:$no_pr"

} 2>&1 | tee -a "$LOG_DIR/ao-progress-reporter.log"
