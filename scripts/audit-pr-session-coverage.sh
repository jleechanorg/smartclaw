#!/usr/bin/env bash
# audit-pr-session-coverage.sh — cross-reference open PRs with active AO sessions
# Idempotent: safe to run on cron. Posts Slack summary on each run.
#
# Env vars:
#   AUDIT_PROJECTS  — space-separated project IDs (default: all from agent-orchestrator.yaml)
#   AO_CONFIG_PATH  — path to agent-orchestrator.yaml (default: ~/.openclaw/agent-orchestrator.yaml)
#   OPENCLAW_SLACK_BOT_TOKEN  — bot token for Slack posting
#   AUDIT_DRY_RUN   — set to "1" to log actions without spawning/claiming

set -uo pipefail

CONFIG_PATH="${AO_CONFIG_PATH:-$HOME/.openclaw/agent-orchestrator.yaml}"
LOG_FILE="${HOME}/.openclaw/logs/audit-pr-coverage.log"
SLACK_CHANNEL="C0AKALZ4CKW"
SLACK_TOKEN="${OPENCLAW_SLACK_BOT_TOKEN:-}"
AUDIT_PROJECTS="${AUDIT_PROJECTS:-}"
DRY_RUN="${AUDIT_DRY_RUN:-0}"

ts() { date '+%Y-%m-%dT%H:%M:%S%z'; }

log() { echo "[$(ts)] $*" | tee -a "$LOG_FILE"; }

mkdir -p "$(dirname "$LOG_FILE")"

# --- Rate limit check ---
check_rate_limit() {
  local remaining
  remaining=$(gh api rate_limit --jq '.resources.graphql.remaining' 2>/dev/null) || {
    log "SKIP: unable to read gh graphql rate limit"
    return 1
  }
  [[ "$remaining" =~ ^[0-9]+$ ]] || {
    log "SKIP: invalid gh graphql rate limit value: '$remaining'"
    return 1
  }
  if [[ "$remaining" -lt 10 ]]; then
    log "SKIP: gh rate limit low ($remaining graphql remaining)"
    return 1
  fi
  log "gh rate limit OK ($remaining graphql remaining)"
}

