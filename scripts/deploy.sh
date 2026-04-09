#!/usr/bin/env bash
# deploy.sh — Deploy openclaw changes to production via staging canary gate.
#
# Architecture:
#   ~/.openclaw/      = STAGING (the repo checkout, port 18810)
#   ~/.openclaw_prod/ = PRODUCTION (separate dir, port 18789, symlinks to shared resources)
#
# Flow:
#   1. Validate staging gateway (port 18810) with canary + monitor-agent (OPENCLAW_STATE_DIR
#      from ai.openclaw.staging.plist via OPENCLAW_MONITOR_GATEWAY_PLIST_PATH)
#   2. Push current branch to origin/main (if needed) — BLOCKED if monitor fails
#   3. Sync validated config from staging → prod dir (openclaw.json, cron/, scripts/,
#      lib/, run-scheduled-job.sh, workspace/, memory/, launchd/, symlinks)
#   4. Restart prod gateway (port 18789) and run canary + monitor-agent (plist:
#      ai.openclaw.gateway.plist → ~/.openclaw_prod/)
#
# Blocking: If monitor-agent exits non-zero at any stage, deploy halts before
# pushing to origin/main, and alerts are sent to Slack + email.
#
# Usage:
#   ./scripts/deploy.sh              # full deploy
#   ./scripts/deploy.sh --skip-push  # skip git push (already pushed)
#   ./scripts/deploy.sh --prod-only  # skip staging, deploy to prod only
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
STAGING_DIR="$HOME/.openclaw"
PROD_DIR="$HOME/.openclaw_prod"
STAGING_PORT="${OPENCLAW_STAGING_PORT:-18810}"
PROD_PORT="${OPENCLAW_PROD_PORT:-18789}"
MONITOR_FAILURE_SLACK_TARGET="${OPENCLAW_DEPLOY_MONITOR_FAILURE_SLACK_TARGET:-${OPENCLAW_MONITOR_FAILURE_SLACK_TARGET:-C0AKYEY48GM}}"
GATEWAY_START_TIMEOUT_SECONDS="${OPENCLAW_DEPLOY_GATEWAY_START_TIMEOUT_SECONDS:-150}"
GATEWAY_START_POLL_SECONDS="${OPENCLAW_DEPLOY_GATEWAY_START_POLL_SECONDS:-5}"
CANARY_MAX_ATTEMPTS="${OPENCLAW_DEPLOY_CANARY_MAX_ATTEMPTS:-3}"
CANARY_RETRY_COOLDOWN_SECONDS="${OPENCLAW_DEPLOY_CANARY_RETRY_COOLDOWN_SECONDS:-15}"
DEPLOY_RUN_ID="$(date +%Y%m%d%H%M%S)-$$"
STAGING_CANARY_LOG="/tmp/staging-canary-${DEPLOY_RUN_ID}.log"
PROD_CANARY_LOG="/tmp/prod-canary-${DEPLOY_RUN_ID}.log"
STAGING_MONITOR_LOG="/tmp/staging-monitor-${DEPLOY_RUN_ID}.log"
STAGING_MONITOR_STDOUT="/tmp/staging-monitor-${DEPLOY_RUN_ID}.stdout"
STAGING_MONITOR_LOCK="/tmp/staging-monitor-${DEPLOY_RUN_ID}.lock"
PROD_MONITOR_LOG="/tmp/prod-monitor-${DEPLOY_RUN_ID}.log"
PROD_MONITOR_STDOUT="/tmp/prod-monitor-${DEPLOY_RUN_ID}.stdout"
PROD_MONITOR_LOCK="/tmp/prod-monitor-${DEPLOY_RUN_ID}.lock"
SKIP_PUSH=0
PROD_ONLY=0

for arg in "$@"; do
  case "$arg" in
    --skip-push) SKIP_PUSH=1 ;;
    --prod-only) PROD_ONLY=1 ;;
    -h|--help) echo "Usage: $0 [--skip-push] [--prod-only]"; exit 0 ;;
    *) echo "Unknown arg: $arg"; exit 1 ;;
  esac
done

