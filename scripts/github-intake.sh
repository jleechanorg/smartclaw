#!/usr/bin/env bash
# github-intake.sh — GitHub Notification Intake Daemon
#
# Polls GitHub notifications, classifies them, and dispatches actionable
# items to Agent-Orchestrator (AO). Replaces Jeffrey as the human router.
#
# Bead: orch-s91t
#
# Environment variables:
#   INTAKE_DRY_RUN=1       — log actions without executing
#   INTAKE_ENABLED=0       — disable entirely (exit immediately)
#   INTAKE_MAX_DISPATCH=3  — max auto-dispatches per run
#   INTAKE_COOLDOWN=3600   — seconds before re-dispatching same PR
#   INTAKE_STATE_FILE      — path to state JSON (default: ~/.openclaw/state/github-intake.json)
#   INTAKE_SLACK_CHANNEL   — Slack channel for digest (default: C09GRLXF9GR = #all-jleechan-ai)
#   INTAKE_ESCALATE_CHANNEL — Slack channel for escalations (default: user DM D0AFTLEJGJU)
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Source shared library
source "$REPO_ROOT/lib/github-intake-lib.sh"

# Configuration
INTAKE_ENABLED="${INTAKE_ENABLED:-1}"
INTAKE_DRY_RUN="${INTAKE_DRY_RUN:-0}"
INTAKE_MAX_DISPATCH="${INTAKE_MAX_DISPATCH:-3}"
INTAKE_COOLDOWN="${INTAKE_COOLDOWN:-3600}"
INTAKE_STATE_FILE="${INTAKE_STATE_FILE:-$HOME/.openclaw/state/github-intake.json}"
INTAKE_SLACK_CHANNEL="${INTAKE_SLACK_CHANNEL:-C09GRLXF9GR}"
INTAKE_ESCALATE_CHANNEL="${INTAKE_ESCALATE_CHANNEL:-D0AFTLEJGJU}"
AO_DIR="${AO_DIR:-$HOME/projects_reference/agent-orchestrator}"
AO_BIN="${AO_BIN:-$HOME/bin/ao}"
LOG_PREFIX="[github-intake]"

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $LOG_PREFIX $*"; }

# Kill switch
if [[ "$INTAKE_ENABLED" == "0" ]]; then
  log "Disabled (INTAKE_ENABLED=0), exiting."
  exit 0
fi

# Ensure state directory exists
mkdir -p "$(dirname "$INTAKE_STATE_FILE")"
if [[ ! -f "$INTAKE_STATE_FILE" ]]; then
  echo '{"dispatched_prs":{},"total_dispatched":0,"total_escalated":0,"total_skipped":0}' > "$INTAKE_STATE_FILE"
fi

# Fetch notifications
log "Fetching GitHub notifications..."
NOTIFICATIONS="$(gh api notifications -q '.' 2>&1)" || {
  log "ERROR: gh api notifications failed: $NOTIFICATIONS"
  exit 1
}

NOTIF_COUNT="$(echo "$NOTIFICATIONS" | jq 'length')"
log "Found $NOTIF_COUNT notifications"

if [[ "$NOTIF_COUNT" == "0" ]]; then
  log "No notifications. Done."
  exit 0
fi

# Check for active AO sessions
ACTIVE_SESSIONS=""
if [[ -x "$AO_BIN" ]] && [[ -d "$AO_DIR" ]]; then
  ACTIVE_SESSIONS="$(cd "$AO_DIR" && "$AO_BIN" session ls --json 2>/dev/null || echo "[]")"
fi

# Process each notification
dispatched=0
escalated=0
skipped=0
dispatch_details=()
escalation_details=()