# --- Parse projects from YAML ---
# Returns 0 and prints project list, or returns 1 on parse failure.
# Uses 'return' not 'exit' so it works inside command substitutions.
get_projects() {
  if [[ -n "$AUDIT_PROJECTS" ]]; then
    echo "$AUDIT_PROJECTS"
    return 0
  fi
  local result
  result=$(CONFIG_PATH="$CONFIG_PATH" python3 -c "
import os, yaml
cfg = yaml.safe_load(open(os.environ['CONFIG_PATH']))
for proj in cfg.get('projects', {}):
    print(proj)
" 2>/dev/null)
  if [[ -z "$result" ]]; then
    log "ERROR: failed to parse projects from YAML at '$CONFIG_PATH'. Set AUDIT_PROJECTS explicitly."
    return 1
  fi
  echo "$result"
  return 0
}

# --- Get repo for a project ---
get_repo() {
  local project="$1"
  CONFIG_PATH="$CONFIG_PATH" PROJECT="$project" python3 -c '
import os, yaml
cfg = yaml.safe_load(open(os.environ["CONFIG_PATH"]))
print(cfg.get("projects", {}).get(os.environ["PROJECT"], {}).get("repo", ""))
' 2>/dev/null
}

# --- Parse active PRs from ao session ls output ---
# Extracts PR numbers from URLs (e.g. https://github.com/owner/repo/pull/123).
# Falls back to PR: #NNN label format. Skips any bare #NNN that looks like a
# line number (not from a PR URL or label).
parse_active_prs() {
  local sessions_raw="$1"
  echo "$sessions_raw" \
    | grep -oE 'pull/[0-9]+' \
    | grep -oE '[0-9]+' \
    | sort -u
}

# --- Parse ISO-8601 timestamp to epoch seconds (portable: macOS + Linux) ---
parse_timestamp() {
  local ts_str="$1"
  python3 - "$ts_str" <<'EOF'
import sys
from datetime import datetime, timezone
ts = sys.argv[1].replace('Z', '+00:00')
print(int(datetime.fromisoformat(ts).timestamp()))
EOF
}

# --- Check ao spawn success by output (stricter than exit code alone) ---
# Requires ✓ anchor to distinguish from error "Session X was created, but failed
# to claim PR Y: ..." which also contains "Session ... created and claimed PR".
is_claim_success() {
  local output="$1"
  echo "$output" | grep -qE '✓.*Session.*created and claimed PR'
}

# --- Main ---
main() {
  check_rate_limit || exit 0

  local dry_label=""
  [[ "$DRY_RUN" == "1" ]] && dry_label=" (DRY RUN — no real spawns)"
  log "START audit${dry_label}"

  HEALTHY=0
  ORPHANED=0
  FAILED=0

  # get_projects via array so we can check return status
  local projects_output
  projects_output=$(get_projects) || {
    log "ERROR: get_projects failed — aborting run"
    exit 1
  }
  [[ -z "$projects_output" ]] && { log "ERROR: no projects found"; exit 1; }

  for project in $projects_output; do
    # Get repo from config
    local repo
    repo=$(get_repo "$project")
    [[ -z "$repo" ]] && { log "WARN: no repo for project $project"; continue; }

    # List open PRs
    local pr_json
    pr_json=$(gh pr list -R "$repo" --state open --json number,title,updatedAt 2>/dev/null) || {
      log "ERROR: gh pr list failed for $repo"; continue; }
    [[ -z "$pr_json" || "$pr_json" == "[]" ]] && { log "No open PRs for $repo"; continue; }

    # List active sessions for this project
    local sessions_raw sessions_ok
    sessions_raw=$(ao session ls -p "$project" 2>/dev/null) && sessions_ok="true" || {
      sessions_ok="false"
      log "WARN: ao session ls failed for $project — skipping orphan detection for this project"
    }

    # Extract PR numbers from active sessions
    local active_prs=""
    if [[ "$sessions_ok" == "true" ]]; then
      active_prs=$(parse_active_prs "$sessions_raw")
    fi

    log "DEBUG: project=$project repo=$repo sessions_ok=$sessions_ok active_prs=$active_prs"

    # Process each open PR
    local pr_list
    pr_list=$(echo "$pr_json" | python3 -c "import sys,json; [print(json.dumps(p)) for p in json.load(sys.stdin)]" 2>/dev/null) || continue

    while IFS= read -r pr_line; do
      [[ -z "$pr_line" ]] && continue
      local pr_num updated
      pr_num=$(echo "$pr_line" | python3 -c "import sys,json; print(json.load(sys.stdin)['number'])" 2>/dev/null) || continue
      updated=$(echo "$pr_line" | python3 -c "import sys,json; print(json.load(sys.stdin)['updatedAt'])" 2>/dev/null) || continue

      # Check if this PR has an active session
      local has_session="false"
      for active in $active_prs; do
        if [[ "$active" == "$pr_num" ]]; then
          has_session="true"
          break
        fi
      done

      if [[ "$has_session" == "true" ]]; then
        ((HEALTHY++))
      elif [[ "$sessions_ok" == "false" ]]; then
        log "SKIP: cannot assess orphan status for PR #$pr_num ($repo) — session listing failed"
      else
        # Check age — orphan only if last updated > 1 hour ago
        local updated_ts
        updated_ts=$(parse_timestamp "$updated")
        if [[ -z "$updated_ts" || "$updated_ts" == "0" ]]; then
          log "SKIP: cannot parse updatedAt for PR #$pr_num — skipping (fail-closed on parse error)"
          continue
        fi
        local now_ts
        now_ts=$(date +%s)
        local age_seconds=$(( now_ts - updated_ts ))
        local age_hours=$(( age_seconds / 3600 ))

        if [[ "$age_hours" -ge 1 ]]; then
          log "ORPHANED: $repo PR #$pr_num (last update ${age_hours}h ago)"
          # Attempt claim — wrap so one failure doesn't abort loop
          if [[ "$DRY_RUN" == "1" ]]; then
            log "DRY RUN: would claim $repo PR #$pr_num via ao spawn"
            ((ORPHANED++))
          else
            # Capture spawn output to a temp file to avoid stale log contamination
            local spawn_output
            spawn_output=$(mktemp 2>/dev/null)
            if [[ ! -f "$spawn_output" || -z "$spawn_output" ]]; then
              log "ERROR: mktemp failed for PR #$pr_num — skipping claim attempt"
              ((FAILED++))
            elif timeout 300 ao spawn --claim-pr "$pr_num" -p "$project" >> "$spawn_output" 2>&1; then
              # Also verify output contains success marker (strict criteria per github-intake.sh)
              if is_claim_success "$(cat "$spawn_output")"; then
                log "CLAIMED: $repo PR #$pr_num via ao spawn"
                ((ORPHANED++))
              else
                log "WARN: ao spawn exit 0 but output missing 'Session ... created and claimed PR' — verify manually: $repo PR #$pr_num"
                cat "$spawn_output" >> "$LOG_FILE"
                ((FAILED++))
              fi
            else
              log "FAILED: could not claim $repo PR #$pr_num"
              cat "$spawn_output" >> "$LOG_FILE"
              ((FAILED++))
            fi
            rm -f "$spawn_output"
          fi
        fi
      fi
    done <<< "$pr_list"
  done

  # Post summary to Slack
  local slack_msg
  [[ "$DRY_RUN" == "1" ]] && dry_label=" (DRY RUN — no real spawns)"
  slack_msg="*[AO PR Coverage Audit]*\nHealthy: ${HEALTHY} PRs with active sessions | Orphaned: ${ORPHANED} claimed | Failed: ${FAILED}${dry_label}"

  if [[ "$DRY_RUN" == "1" ]]; then
    log "SKIP: Slack post (DRY RUN)"
  elif [[ -n "$SLACK_TOKEN" ]]; then
    local response
    response=$(curl -s --max-time 15 --connect-timeout 5 \
      -X POST "https://slack.com/api/chat.postMessage" \
      -H "Authorization: Bearer $SLACK_TOKEN" \
      -H "Content-Type: application/json" \
      -d "{\"channel\": \"$SLACK_CHANNEL\", \"text\": \"$slack_msg\"}")
    if echo "$response" | python3 -c "import sys,json; r=json.load(sys.stdin); sys.exit(0 if r.get('ok') else 1)" 2>/dev/null; then
      log "Slack: posted summary"
    else
      log "WARN: Slack post failed: $response"
    fi
  else
    log "SKIP: OPENCLAW_SLACK_BOT_TOKEN not set — no Slack post"
  fi

  log "DONE audit — healthy=$HEALTHY orphaned=$ORPHANED failed=$FAILED"
}

main "$@"