ts() { date '+%Y-%m-%d %H:%M:%S'; }
die() {
  local msg="$1"
  local stage="${2:-}"
  echo "DEPLOY FAILED: $msg" >&2

  # Send alerts before exiting — capture stage context for recommendation
  local alert_subject="[OpenClaw Deploy] Stage failed: $msg"
  local alert_body="Deploy aborted at stage: $stage

Reason: $msg

Time: $(ts)
Branch: $(git branch --show-current 2>/dev/null || echo 'unknown')
Commit: $(git log --oneline -1 2>/dev/null || echo 'unknown')

Monitor-agent recommendations:
Check ~/.openclaw/logs/monitor-agent.log for full probe details.
Review recent canary output at /tmp/staging-canary.log and /tmp/prod-canary.log if available.
Run 'bash ~/.openclaw/monitor-agent.sh' manually to re-probe and get actionable fixes.

Next steps:
1. Fix the reported issue
2. Re-run deploy.sh
3. If the issue persists, check staging gateway health: curl http://127.0.0.1:${STAGING_PORT}/health"

  # Alert to Slack (primary failure channel via monitor-agent env, or use default)
  # Default: #all-jleechan-ai (C09GRLXF9GR) — all-hands channel.
  local slack_target="${MONITOR_FAILURE_SLACK_TARGET}"
  local slack_msg="[DEPLOY FAILED] Stage: $stage | Reason: $msg | Branch: $(git branch --show-current 2>/dev/null || echo 'unknown') | Time: $(ts)"
  if command -v openclaw >/dev/null 2>&1; then
    env -u OPENCLAW_GATEWAY_TOKEN -u OPENCLAW_GATEWAY_REMOTE_TOKEN \
      OPENCLAW_STATE_DIR="$PROD_DIR" \
      OPENCLAW_CONFIG_PATH="$PROD_DIR/openclaw.json" \
      openclaw message send --channel slack --target "$slack_target" --message "$slack_msg" 2>/dev/null || true
  fi

  # Alert to email
  "$SCRIPT_DIR/send-alert-email.sh" "$alert_subject" "$alert_body" 2>/dev/null || true

  exit 1
}

send_deploy_success_alert() {
  local stage="$1"
  local port="$2"
  local alert_subject="[OpenClaw Deploy] Success: $stage passed"
  local alert_body="Deploy $stage validation passed.

Time: $(ts)
Stage: $stage (port $port)
Branch: $(git branch --show-current 2>/dev/null || echo 'unknown')
Commit: $(git log --oneline -1 2>/dev/null || echo 'unknown')
Gateway health: $(curl -sf --max-time 5 "http://127.0.0.1:${port}/health" 2>/dev/null || echo 'unreachable')"

  local slack_target="${OPENCLAW_MONITOR_SLACK_TARGET:-C0AP8LRKM9N}"
  if command -v openclaw >/dev/null 2>&1; then
    env -u OPENCLAW_GATEWAY_TOKEN -u OPENCLAW_GATEWAY_REMOTE_TOKEN \
      OPENCLAW_STATE_DIR="$PROD_DIR" \
      OPENCLAW_CONFIG_PATH="$PROD_DIR/openclaw.json" \
      openclaw message send --channel slack --target "$slack_target" --message "$alert_subject" 2>/dev/null || true
  fi
  "$SCRIPT_DIR/send-alert-email.sh" "$alert_subject" "$alert_body" 2>/dev/null || true
}

extract_monitor_status() {
  local log_path="$1"
  [[ -f "$log_path" ]] || return 1
  awk '/^STATUS=/{status=$0} END{if (status!="") print status}' "$log_path" \
    | sed 's/^STATUS=//'
}

assert_monitor_status_good() {
  local stage="$1"
  local log_path="$2"
  local status=""
  local detail=""

  status="$(extract_monitor_status "$log_path" || true)"
  if [[ "$status" == "GOOD" ]]; then
    echo "Monitor status gate passed: STATUS=GOOD"
    return 0
  fi

  if [[ -f "$log_path" ]]; then
    detail="$(
      { grep -E '^STATUS=|^ACTIVE PROBLEMS:|^[[:space:]]*• ' "$log_path" 2>/dev/null || true; } \
      | tail -n 20 \
      | tr '\n' ' ' \
      | sed 's/[[:space:]]\+/ /g' \
      | cut -c1-280
    )"
  fi
  die "Monitor reported STATUS=${status:-unknown}${detail:+ ($detail)} — see $log_path" "$stage"
}

section() { echo ""; echo "=== $1 ==="; echo "$(ts)"; echo ""; }

is_stub_main_config() {
  python3 - "$1" <<'PY'
import json
import sys

with open(sys.argv[1]) as fh:
    cfg = json.load(fh)

slack = cfg.get("channels", {}).get("slack", {}) or {}
required = [
    cfg.get("gateway", {}).get("auth", {}).get("token"),
    cfg.get("meta", {}).get("lastTouchedVersion"),
    cfg.get("agents", {}).get("defaults", {}).get("workspace"),
    cfg.get("plugins", {}).get("entries"),
]

missing = any(not item for item in required)
if slack.get("enabled") is True and not (slack.get("botToken") and slack.get("appToken")):
    missing = True

sys.exit(0 if missing else 1)
PY
}

