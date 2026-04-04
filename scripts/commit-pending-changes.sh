#!/usr/bin/env bash
# commit-pending-changes.sh
# Runs every 30 min via launchd. Detects uncommitted changes to already-tracked
# files in ~/.smartclaw, stages+commits them on a feature branch, and opens/updates
# a PR. Untracked files trigger a Slack warning (never auto-committed).
#
# Safety rules:
#   - Only touches already-git-tracked files (git add <tracked files>)
#   - Untracked files are NEVER auto-added or auto-committed
#   - Untracked files → Slack warning only
#
# AO LLM fallback: if the core git/PR path fails, spawn an AO agent to diagnose
# and fix the issue, posting results to Slack.
#
# Idempotency: tracks last-commit SHA per run to avoid re-committing unchanged state.
# Overlap lock prevents concurrent runs.

set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin${PATH:+:$PATH}"

# Suppress SIGPIPE
trap '' PIPE

# ── Config ────────────────────────────────────────────────────────────────────
LOCK_DIR="${CPC_LOCK_DIR:-${TMPDIR:-/tmp}/openclaw-commit-pending.lock}"
LOG_DIR="${CPC_LOG_DIR:-${HOME}/.smartclaw/logs}"
STATE_FILE="${CPC_STATE_FILE:-$HOME/.smartclaw/logs/commit-pending-state.json}"
COMMIT_LOG="${CPC_COMMIT_LOG:-$HOME/.smartclaw/logs/commit-pending.log}"

# Default 30 minutes; plist uses StartInterval=1800
RUN_INTERVAL_SECS="${CPC_RUN_INTERVAL_SECS:-1800}"

REPO="${CPC_REPO:-${HOME}/.smartclaw}"
# Respect existing git config first; override via CPC_GIT_EMAIL / CPC_GIT_NAME if needed
GIT_EMAIL="${CPC_GIT_EMAIL:-$(git config user.email 2>/dev/null || echo jeffrey@openclaw.ai)}"
GIT_NAME="${CPC_GIT_NAME:-$(git config user.name 2>/dev/null || echo 'OpenClaw Auto-Commit')}"
PR_BRANCH="${CPC_PR_BRANCH:-auto/commit-pending}"
PR_TITLE_PREFIX="${CPC_PR_TITLE_PREFIX:-[Auto]}"   # PR title = "$PR_TITLE_PREFIX Changes"}

# Slack
SLACK_TOKEN="${SLACK_BOT_TOKEN:-}"
SLACK_CHANNEL="${CPC_SLACK_CHANNEL:-${JLEECHAN_DM_CHANNEL:-}}"  # DM channel
SLACK_AS_USER="${CPC_SLACK_AS_USER:-1}"   # 1=post as bot, 0=post as user

# AO fallback
USE_AO_FALLBACK="${CPC_USE_AO_FALLBACK:-1}"  # 0 to disable AO fallback

mkdir -p "$LOG_DIR"

# ── Helpers ───────────────────────────────────────────────────────────────────

log() { echo "[$(date '+%Y-%m-%dT%H:%M:%S')] [commit-pending] $*" | tee -a "$COMMIT_LOG"; }

load_state() {
  if [[ -f "$STATE_FILE" ]]; then
    local content
    content="$(cat "$STATE_FILE" 2>/dev/null)" || content='{}'
    # Validate it's valid JSON before returning
    echo "$content" | jq -e '.' >/dev/null 2>&1 && echo "$content" || echo '{}'
  else
    echo '{}'
  fi
}

save_state() {
  local tmp
  tmp="$(mktemp "$STATE_FILE.XXXXXX")"
  cat > "$tmp" < /dev/stdin
  mv "$tmp" "$STATE_FILE"
}

was_run_recently() {
  local state last_ts now_sec ts_sec
  state="$(load_state)"
  last_ts="$(printf '%s' "$state" | jq -r '.last_run_ts // empty' 2>/dev/null)" || last_ts=""
  [[ -z "$last_ts" || "$last_ts" == "null" ]] && return 1
  now_sec="$(date +%s)"
  # Cross-platform: macOS uses -j -f, Linux uses -d
  if [[ "$(uname -s)" == "Darwin" ]]; then
    ts_sec="$(date -j -f "%Y-%m-%dT%H:%M:%SZ" "$last_ts" '+%s' 2>/dev/null)" || return 1
  else
    ts_sec="$(date -d "$last_ts" '+%s' 2>/dev/null)" || return 1
  fi
  [[ $((now_sec - ts_sec)) -lt RUN_INTERVAL_SECS ]] && return 0
  return 1
}