for i in $(seq 0 $(( NOTIF_COUNT - 1 ))); do
  notif="$(echo "$NOTIFICATIONS" | jq ".[$i]")"
  notif_id="$(echo "$notif" | jq -r '.id')"
  title="$(echo "$notif" | jq -r '.subject.title // "untitled"')"
  subject_url="$(echo "$notif" | jq -r '.subject.url // ""')"
  repo_name="$(echo "$notif" | jq -r '.repository.full_name // "unknown"')"

  # Classify
  classification="$(classify_notification "$notif")"
  action="$(echo "$classification" | jq -r '.action')"

  case "$action" in
    auto-dispatch)
      # Rate limit check
      rate_check="$(check_rate_limit "$dispatched" "$INTAKE_MAX_DISPATCH")"
      if [[ "$rate_check" == "limited" ]]; then
        log "RATE LIMITED: skipping $title (already dispatched $dispatched this run)"
        skipped=$(( skipped + 1 ))
        continue
      fi

      # Extract PR/issue number
      pr_number="$(extract_pr_number "$subject_url")"
      if [[ -z "$pr_number" ]]; then
        log "SKIP: no PR/issue number for $title"
        skipped=$(( skipped + 1 ))
        continue
      fi

      # Cooldown check
      cooldown_check="$(check_cooldown "$pr_number" "$INTAKE_COOLDOWN")"
      if [[ "$cooldown_check" == "cooldown" ]]; then
        log "COOLDOWN: skipping PR #$pr_number ($title)"
        skipped=$(( skipped + 1 ))
        continue
      fi

      # Map repo to AO project
      ao_project="$(repo_to_ao_project "$repo_name")"
      if [[ -z "$ao_project" ]]; then
        log "SKIP: no AO project mapping for repo $repo_name"
        skipped=$(( skipped + 1 ))
        continue
      fi

      # Check PR state — skip merged/closed PRs
      subject_type_raw="$(echo "$notif" | jq -r '.subject.type // ""')"
      if [[ "$subject_type_raw" == "PullRequest" ]]; then
        pr_state="$(check_pr_state "$repo_name" "$pr_number")"
        if [[ "$pr_state" == "merged" ]]; then
          log "SKIP MERGED: PR #$pr_number ($title)"
          skipped=$(( skipped + 1 ))
          if [[ "$INTAKE_DRY_RUN" != "1" ]]; then
            gh api -X PATCH "notifications/threads/$notif_id" 2>/dev/null || true
          fi
          continue
        elif [[ "$pr_state" == "closed" ]]; then
          log "SKIP CLOSED: PR #$pr_number ($title)"
          skipped=$(( skipped + 1 ))
          if [[ "$INTAKE_DRY_RUN" != "1" ]]; then
            gh api -X PATCH "notifications/threads/$notif_id" 2>/dev/null || true
          fi
          continue
        fi
      fi

      # Check if AO already has an active session for this PR
      if [[ -n "$ACTIVE_SESSIONS" ]] && echo "$ACTIVE_SESSIONS" | jq -e ".[] | select(.pr == $pr_number)" >/dev/null 2>&1; then
        log "ACTIVE SESSION: skipping PR #$pr_number ($title) — already managed by AO"
        skipped=$(( skipped + 1 ))
        continue
      fi

      # Select agent
      agent="$(select_agent "$repo_name" "$title")"

      log "DISPATCH: PR #$pr_number ($title) → $agent via project $ao_project"

      RATE_LIMITED=0
      SPAWN_SUCCESS=0
      if [[ "$INTAKE_DRY_RUN" != "1" ]]; then
        # Pre-spawn: clean Claude Code artifacts from base repo worktrees
        # These files (.claude/settings.json, .claude/metadata-updater.sh) are
        # written by claude sessions and cause "uncommitted changes" errors in AO
        ao_yaml="$HOME/agent-orchestrator.yaml"
        if [[ -f "$ao_yaml" ]]; then
          base_path="$(grep -A3 "^  ${ao_project}:" "$ao_yaml" | grep 'path:' | awk '{print $2}' | sed "s|~|$HOME|")"
          if [[ -n "$base_path" && -d "$base_path" ]]; then
            (cd "$base_path" && git checkout -- .claude/settings.json 2>/dev/null; rm -f .claude/metadata-updater.sh 2>/dev/null) || true
          fi
          wt_dir="$(grep -A6 "^  ${ao_project}:" "$ao_yaml" | grep 'worktreeDir:' | awk '{print $2}' | sed "s|~|$HOME|")"
          if [[ -n "$wt_dir" && -d "$wt_dir" ]]; then
            (cd "$wt_dir" && git checkout -- .claude/settings.json 2>/dev/null; rm -f .claude/metadata-updater.sh 2>/dev/null) || true
          fi
        fi

        # Dispatch via AO - try --claim-pr first
        # Capture exit code directly - script uses set -uo pipefail (no errexit)
        # ao auto-detects project from cwd — do NOT pass project name as positional arg
        # (ao interprets it as "Issue identifier", causing "Multiple projects" error)
        # cd to project-specific directory so ao auto-detects the correct project
        local_path="$(repo_to_local_path "$repo_name")"
        if [[ -z "$local_path" || ! -d "$local_path" ]]; then
          log "ERROR: Unknown repo $repo_name or missing dir $local_path"
          skipped=$(( skipped + 1 ))
          continue
        fi
        spawn_output="$(cd "$local_path" && timeout 30 "$AO_BIN" spawn --claim-pr "$pr_number" 2>&1)"
        spawn_rc=$?

        # Check for success first (rc=0 AND success message), then rate-limit, then failure
        if [[ "$spawn_rc" -eq 0 ]] && echo "$spawn_output" | grep -q "Session .* created and claimed PR"; then
          log "SUCCESS: $(echo "$spawn_output" | grep 'Session')"
          # Update state file with dispatch timestamp on success
          tmp_state="$(mktemp)"
          jq ".dispatched_prs[\"$pr_number\"] = $(date +%s) | .total_dispatched += 1" "$INTAKE_STATE_FILE" > "$tmp_state"
          mv "$tmp_state" "$INTAKE_STATE_FILE"
          dispatch_details+=("PR #$pr_number: $title → $agent ($ao_project)")
          SPAWN_SUCCESS=1
        elif echo "$spawn_output" | grep -qiE "rate.limit|API rate limit|GraphQL rate limit|gh: .* rate|exceeded.*limit"; then
          log "RATE LIMIT: --claim-pr failed for PR #$pr_number, NOT marking as dispatched (will retry on next intake)"
          RATE_LIMITED=1
          # Do NOT fall back to unclaimed spawn - that creates clean worktree from main
          # which bypasses the PR branch and causes duplicate worktree issues
        else
          log "WARNING: ao spawn failed for PR #$pr_number (rc=$spawn_rc): $(echo "$spawn_output" | tail -1)"
          # Update state file on failure too - enables cooldown to prevent infinite retry loops
          tmp_state="$(mktemp)"
          jq ".dispatched_prs[\"$pr_number\"] = $(date +%s)" "$INTAKE_STATE_FILE" > "$tmp_state"
          mv "$tmp_state" "$INTAKE_STATE_FILE"
        fi
      else
        log "DRY RUN: would dispatch PR #$pr_number via $agent"
        # In dry-run, treat as success for reporting purposes
        dispatch_details+=("PR #$pr_number: $title → $agent ($ao_project) [DRY RUN]")
        SPAWN_SUCCESS=1
      fi

      # Only increment dispatched count and mark notification read if spawn succeeded and NOT rate-limited
      if [[ "$SPAWN_SUCCESS" -eq 1 ]] && [[ "$RATE_LIMITED" -eq 0 ]]; then
        dispatched=$(( dispatched + 1 ))
        # Mark notification as read (only in non-dry-run mode)
        if [[ "$INTAKE_DRY_RUN" != "1" ]]; then
          gh api -X PATCH "notifications/threads/$notif_id" 2>/dev/null || true
        fi
      fi
      ;;

    escalate)
      log "ESCALATE: $title ($repo_name)"
      escalation_details+=("$title ($repo_name)")
      escalated=$(( escalated + 1 ))

      # Mark notification as read (we've triaged it)
      if [[ "$INTAKE_DRY_RUN" != "1" ]]; then
        gh api -X PATCH "notifications/threads/$notif_id" 2>/dev/null || true
      fi
      ;;

    skip)
      skipped=$(( skipped + 1 ))
      # Mark noise as read
      if [[ "$INTAKE_DRY_RUN" != "1" ]]; then
        gh api -X PATCH "notifications/threads/$notif_id" 2>/dev/null || true
      fi
      ;;
  esac