# Canary can occasionally flake with heartbeat curl timeout (e.g. exit 28) while the
# gateway is still busy. Retry with recovery + cooldown using bounded attempts.
post_monitor_canary_with_retry() {
  local port="$1"
  local log="$2"
  local prod_config="${3:-0}"
  local attempt=1
  local max_attempts="${CANARY_MAX_ATTEMPTS}"
  local cooldown="${CANARY_RETRY_COOLDOWN_SECONDS}"
  local run_canary
  run_canary() {
    if [[ "$prod_config" -eq 1 ]]; then
      OPENCLAW_STAGING_CONFIG="$PROD_DIR/openclaw.json" \
        bash "$SCRIPT_DIR/staging-canary.sh" --port "$port" >> "$log" 2>&1
    else
      bash "$SCRIPT_DIR/staging-canary.sh" --port "$port" >> "$log" 2>&1
    fi
  }
  while (( attempt <= max_attempts )); do
    if run_canary; then
      return 0
    fi
    if (( attempt == max_attempts )); then
      break
    fi
    echo "  Canary attempt ${attempt}/${max_attempts} failed — attempting gateway recovery..."
    ensure_gateway_up_for_port "$port" 1 || true
    echo "  Retrying canary after ${cooldown}s cooldown..."
    sleep "$cooldown"
    attempt=$(( attempt + 1 ))
  done
  return 1
}

ensure_gateway_up_for_port() {
  local port="$1"
  local require_label="${2:-0}"
  local label=""
  local plist=""
  local domain="gui/$(id -u)"
  local started_at="$(date +%s)"
  local now elapsed
  local listener_pids=""
  local parent_pid=""
  local parent_comm=""
  if [[ "$port" == "$STAGING_PORT" ]]; then
    label="ai.openclaw.staging"
    plist="$HOME/Library/LaunchAgents/ai.openclaw.staging.plist"
  elif [[ "$port" == "$PROD_PORT" ]]; then
    label="ai.openclaw.gateway"
    plist="$HOME/Library/LaunchAgents/ai.openclaw.gateway.plist"
  else
    return 1
  fi
  if curl -sf --max-time 8 "http://127.0.0.1:${port}/health" >/dev/null 2>&1; then
    if [[ "$require_label" -eq 0 ]] || launchctl print "${domain}/${label}" >/dev/null 2>&1; then
      return 0
    fi
  fi

  if [[ "$require_label" -eq 1 ]] && ! launchctl print "${domain}/${label}" >/dev/null 2>&1; then
    listener_pids="$(lsof -nP -iTCP:${port} -sTCP:LISTEN -t 2>/dev/null | sort -u || true)"
    if [[ -n "$listener_pids" ]]; then
      while read -r pid; do
        [[ -n "$pid" ]] || continue
        kill -TERM "$pid" 2>/dev/null || true
        parent_pid="$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ' || true)"
        if [[ "$parent_pid" =~ ^[0-9]+$ ]]; then
          parent_comm="$(ps -o comm= -p "$parent_pid" 2>/dev/null | tr -d ' ' || true)"
          if [[ "$parent_comm" == "openclaw" ]]; then
            kill -TERM "$parent_pid" 2>/dev/null || true
          fi
        fi
      done <<< "$listener_pids"
      sleep 2
    fi
  fi

  launchctl enable "${domain}/${label}" >/dev/null 2>&1 || true
  if launchctl print "${domain}/${label}" >/dev/null 2>&1; then
    launchctl kickstart -k "${domain}/${label}" >/dev/null 2>&1 || true
  else
    launchctl bootstrap "$domain" "$plist" >/dev/null 2>&1 || \
      launchctl kickstart -k "${domain}/${label}" >/dev/null 2>&1 || true
  fi

  while true; do
    if curl -sf --max-time 8 "http://127.0.0.1:${port}/health" >/dev/null 2>&1; then
      if [[ "$require_label" -eq 0 ]] || launchctl print "${domain}/${label}" >/dev/null 2>&1; then
        return 0
      fi
    fi
    now="$(date +%s)"
    elapsed=$(( now - started_at ))
    if (( elapsed >= GATEWAY_START_TIMEOUT_SECONDS )); then
      break
    fi
    sleep "$GATEWAY_START_POLL_SECONDS"
  done

  # One last nudge before declaring hard failure (handles rare launchd races).
  launchctl kickstart -k "${domain}/${label}" >/dev/null 2>&1 || true
  started_at="$(date +%s)"
  while true; do
    if curl -sf --max-time 8 "http://127.0.0.1:${port}/health" >/dev/null 2>&1; then
      if [[ "$require_label" -eq 0 ]] || launchctl print "${domain}/${label}" >/dev/null 2>&1; then
        return 0
      fi
    fi
    now="$(date +%s)"
    elapsed=$(( now - started_at ))
    if (( elapsed >= 45 )); then
      break
    fi
    sleep "$GATEWAY_START_POLL_SECONDS"
  done
  return 1
}