record_run() {
  # Reset the exponential backoff counter when tracked work was committed.
  # This is the key to backoff behavior: untracked-only runs accumulate backoff
  # (1x→2x→4x) and reset back to 1x on the next successful tracked-work run.
  local now_iso reset_backoff="${1:-false}"
  now_iso="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  if [[ "$reset_backoff" == "true" ]]; then
    load_state | jq --arg ts "$now_iso" '.last_run_ts = $ts | .untracked_warning_count = 0' | save_state
  else
    load_state | jq --arg ts "$now_iso" '.last_run_ts = $ts' | save_state
  fi
}

warn_untracked() {
  local count="${1:-unknown}"
  local state untracked_count
  state="$(load_state)"
  untracked_count="$(printf '%s' "$state" | jq -r '.untracked_warning_count // 0' 2>/dev/null || echo '0')"

  # Exponential backoff: only warn every N intervals (doubling each time)
  # count 0→warn at 30min, 1→warn at 60min, 2→warn at 120min (cap)
  local backoff_multiplier
  case "$untracked_count" in
    0) backoff_multiplier=1 ;;
    1) backoff_multiplier=2 ;;
    *) backoff_multiplier=4 ;;
  esac
  local backoff_sec=$((RUN_INTERVAL_SECS * backoff_multiplier))

  local state_ts now_s ts_s
  state_ts="$(printf '%s' "$state" | jq -r '.last_untracked_warn // empty' 2>/dev/null)" || state_ts=""
  now_s="$(date +%s)"
  if [[ -n "$state_ts" && "$state_ts" != "null" ]]; then
    if [[ "$(uname -s)" == "Darwin" ]]; then
      ts_s="$(date -j -f "%Y-%m-%dT%H:%M:%SZ" "$state_ts" '+%s' 2>/dev/null)" || ts_s=0
    else
      ts_s="$(date -d "$state_ts" '+%s' 2>/dev/null)" || ts_s=0
    fi
    if [[ $((now_s - ts_s)) -lt backoff_sec ]]; then
      log "SKIP: untracked warning suppressed by backoff (count=$untracked_count, next in ${backoff_sec}s)"
      return 0
    fi
  fi

  local files
  files="$(untracked_files | head -20)" || files="(could not list)"

  # CPC_DISABLE_SLACK=1 skips the Slack API call entirely (used by tests).
  # Always log the warning and update state regardless of Slack outcome.
  if [[ "${CPC_DISABLE_SLACK:-0}" != "1" ]]; then
    slack_post "⚠️ Untracked files in ~/.smartclaw ($count total) — NOT auto-committed.

Top 20:
$(echo "$files" | head -20)

Add to git manually if intentional." || log "WARN: Slack notification failed"
  fi

  log "WARN: $count untracked files — NOT auto-committed"
  load_state | jq \
    --argjson c $((untracked_count + 1)) \
    --arg ts "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
    '.untracked_warning_count = $c | .last_untracked_warn = $ts' | save_state
}

# ── Slack ─────────────────────────────────────────────────────────────────────

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

MCP_MAIL_BOT_TOKEN="$(resolve_mcp_mail_token)"

slack_post() {
  local text="$1"
  local token

  if [[ "$SLACK_AS_USER" == "0" ]]; then
    token="${SLACK_USER_TOKEN:-}"
  else
    token="${SLACK_TOKEN:-${MCP_MAIL_BOT_TOKEN:-}}"
  fi

  if [[ -z "$token" ]]; then
    log "WARN: no Slack token — skipping notification (set SLACK_BOT_TOKEN to enable)"
    return 0
  fi

  local payload
  if [[ -n "$SLACK_CHANNEL" ]]; then
    payload="$(jq -n \
      --arg ch "$SLACK_CHANNEL" \
      --arg txt "[AI Terminal: commit-pending] $text" \
      '{channel: $ch, text: $txt, unfold_multiple_attachments: false}')"
  else
    payload="$(jq -n \
      --arg txt "[AI Terminal: commit-pending] $text" \
      '{text: $txt}')"
  fi

  curl --silent --show-error --fail \
    --connect-timeout 10 --max-time 30 \
    -X POST "https://slack.com/api/chat.postMessage" \
    -H "Authorization: Bearer $token" \
    -H "Content-Type: application/json" \
    -d "$payload" | jq -e '.ok == true' > /dev/null 2>&1
}