done

# Format and post digest
digest="$(format_digest "$dispatched" "$escalated" "$skipped")"
log "$digest"

if [[ "$INTAKE_DRY_RUN" != "1" ]] && (( dispatched + escalated > 0 )); then
  # Build detailed message
  msg="$digest"
  if (( ${#dispatch_details[@]} > 0 )); then
    msg="$msg\n\nDispatched:"
    for d in "${dispatch_details[@]}"; do
      msg="$msg\n• $d"
    done
  fi
  if (( ${#escalation_details[@]} > 0 )); then
    msg="$msg\n\nNeeds human review:"
    for e in "${escalation_details[@]}"; do
      msg="$msg\n• $e"
    done
  fi

  # Post to Slack via bot token
  if [[ -f "$REPO_ROOT/set-slack-env.sh" ]]; then
    source "$REPO_ROOT/set-slack-env.sh"
    curl -s -X POST "https://slack.com/api/chat.postMessage" \
      -H "Authorization: Bearer $OPENCLAW_SLACK_BOT_TOKEN" \
      -H "Content-Type: application/json" \
      -d "$(jq -n --arg channel "$INTAKE_SLACK_CHANNEL" --arg text "$(echo -e "$msg")" '{channel: $channel, text: $text}')" \
      >/dev/null 2>&1 || log "WARNING: Slack digest post failed"
  fi

  # DM escalations to Jeffrey
  if (( ${#escalation_details[@]} > 0 )); then
    esc_msg="[github-intake] Items needing your review:"
    for e in "${escalation_details[@]}"; do
      esc_msg="$esc_msg\n• $e"
    done
    if [[ -n "${OPENCLAW_SLACK_BOT_TOKEN:-}" ]]; then
      curl -s -X POST "https://slack.com/api/chat.postMessage" \
        -H "Authorization: Bearer $OPENCLAW_SLACK_BOT_TOKEN" \
        -H "Content-Type: application/json" \
        -d "$(jq -n --arg channel "$INTAKE_ESCALATE_CHANNEL" --arg text "$(echo -e "$esc_msg")" '{channel: $channel, text: $text}')" \
        >/dev/null 2>&1 || log "WARNING: Slack escalation DM failed"
    fi
  fi
fi

log "Done. dispatched=$dispatched escalated=$escalated skipped=$skipped"