# ── Preflight ──────────────────────────────────────────────────────────────

section "Preflight"

cd "$REPO_DIR"
BRANCH="$(git branch --show-current)"
REMOTE="$(git remote get-url origin)"
echo "Branch:      $BRANCH"
echo "Remote:      $REMOTE"
echo "Staging dir: $STAGING_DIR"
echo "Prod dir:    $PROD_DIR"

if [[ "$REMOTE" != *"jleechanclaw"* ]]; then
  die "origin does not point to jleechanclaw: $REMOTE" "Preflight"
fi

if [[ ! -d "$PROD_DIR" ]]; then
  die "Prod directory does not exist: $PROD_DIR (run scripts/install.sh first)" "Preflight"
fi

echo ""
echo "Running gateway preflight..."
bash "$SCRIPT_DIR/gateway-preflight.sh" || die "gateway-preflight.sh failed" "Preflight"

# ── Stage 1: Staging validation ────────────────────────────────────────────

if [[ "$PROD_ONLY" -eq 0 ]]; then
  section "Stage 1: Staging Gateway Validation (port $STAGING_PORT)"

  STAGING_HEALTH=$(curl -sf --max-time 8 "http://127.0.0.1:${STAGING_PORT}/health" 2>&1 || echo "")
  if [[ -z "$STAGING_HEALTH" ]]; then
    echo "Staging gateway not responding — recovering launchd job..."
    ensure_gateway_up_for_port "$STAGING_PORT" 1 || true
    STAGING_HEALTH=$(curl -sf --max-time 8 "http://127.0.0.1:${STAGING_PORT}/health" 2>&1 || echo "")
    [[ -n "$STAGING_HEALTH" ]] || die "Staging gateway failed to start on port $STAGING_PORT" "Stage 1: Gateway Start"
  fi
  echo "Staging gateway healthy: $STAGING_HEALTH"

  echo ""
  echo "Running staging canary..."
  post_monitor_canary_with_retry "$STAGING_PORT" "$STAGING_CANARY_LOG" 0 \
    || die "Staging canary FAILED — see $STAGING_CANARY_LOG" "Stage 1: Canary"

  echo ""
  echo "Running monitor-agent against staging..."
  # Run monitor against staging while overriding its gateway health target.
  # Stage 4 performs the production monitor gate separately.
  env -u OPENCLAW_GATEWAY_TOKEN -u OPENCLAW_GATEWAY_REMOTE_TOKEN \
    OPENCLAW_MONITOR_HTTP_GATEWAY_URL="http://127.0.0.1:${STAGING_PORT}/health" \
    OPENCLAW_STATE_DIR="$PROD_DIR" \
    OPENCLAW_CONFIG_PATH="$PROD_DIR/openclaw.json" \
    OPENCLAW_MONITOR_GATEWAY_PLIST_PATH="$HOME/Library/LaunchAgents/ai.openclaw.staging.plist" \
    OPENCLAW_MONITOR_LOG_FILE="$STAGING_MONITOR_LOG" \
    OPENCLAW_MONITOR_LOCK_DIR="$STAGING_MONITOR_LOCK" \
    OPENCLAW_MONITOR_SLACK_TARGET="" \
    OPENCLAW_MONITOR_FAILURE_SLACK_TARGET="$MONITOR_FAILURE_SLACK_TARGET" \
    OPENCLAW_MONITOR_SLACK_READ_PROBE_ENABLE=0 \
    OPENCLAW_MONITOR_GATEWAY_PROBE_MESSAGE_ENABLE=0 \
    OPENCLAW_MONITOR_THREAD_REPLY_CHECK=0 \
    OPENCLAW_MONITOR_FAIL_CLOSED_CONFIG_SIGNATURES_ENABLE=0 \
    OPENCLAW_MONITOR_TOKEN_PROBES_ENABLE=0 \
    OPENCLAW_MONITOR_MEMORY_LOOKUP_ENABLE=0 \
    OPENCLAW_MONITOR_DOCTOR_SH_ENABLE=0 \
    OPENCLAW_MONITOR_INFERENCE_PROBE_ENABLE=0 \
    OPENCLAW_MONITOR_PHASE2_ENABLE=0 \
    OPENCLAW_MONITOR_RUN_CANARY=0 \
    bash "$HOME/.openclaw/monitor-agent.sh" > "$STAGING_MONITOR_STDOUT" 2>&1 \
    || die "Monitor-agent FAILED on staging — see $STAGING_MONITOR_LOG and $STAGING_MONITOR_STDOUT" "Stage 1: Monitor"
  assert_monitor_status_good "Stage 1: Monitor" "$STAGING_MONITOR_LOG"

  # Re-run canary as E2E confirmation post-monitor
  post_monitor_canary_with_retry "$STAGING_PORT" "$STAGING_CANARY_LOG" 0 \
    || die "Post-monitor canary FAILED — see $STAGING_CANARY_LOG" "Stage 1: Canary (re-check)"

  echo ""
  echo "STAGING PASSED — all checks green on port $STAGING_PORT"