# TODO: wire slack_upload into the commit or PR flow when Slack snippet posting is needed.
slack_upload() {
  # Upload a text file as a Slack snippet
  local content="$1" title="${2:-commit-pending output}"
  local token="${SLACK_TOKEN:-${MCP_MAIL_BOT_TOKEN:-}}"
  [[ -z "$token" ]] && return 1

  local tmpfile
  tmpfile="$(mktemp /tmp/commit-pending-slack-XXXXXX.txt)"
  echo "$content" > "$tmpfile"

  local result
  result="$(curl --silent --show-error \
    --connect-timeout 15 --max-time 60 \
    -X POST "https://slack.com/api/files.uploadV2" \
    -H "Authorization: Bearer $token" \
    -F "filename=$title" \
    -F "title=$title" \
    -F "channels=${SLACK_CHANNEL:-}" \
    -F "content=@$tmpfile" 2>/dev/null)"

  rm -f "$tmpfile"
  echo "$result" | jq -e '.ok == true' > /dev/null 2>&1
}

# ── AO LLM Fallback ───────────────────────────────────────────────────────────

run_ao_fallback() {
  local error_summary="$1"
  log "AO FALLBACK: spawning AO agent to diagnose failure"

  # Build a concise context summary
  local git_status git_diff
  git_status="$(cd "$REPO" && git status --short 2>/dev/null || echo "git unavailable")"
  local untracked
  untracked="$(echo "$git_status" | grep '^??' | wc -l | tr -d ' ' || echo '0')"
  git_diff="$(cd "$REPO" && git diff --stat HEAD 2>/dev/null | tail -1 || echo 'none')"

  local ao_task="Diagnose and fix the commit-pending-changes.sh failure in smartclaw.

Error summary: $error_summary

Git status (first 20 lines):
$(echo "$git_status" | head -20)

Untracked file count: $untracked

Git diff stat: $git_diff

Tasks:
1. Diagnose why scripts/commit-pending-changes.sh failed
2. Fix ONLY the specific error in that script
3. Test the fix with a dry run (CPC_DRY_RUN=1)
4. DO NOT commit unrelated changes
5. Report findings in the PR comment — do NOT force-push or overwrite main

Do NOT make arbitrary changes beyond the specific failing function."

  # Try AO spawn; fall back to direct execution if unavailable
  if [[ "${USE_AO_FALLBACK:-1}" == "1" ]] && command -v ao >/dev/null 2>&1; then
    log "AO FALLBACK: spawning AO agent (non-blocking)..."
    local ao_output
    ao_output="$(ao spawn \
      --repo jleechanorg/smartclaw \
      --project smartclaw \
      --task "$ao_task" \
      --model minimax/MiniMax-M2.7 \
      2>&1)"
    if [ $? -eq 0 ]; then
      log "AO FALLBACK: agent spawned successfully"
    else
      log "AO FALLBACK: spawn returned non-zero — check AO session for outcome"
    fi
    log "AO FALLBACK: see AO dashboard for agent status"
  else
    log "AO FALLBACK: ao binary not available — logging error for manual review"
    log "ERROR: commit-pending-changes failed and AO fallback unavailable"
    log "Git status at time of failure:"
    log "$git_status"
  fi
}

# ── Git helpers ───────────────────────────────────────────────────────────────

tracked_changes() {
  cd "$REPO" || return 1
  # All modified or staged tracked files (untracked files are handled separately)
  git diff --name-only HEAD 2>/dev/null
  git diff --cached --name-only HEAD 2>/dev/null
}

untracked_files() {
  cd "$REPO" || return 1
  # Truly untracked: not in git index at all
  git ls-files --others --exclude-standard 2>/dev/null
}

open_pr_url() {
  cd "$REPO" || return 1
  gh pr view "$PR_BRANCH" --json url --jq '.url' 2>/dev/null || echo ""
}

open_pr_number() {
  cd "$REPO" || return 1
  gh pr view "$PR_BRANCH" --json number --jq '.number' 2>/dev/null || echo ""
}

# ── Commit + PR logic ────────────────────────────────────────────────────────