fi

# ── Stage 2: Push to origin/main ──────────────────────────────────────────

section "Stage 2: Push to Origin"

if [[ "$SKIP_PUSH" -eq 0 ]]; then
  if [[ "$BRANCH" != "main" ]]; then
    echo "Merging $BRANCH into main..."
    git checkout main
    git pull origin main
    git merge "$BRANCH" --no-edit || die "Merge conflict — resolve manually" "Stage 2: Merge"
    git push origin main || die "Push to origin/main failed" "Stage 2: Push"
    echo "Pushed to origin/main"
  else
    echo "Already on main — pulling latest..."
    git pull origin main || die "Pull failed" "Stage 2: Pull"
    AHEAD=$(git rev-list origin/main..HEAD --count 2>/dev/null || echo "0")
    if [[ "$AHEAD" -gt 0 ]]; then
      echo "Pushing $AHEAD commit(s) to origin/main..."
      git push origin main || die "Push to origin/main failed" "Stage 2: Push"
    else
      echo "Already up to date with origin/main"
    fi
  fi
else
  echo "Skipping push (--skip-push)"
fi

# ── Stage 3: Sync config to prod ─────────────────────────────────────────

section "Stage 3: Sync Config to Production"

echo "Syncing validated config from staging → prod..."

# Copy the main config only when staging has a full runtime config.
# The repo checkout may intentionally hold a redacted stub while production keeps
# the secretful live config in ~/.openclaw_prod/openclaw.json.
if is_stub_main_config "$STAGING_DIR/openclaw.json"; then
  if [[ -f "$PROD_DIR/openclaw.json" ]]; then
    echo "  WARN: staging openclaw.json is an incomplete repo stub; preserving existing prod openclaw.json"
  else
    die "Staging openclaw.json is incomplete and prod openclaw.json is missing" "Stage 3: Config Sync"
  fi
else
  cp "$STAGING_DIR/openclaw.json" "$PROD_DIR/openclaw.json"
  echo "  openclaw.json synced"
fi

# Copy cron jobs — these are the gateway's scheduled job definitions
if [[ -f "$STAGING_DIR/cron/jobs.json" ]]; then
  mkdir -p "$PROD_DIR/cron"
  cp "$STAGING_DIR/cron/jobs.json" "$PROD_DIR/cron/jobs.json"
  echo "  cron/jobs.json synced"
fi

# Sync scripts/ — entire directory of operational scripts
if [[ -d "$STAGING_DIR/scripts" ]]; then
  rsync -av --delete \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude '*.pyo' \
    --exclude '.git' \
    "$STAGING_DIR/scripts/" "$PROD_DIR/scripts/"
  echo "  scripts/ synced"
fi

# Sync lib/ — shared shell libraries (token-probes.sh, core-md-probe.sh, etc.).
# doctor.sh sources $LIVE_OPENCLAW/lib/token-probes.sh when LIVE_OPENCLAW is prod
# (~/.openclaw_prod via OPENCLAW_STATE_DIR); deploy does not mirror the full repo,
# so prod must receive lib/ explicitly.
if [[ -d "$STAGING_DIR/lib" ]]; then
  mkdir -p "$PROD_DIR/lib"
  rsync -av --delete \
    --exclude '.git' \
    "$STAGING_DIR/lib/" "$PROD_DIR/lib/"
  echo "  lib/ synced"
fi

# Repo-root gateway cron helper — doctor.sh require_file(run-scheduled-job.sh) on the live tree.
if [[ -f "$STAGING_DIR/run-scheduled-job.sh" ]]; then
  cp -p "$STAGING_DIR/run-scheduled-job.sh" "$PROD_DIR/run-scheduled-job.sh"
  chmod +x "$PROD_DIR/run-scheduled-job.sh"
  echo "  run-scheduled-job.sh synced"
fi

# Monitor agent — runs health checks and canary probes against the live gateway.
if [[ -f "$STAGING_DIR/monitor-agent.sh" ]]; then
  cp -p "$STAGING_DIR/monitor-agent.sh" "$PROD_DIR/monitor-agent.sh"
  chmod +x "$PROD_DIR/monitor-agent.sh"
  echo "  monitor-agent.sh synced"
fi

# Sync workspace/ — all workspace content except runtime artifacts
# (excludes __pycache__, tmp, temp, sqlite dbs, claude-memory-context)
if [[ -d "$STAGING_DIR/workspace" ]]; then
  rsync -av --delete \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude 'tmp*' \
    --exclude 'temp*' \
    --exclude '*.sqlite' \
    --exclude '*.sqlite.backup-*' \
    --exclude '*.sqlite.tmp-*' \
    --exclude 'claude-memory-context.md' \
    --exclude '.git' \
    "$STAGING_DIR/workspace/" "$PROD_DIR/workspace/"
  echo "  workspace/ synced"
fi

# Sync memory/ — daily notes (not sqlite runtime state)
if [[ -d "$STAGING_DIR/memory" ]]; then
  rsync -av --delete \
    --exclude '*.sqlite' \
    --exclude '*.sqlite.backup-*' \
    --exclude '*.sqlite.tmp-*' \
    --exclude 'extraction-state.lock' \
    "$STAGING_DIR/memory/" "$PROD_DIR/memory/"
  echo "  memory/ synced"
fi

# Ensure symlinks are current for shared resources
for target in SOUL.md TOOLS.md HEARTBEAT.md extensions agents credentials lcm.db skills; do
  src="$STAGING_DIR/$target"
  dst="$PROD_DIR/$target"
  if [[ -e "$src" ]] && [[ ! -L "$dst" ]]; then
    ln -sf "$src" "$dst"
    echo "  symlinked $target"
  fi
done

echo "Config sync complete"

# Seed auth-profiles.json — required for agent to authenticate with LLM providers.
# This file is NOT synced by rsync (agents/ is symlinked, not copied).
# Missing auth-profiles in prod causes silent Slack failure: HTTP 200 liveness
# but every message fails with "No API key found for provider anthropic".
PROD_AUTH="$PROD_DIR/agents/main/agent/auth-profiles.json"
STAGING_AUTH="$STAGING_DIR/agents/main/agent/auth-profiles.json"
if [[ ! -f "$PROD_AUTH" ]]; then
  if [[ -f "$STAGING_AUTH" ]]; then
    mkdir -p "$(dirname "$PROD_AUTH")"
    cp "$STAGING_AUTH" "$PROD_AUTH"
    echo "  Seeded auth-profiles.json into prod state dir (was missing)"
  else
    die "auth-profiles.json missing from both staging ($STAGING_AUTH) and prod ($PROD_AUTH) — agent cannot authenticate" "Stage 3: Auth Profiles"
  fi
else
  echo "  auth-profiles.json present in prod"
fi

# ── Stage 3.5: Sync launchd plist templates + run gateway migration ────────
# This handles the critical plist-label migration (ai.openclaw.gateway → com.openclaw.gateway).
# install-launchagents.sh reads plist templates from REPO_DIR/launchd/, so we pass REPO_DIR=STAGING_DIR
# to ensure templates are resolved from the staging repo (which has the canonical plist files),
# while the script itself runs from the prod scripts/ dir (where deploy.sh placed it).
section "Stage 3.5: Sync launchd plists + run install-launchagents.sh"

if [[ -d "$STAGING_DIR/launchd" ]]; then
  mkdir -p "$PROD_DIR/launchd"
  rsync -av \
    --exclude '*.pyc' \
    --exclude '.git' \
    "$STAGING_DIR/launchd/" "$PROD_DIR/launchd/"
  echo "  launchd/ synced"
else
  echo "  WARNING: no launchd/ directory in staging — skipping plist sync"
fi

# Run install-launchagents.sh from staging so REPO_DIR resolves correctly.
# (install-launchagents.sh computes REPO_DIR=$(dirname $0)/.. at runtime;
# running from staging ensures it points to the staging repo which has canonical plist templates.)
# The script's install_plist function launches services via launchctl bootstrap using the
# absolute paths we've already synced to PROD_DIR, so the services start correctly on prod.
# This step is idempotent — safe to re-run on every deploy.
bash "$STAGING_DIR/scripts/install-launchagents.sh" > /tmp/install-launchagents.log 2>&1 \
  || echo "  install-launchagents.sh exited $? — see /tmp/install-launchagents.log"