do_commit_and_pr() {
  local changed_files untracked_count pr_url pr_num commit_msg
  local branch="$PR_BRANCH"

  # Check for tracked changes
  changed_files="$(tracked_changes | sort -u | grep -v '^$')" || true
  untracked_count="$(untracked_files | wc -l | tr -d ' ' 2>/dev/null || echo '0')"

  if [[ -z "$changed_files" && "$untracked_count" == "0" ]]; then
    log "No changes detected — nothing to commit"
    return 2   # no-work: caller preserves last_run_ts and backoff state
  fi

  if [[ -z "$changed_files" && "$untracked_count" != "0" ]]; then
    log "Only untracked files present — warning but not committing"
    warn_untracked "$untracked_count"
    return 2   # no-work: caller preserves last_run_ts and backoff state
  fi

  # ── Switch to feature branch FIRST, then stage ─────────────────────────────
  # Reorder: checkout the target branch BEFORE staging, so staged changes land
  # on the feature branch (not main). Staging on main before checkout is
  # architecturally risky — if checkout fails the staged work would be lost.
  cd "$REPO" || return 1

  log "Switching to branch $branch..."
  # Pull latest remote history so 'git checkout -b' creates a local tracking branch
  # (not a diverged copy that would cause a non-fast-forward push error later).
  git fetch origin "$branch" 2>/dev/null || true
  if git rev-parse --verify "$branch" >/dev/null 2>&1; then
    git checkout "$branch" || {
      log "ERROR: git checkout $branch failed — aborting to avoid committing on wrong branch"
      return 1
    }
  else
    # No local branch — use remote tracking branch if available to avoid divergence.
    # If origin/$branch doesn't exist, git checkout -b creates from HEAD (safe default).
    git checkout -b "$branch" --track "origin/$branch" 2>/dev/null || \
      git checkout -b "$branch" || {
        log "ERROR: git checkout -b $branch failed"
        return 1
      }
  fi

  # ── Stage tracked files only ───────────────────────────────────────────────
  log "Staging tracked files..."
  # Guard: if user has pre-existing staged changes, skip to avoid destroying them
  if [[ -n "$(git diff --cached --name-only 2>/dev/null)" ]]; then
    log "SKIP: index has pre-existing staged changes — not overwriting with git add -u"
    log "Stage your changes manually or reset the index before re-running"
    return 2   # no-work: main will not update last_run_ts → retries on next interval
  fi
  # git add -u stages ALL updated tracked files (never touches untracked).
  # Capture output to detect errors; fail closed if staging goes wrong.
  if ! git add -u; then
    log "ERROR: git add -u failed — aborting"
    return 1
  fi

  # Verify nothing untracked slipped in (git add -u cannot add untracked, but guard)
  local untracked_count
  untracked_count="$(git ls-files --others --exclude-standard 2>/dev/null | wc -l | tr -d ' ' || echo '0')"
  if [[ "$untracked_count" != "0" ]]; then
    log "WARN: untracked files present — warning"
    warn_untracked "$untracked_count"
  fi

  # Commit with descriptive message
  local file_count changed_count
  file_count="$(echo "$changed_files" | wc -l | tr -d ' ' || echo '?')"
  changed_count="$(git diff --cached --stat --short 2>/dev/null | tail -1 || echo "$file_count files")"

  commit_msg="[Auto] Pending changes committed $(date '+%Y-%m-%d %H:%M')"

  if git diff --cached --quiet 2>/dev/null; then
    log "No staged changes to commit — up to date"
    return 2   # no-new-work: backoff state is preserved
  fi

  log "Committing: $commit_msg"
  log "Files: $changed_count"
  local commit_output
  commit_output="$(git commit -m "$commit_msg" 2>&1)" || {
    log "ERROR: git commit failed: $(echo "$commit_output" | tail -3)"
    return 1
  }

  # ── Push branch ─────────────────────────────────────────────────────────────
  log "Pushing branch $branch..."
  git push -u origin "$branch" 2>/dev/null || {
    log "ERROR: git push failed"
    return 1
  }

  # ── Open/update PR ──────────────────────────────────────────────────────────
  pr_url="$(open_pr_url)"
  pr_num="$(open_pr_number)"

  if [[ -n "$pr_url" ]]; then
    log "PR already exists: $pr_url — adding comment"
    gh pr comment "$pr_num" \
      --body "Auto-commit triggered: staged and committed $(git log -1 --format='%H (%ci)').

Files changed: $file_count" \
      2>/dev/null || true
  else
    log "Creating new PR..."
    local pr_body pr_output
    pr_body="## Auto-Commit: Pending Changes

Automated PR for uncommitted tracked changes in \`~/.smartclaw/\`.

**Safety rules:**
- Only git-tracked files are committed — untracked files are never auto-added
- Untracked files trigger a Slack warning instead

**This run:**
- Changed files: $file_count
- Commit: $(git log -1 --format='%H %s')

_This PR is auto-created by \`commit-pending-changes.sh\` (launchd, every 30 min)._"

    # Capture gh pr create output — URL appears on its own line in stdout on success.
    # Fall back to empty string if gh fails or the URL cannot be parsed.
    pr_output="$(gh pr create \
      --title "$PR_TITLE_PREFIX Pending changes $(date '+%Y-%m-%d %H:%M')" \
      --body "$pr_body" \
      --base main 2>&1)" || pr_output=""

    pr_url="$(echo "$pr_output" | grep -E '^https?://' | head -1 || echo '')"

    # Fallback: try JSON output if available (gh ≥2.4 supports --json url)
    if [[ -z "$pr_url" ]]; then
      pr_url="$(echo "$pr_output" | jq -r '.url' 2>/dev/null || echo '')"
    fi

    # Fallback: try gh pr view on the branch
    if [[ -z "$pr_url" || "$pr_url" == "null" ]]; then
      pr_url="$(gh pr view "$branch" --json url --jq '.url' 2>/dev/null || echo '')"
    fi

    # Last-resort fallback: search for PRs with this head branch
    if [[ -z "$pr_url" || "$pr_url" == "null" ]]; then
      pr_url="$(gh pr list --head "$branch" --json url --jq '.[0].url' 2>/dev/null || echo '')"
    fi
  fi

  if [[ -n "$pr_url" && "$pr_url" != "null" ]]; then
    log "PR ready: $pr_url"
    slack_post "Auto-commit PR updated: $pr_url — $file_count file(s) changed" || log "WARN: Slack notification failed"
  else
    log "WARN: could not determine PR URL"
  fi

  # Leave working tree on $branch so user can inspect commits; no checkout to main
  # which avoids any TOCTOU data-loss risk on user's active working tree.
  log "Done — $file_count file(s) committed on $branch"
  return 0
}

# ── Overlap lock ──────────────────────────────────────────────────────────────
acquire_lock() {
  if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    log "SKIP: another instance running"
    exit 0
  fi
  trap 'rmdir "$LOCK_DIR" >/dev/null 2>&1 || true' EXIT
  # Hold the lock briefly so concurrent-test can observe it
  if [[ "${CPC_TEST_HOLD_LOCK:-0}" == "1" ]]; then
    sleep 10
  fi
}

# ── Main ─────────────────────────────────────────────────────────────────────

acquire_lock
log "Starting commit-pending-changes (interval=${RUN_INTERVAL_SECS}s)"

# Idempotency guard — skip if ran recently
if was_run_recently; then
  log "SKIP: ran recently (within ${RUN_INTERVAL_SECS}s)"
  exit 0
fi

cd "$REPO" || { log "ERROR: cannot cd to $REPO"; exit 1; }

# Verify git repo
if ! git rev-parse --git-dir >/dev/null 2>&1; then
  log "ERROR: $REPO is not a git repo"
  exit 1
fi

# Verify gh is authenticated (skip in test environments via CPC_SKIP_GH_AUTH=1)
if [[ "${CPC_SKIP_GH_AUTH:-0}" != "1" ]] && ! gh auth status >/dev/null 2>&1; then
  log "ERROR: gh CLI is not authenticated — run 'gh auth login' first"
  exit 1
fi

# Ensure git identity
git config user.email "$GIT_EMAIL" 2>/dev/null || true
git config user.name "$GIT_NAME" 2>/dev/null || true

# Ensure on main or $PR_BRANCH (skip only if on an unrelated branch to avoid data loss)
current_branch="$(git symbolic-ref --short HEAD 2>/dev/null || true)"
if [[ -n "$current_branch" && "$current_branch" != "main" && "$current_branch" != "$PR_BRANCH" ]]; then
  log "SKIP: on branch '$current_branch' — refusing to switch to avoid data loss"
  exit 1
fi

# Execute commit + PR
# Return codes: 0=success with tracked work, 1=failure, 2=no-work (skip/idempotent)
# Temporarily disable errexit so we can capture the exit code instead of exiting immediately.
set +e
do_commit_and_pr
result=$?
set -e

if [[ "$result" == "1" ]]; then
  log "ERROR: do_commit_and_pr failed — running AO fallback"
  run_ao_fallback "commit-pending-changes: do_commit_and_pr failed in $REPO"
  slack_post "❌ commit-pending-changes failed — AO fallback triggered. Check logs: $COMMIT_LOG"
  exit 1
fi

if [[ "$result" == "0" ]]; then
  # Only update last_run_ts on successful tracked-work commit.
  # For early-return/no-work cases (code 2), we intentionally do NOT update
  # last_run_ts — this allows the next scheduled run to retry immediately
  # (was_run_recently will gate it if within interval, but otherwise it runs).
  # Reset backoff counter so untracked-only runs start fresh next time.
  record_run true
fi

# Exit explicitly: no-work (code 2) and success (code 0) both exit 0 from here.
exit 0

log "Done — commit-pending-changes complete"