echo "  install-launchagents.sh complete"

# ── Stage 4: Production gateway restart + validation ──────────────────────

section "Stage 4: Production Gateway Validation (port $PROD_PORT)"

echo "Restarting production gateway..."
launchctl stop "gui/$(id -u)/ai.openclaw.gateway" 2>/dev/null || true
launchctl bootout "gui/$(id -u)/ai.openclaw.gateway" 2>/dev/null || true
sleep 3

# Kill orphaned openclaw-gateway processes that are NOT part of the staging or prod gateways.
# IMPORTANT: the LISTEN pid from lsof is not always the same as the `openclaw-gateway`
# worker pid (parent may be `node` / wrapper). Comparing pgrep to listener PIDs alone
# could kill the worker for port $STAGING_PORT while leaving the parent — SIGTERM/SIGKILL
# to the gateway and failed deploys. Protect any PID in the ancestor chain of a process
# that has TCP state on $STAGING_PORT or $PROD_PORT.
_gateway_port_protected_pids() {
  {
    lsof -nP -iTCP:"${STAGING_PORT}" -t 2>/dev/null || true
    lsof -nP -iTCP:"${PROD_PORT}" -t 2>/dev/null || true
  } | sort -u
}
_pid_has_protected_ancestor() {
  local pid="$1"
  local prot_file="$2"
  local walk="$pid"
  local pp
  [[ -s "$prot_file" ]] || return 1
  for _ in $(seq 1 64); do
    if grep -qx "$walk" "$prot_file" 2>/dev/null; then
      return 0
    fi
    pp=$(ps -o ppid= -p "$walk" 2>/dev/null | tr -d ' ')
    [[ "$pp" =~ ^[0-9]+$ ]] || break
    [[ "$pp" -le 1 ]] && break
    walk="$pp"
  done
  return 1
}
_deploy_prot_file="$(mktemp "${TMPDIR:-/tmp}/deploy-prot.XXXXXX")"
_gateway_port_protected_pids >"$_deploy_prot_file"
_orphan_pids=""
while read -r _gwpid; do
  [[ -n "$_gwpid" ]] || continue
  if _pid_has_protected_ancestor "$_gwpid" "$_deploy_prot_file"; then
    continue
  fi
  _orphan_pids="${_orphan_pids}${_gwpid}"$'\n'
done < <(pgrep -x openclaw-gateway 2>/dev/null)
rm -f "$_deploy_prot_file"
if [[ -n "$(echo "$_orphan_pids" | tr -d '[:space:]')" ]]; then
  echo "  Killing orphaned openclaw-gateway process(es) (not on ports ${STAGING_PORT}/${PROD_PORT} trees):"
  printf '%s\n' "$_orphan_pids" | while read -r pid; do
      [[ -z "$pid" ]] && continue
      port=$(lsof -i -P -n -a -p "$pid" 2>/dev/null | awk 'NR>1 {print $9}' | head -1 || true)
      echo "    Killing PID $pid${port:+ on $port}"
      kill -9 "$pid" 2>/dev/null || true
    done
  sleep 2
fi

# Clear stale session lock files before bringing up the new instance
_clear_stale_locks() {
  local sessions_dir="$1"
  find "$sessions_dir" -name "*.lock" 2>/dev/null | while read -r f; do
    local raw pid
    raw=$(cat "$f" 2>/dev/null)
    pid=$(echo "$raw" | python3 -c "import sys,json; print(json.load(sys.stdin)['pid'])" 2>/dev/null \
          || echo "$raw" | tr -d '[:space:]')
    if [[ "$pid" =~ ^[0-9]+$ ]] && ! kill -0 "$pid" 2>/dev/null; then
      rm -f "$f" && echo "  Removed stale lock: $f (dead pid=$pid)"
    fi
  done
}
_clear_stale_locks "$PROD_DIR/agents/main/sessions"
# Also clear staging-dir locks in case of shared inode (symlinked sessions dirs)
_clear_stale_locks "$STAGING_DIR/agents/main/sessions"

ensure_gateway_up_for_port "$PROD_PORT" 1 \
  || die "Production gateway failed to start on port $PROD_PORT under label ai.openclaw.gateway" "Stage 4: Gateway Start"
PROD_HEALTH=""
for _health_attempt in 1 2 3; do
  PROD_HEALTH="$(curl -sf --max-time 8 "http://127.0.0.1:${PROD_PORT}/health" 2>&1 || true)"
  [[ -n "$PROD_HEALTH" ]] && break
  sleep 3
done
[[ -n "$PROD_HEALTH" ]] \
  || die "Production gateway /health unavailable after startup on port $PROD_PORT" "Stage 4: Gateway Health"
echo "Production gateway healthy: $PROD_HEALTH"

# Assert exactly 1 gateway process listening on the prod port.
# Count unique PIDs (lsof shows IPv4+IPv6 as separate lines for same PID).
_running_gw="$(
  { lsof -i ":${PROD_PORT}" -sTCP:LISTEN -t 2>/dev/null || true; } \
    | sort -u | wc -l | tr -d ' '
)"
if [[ "$_running_gw" -ne 1 ]]; then
  die "Post-restart gateway instance count=$_running_gw on port $PROD_PORT (expected 1) — possible orphan conflict" "Stage 4: Single-instance check"
fi
echo "  Single-instance check: 1 openclaw-gateway process confirmed on port $PROD_PORT"

# Assert canonical label is loaded and legacy label is NOT loaded.
if ! launchctl print "gui/$(id -u)/ai.openclaw.gateway" >/dev/null 2>&1; then
  die "Canonical label ai.openclaw.gateway not loaded after restart" "Stage 4: Label assertion"
fi
if launchctl print "gui/$(id -u)/com.openclaw.gateway" >/dev/null 2>&1; then
  die "Legacy label com.openclaw.gateway is still loaded — remove duplicate plist" "Stage 4: Label assertion"
fi
echo "  Label assertion: ai.openclaw.gateway loaded, com.openclaw.gateway absent"

echo ""
echo "Running production canary..."
post_monitor_canary_with_retry "$PROD_PORT" "$PROD_CANARY_LOG" 1 \
  || die "Production canary FAILED — see $PROD_CANARY_LOG" "Stage 4: Canary"

echo ""
echo "Running monitor-agent against production (~/.openclaw_prod via gateway plist)..."
env -u OPENCLAW_GATEWAY_TOKEN -u OPENCLAW_GATEWAY_REMOTE_TOKEN \
  OPENCLAW_MONITOR_HTTP_GATEWAY_URL="http://127.0.0.1:${PROD_PORT}/health" \
  OPENCLAW_STATE_DIR="$PROD_DIR" \
  OPENCLAW_CONFIG_PATH="$PROD_DIR/openclaw.json" \
  OPENCLAW_MONITOR_GATEWAY_PLIST_PATH="$HOME/Library/LaunchAgents/ai.openclaw.gateway.plist" \
  OPENCLAW_MONITOR_LOG_FILE="$PROD_MONITOR_LOG" \
  OPENCLAW_MONITOR_LOCK_DIR="$PROD_MONITOR_LOCK" \
  OPENCLAW_MONITOR_SLACK_TARGET="" \
  OPENCLAW_MONITOR_FAILURE_SLACK_TARGET="$MONITOR_FAILURE_SLACK_TARGET" \
  OPENCLAW_MONITOR_RUN_CANARY=0 \
  bash "$HOME/.openclaw/monitor-agent.sh" > "$PROD_MONITOR_STDOUT" 2>&1 \
  || die "Monitor-agent FAILED on production — see $PROD_MONITOR_LOG and $PROD_MONITOR_STDOUT" "Stage 4: Monitor"
assert_monitor_status_good "Stage 4: Monitor" "$PROD_MONITOR_LOG"

# Re-run canary as E2E confirmation post-monitor
post_monitor_canary_with_retry "$PROD_PORT" "$PROD_CANARY_LOG" 1 \
  || die "Post-monitor canary FAILED — see $PROD_CANARY_LOG" "Stage 4: Canary (re-check)"

# ── Done ──────────────────────────────────────────────────────────────────

section "Deploy Complete"
echo "Branch:  $BRANCH"
if [[ "$PROD_ONLY" -eq 0 ]]; then
  echo "Staging: PASS (port $STAGING_PORT, dir: $STAGING_DIR)"
else
  echo "Staging: SKIPPED (--prod-only)"
fi
echo "Prod:    PASS (port $PROD_PORT, dir: $PROD_DIR)"
echo "Commit:  $(git log --oneline -1)"
echo ""
echo "$(ts) — deploy finished successfully"

# Send success notifications
if [[ "$PROD_ONLY" -eq 0 ]]; then
  send_deploy_success_alert "Staging" "$STAGING_PORT" 2>/dev/null || true
fi
send_deploy_success_alert "Production" "$PROD_PORT" 2>/dev/null || true
