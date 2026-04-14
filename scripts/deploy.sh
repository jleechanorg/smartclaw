#!/usr/bin/env bash
# deploy.sh — Hermes gateway deploy for smartclaw.
#
# Architecture:
#   Hermes (primary agent):
#     ~/.smartclaw/hermes/     = STAGING (git repo root = smartclaw checkout)
#     ~/.smartclaw/hermes_prod/ = PRODUCTION (separate runtime data)
#
# Flow (Hermes only):
#   preflight → validate-staging → sync-config → restart-prod → post-restart-validation
#
# Usage:
#   ./scripts/deploy.sh                        # deploy Hermes (default)
#   ./scripts/deploy.sh --dry-run              # preflight checks only
#   ./scripts/deploy.sh --prod-only            # skip staging validation
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Shared variables ──────────────────────────────────────────────────────────
DEPLOY_RUN_ID="$(date +%Y%m%d%H%M%S)-$$"
MONITOR_FAILURE_SLACK_TARGET="${SMARTCLAW_DEPLOY_SLACK_TARGET:-}"
SKIP_PUSH=0
PROD_ONLY=0
DRY_RUN=0
DEPLOY_SYSTEMS=(hermes)

# ── Hermes variables ──────────────────────────────────────────────────────────
HERMES_BIN="${HERMES_BIN:-hermes}"
HERMES_STAGING_HOME="${HERMES_STAGING_HOME:-$HOME/.smartclaw/hermes}"
HERMES_PROD_HOME="${HERMES_PROD_HOME:-$HOME/.smartclaw/hermes_prod}"
HERMES_PROD_LABEL="ai.smartclaw.hermes.prod"
HERMES_GATEWAY_START_TIMEOUT_SECONDS="${HERMES_GATEWAY_START_TIMEOUT_SECONDS:-90}"
HERMES_GATEWAY_START_POLL_SECONDS="${HERMES_GATEWAY_START_POLL_SECONDS:-3}"
HERMES_MONITOR_LOG="/tmp/hermes-monitor-${DEPLOY_RUN_ID}.log"

# ── Argument parsing ──────────────────────────────────────────────────────────

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dry-run)   DRY_RUN=1 ;;
      --prod-only) PROD_ONLY=1 ;;
      -h|--help)
        echo "Usage: $0 [--dry-run] [--prod-only]"
        exit 0
        ;;
      *) echo "Unknown arg: $1"; exit 1 ;;
    esac
    shift
  done
}

# ── Shared utility functions ──────────────────────────────────────────────────

ts() { date '+%Y-%m-%d %H:%M:%S'; }
section() { echo ""; echo "=== $1 ==="; echo "$(ts)"; echo ""; }

die() {
  local msg="$1"
  local stage="${2:-}"
  local system="${3:-Hermes}"
  echo "DEPLOY FAILED: $msg" >&2
  if [[ -n "$slack_target" ]] && command -v hermes >/dev/null 2>&1; then
    HERMES_HOME="$HERMES_PROD_HOME" "$HERMES_BIN" message send --target "$slack_target" --message "[DEPLOY FAILED] System: $system | Stage: $stage | Reason: $msg | Time: $(ts)" 2>/dev/null || true
  fi
  exit 1
}

assert_monitor_status_good() {
  local stage="$1"
  local log_path="$2"
  local system="${3:-Hermes}"
  local status=""
  local detail=""

  status="$(awk '/^STATUS=/{status=$0} END{if (status!="") print status}' "$log_path" 2>/dev/null | sed 's/^STATUS=//' || true)"
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
  die "Monitor reported STATUS=${status:-unknown}${detail:+ ($detail)} — see $log_path" "$stage" "$system"
}

# ══════════════════════════════════════════════════════════════════════════════
# HERMES DEPLOY PIPELINE
# ══════════════════════════════════════════════════════════════════════════════

hermes_check_env_key() {
  local env_file="$1" key="$2"
  local val
  val=$(grep -E "^${key}=" "$env_file" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'")
  [[ -n "$val" ]]
}

hermes_preflight() {
  section "Hermes Preflight"

  local fail=0

  # 1. config.yaml valid YAML
  if ! python3 -c "import yaml" 2>/dev/null; then
    echo "  FAIL: PyYAML not installed (pip install pyyaml)"
    fail=1
  elif python3 -c "import yaml; yaml.safe_load(open('$HERMES_PROD_HOME/config.yaml'))" 2>/dev/null; then
    echo "  PASS: config.yaml is valid YAML"
  else
    echo "  FAIL: config.yaml is invalid or missing"
    fail=1
  fi

  # 2. .env has required keys
  local env_file="$HERMES_PROD_HOME/.env"
  if [[ -f "$env_file" ]]; then
    local missing_keys=()
    for key in MINIMAX_API_KEY SLACK_BOT_TOKEN SLACK_APP_TOKEN HERMES_HOME; do
      if ! hermes_check_env_key "$env_file" "$key"; then
        missing_keys+=("$key")
      fi
    done
    if [[ ${#missing_keys[@]} -eq 0 ]]; then
      echo "  PASS: .env has all required keys"
    else
      echo "  FAIL: .env missing keys: ${missing_keys[*]}"
      fail=1
    fi
  else
    echo "  FAIL: .env not found at $env_file"
    fail=1
  fi

  # 3. auth.json exists and valid JSON
  local auth_file="$HERMES_PROD_HOME/auth.json"
  if [[ -f "$auth_file" ]] && python3 -c "import json; json.load(open('$auth_file'))" 2>/dev/null; then
    echo "  PASS: auth.json is valid JSON"
  else
    echo "  FAIL: auth.json missing or invalid"
    fail=1
  fi

  # 4. LaunchAgent plist loaded
  local domain="gui/$(id -u)"
  if launchctl print "${domain}/${HERMES_PROD_LABEL}" >/dev/null 2>&1; then
    echo "  PASS: LaunchAgent ${HERMES_PROD_LABEL} is loaded"
  else
    if [[ "$DRY_RUN" -eq 1 ]]; then
      echo "  SKIP: LaunchAgent bootstrap skipped (dry-run)"
    else
      echo "  WARN: LaunchAgent not loaded — attempting bootstrap..."
      local plist="$HOME/Library/LaunchAgents/${HERMES_PROD_LABEL}.plist"
      if [[ -f "$plist" ]]; then
        launchctl bootstrap "$domain" "$plist" 2>/dev/null || true
        if launchctl print "${domain}/${HERMES_PROD_LABEL}" >/dev/null 2>&1; then
          echo "  PASS: LaunchAgent bootstrapped successfully"
        else
          echo "  FAIL: LaunchAgent bootstrap failed"
          fail=1
        fi
      else
        echo "  FAIL: Plist not found at $plist"
        fail=1
      fi
    fi
  fi

  # 5. Plist HERMES_HOME matches prod
  local plist_file="$HOME/Library/LaunchAgents/${HERMES_PROD_LABEL}.plist"
  if [[ -f "$plist_file" ]]; then
    local plist_home
    plist_home=$(python3 -c "
import plistlib
with open('$plist_file', 'rb') as f:
    d = plistlib.load(f)
print(d.get('EnvironmentVariables', {}).get('HERMES_HOME', ''))
" 2>/dev/null || echo "")
    if [[ "$plist_home" == "$HERMES_PROD_HOME" ]]; then
      echo "  PASS: Plist HERMES_HOME matches prod ($HERMES_PROD_HOME)"
    else
      echo "  FAIL: Plist HERMES_HOME='$plist_home' != expected '$HERMES_PROD_HOME'"
      fail=1
    fi
  fi

  if [[ $fail -ne 0 ]]; then
    die "Hermes preflight failed — see above" "Hermes Preflight" "Hermes"
  fi
  echo ""
  echo "HERMES PREFLIGHT PASSED"
}

hermes_validate_staging() {
  section "Hermes Staging Validation"

  local gw_status
  gw_status=$(HERMES_HOME="$HERMES_STAGING_HOME" "$HERMES_BIN" gateway status 2>&1 || true)
  if echo "$gw_status" | grep -qi "running"; then
    echo "  Hermes staging gateway: running"
  else
    echo "  WARN: Hermes staging gateway not running (non-blocking)"
    echo "  Output: $gw_status"
    return 0
  fi

  local hermes_status
  hermes_status=$(HERMES_HOME="$HERMES_STAGING_HOME" "$HERMES_BIN" status 2>&1 || true)
  if echo "$hermes_status" | grep -i "Slack" | grep -q "✓"; then
    echo "  Hermes staging Slack: configured"
  else
    echo "  WARN: Hermes staging Slack not configured (non-blocking)"
  fi
  echo "HERMES STAGING: OK (or non-blocking warnings)"
}

hermes_sync_config() {
  section "Hermes Config Sync (staging → prod)"

  # config.yaml — only sync if staging has model.default set (not a stub)
  if python3 -c "
import yaml
with open('$HERMES_STAGING_HOME/config.yaml') as f:
    cfg = yaml.safe_load(f)
model = cfg.get('model', {})
assert model.get('default') or model.get('provider'), 'stub config'
" 2>/dev/null; then
    cp "$HERMES_STAGING_HOME/config.yaml" "$HERMES_PROD_HOME/config.yaml"
    echo "  config.yaml synced"
  else
    echo "  SKIP: staging config.yaml is a stub; preserving prod config"
  fi

  # skills/ directory
  if [[ -d "$HERMES_STAGING_HOME/skills" ]]; then
    local skill_count
    skill_count=$(find "$HERMES_STAGING_HOME/skills" -name "*.py" -o -name "*.md" 2>/dev/null | wc -l | tr -d ' ')
    if [[ "$skill_count" -lt 1 ]]; then
      echo "  SKIP: staging skills/ appears empty; preserving prod skills"
    else
      mkdir -p "$HERMES_PROD_HOME/skills"
      rsync -av --delete \
        --exclude '__pycache__' \
        --exclude '*.pyc' \
        "$HERMES_STAGING_HOME/skills/" "$HERMES_PROD_HOME/skills/" 2>/dev/null
      echo "  skills/ synced ($skill_count files)"
    fi
  fi

  # Policy files (SOUL.md, AGENTS.md, TOOLS.md, HEARTBEAT.md, prefill.json)
  for policy_file in SOUL.md AGENTS.md TOOLS.md HEARTBEAT.md prefill.json; do
    if [[ -f "$HERMES_STAGING_HOME/$policy_file" ]]; then
      cp "$HERMES_STAGING_HOME/$policy_file" "$HERMES_PROD_HOME/$policy_file"
      echo "  $policy_file synced"
    fi
  done

  echo "  SKIP: .env (prod has own secrets)"
  echo "  SKIP: auth.json (prod has own credentials)"
  echo "  SKIP: logs/ (runtime only)"

  echo "Hermes config sync complete"
}

hermes_restart_prod() {
  section "Hermes Stage H3: Restart Production Gateway"

  local domain="gui/$(id -u)"

  echo "Restarting Hermes prod gateway via launchctl kickstart -k..."
  launchctl kickstart -k "${domain}/${HERMES_PROD_LABEL}" 2>/dev/null \
    || die "launchctl kickstart failed for ${HERMES_PROD_LABEL}" "Hermes Restart" "Hermes"

  echo "Waiting for gateway to come up (timeout ${HERMES_GATEWAY_START_TIMEOUT_SECONDS}s)..."
  local started_at
  started_at=$(date +%s)
  while true; do
    local status_out
    status_out=$(HERMES_HOME="$HERMES_PROD_HOME" "$HERMES_BIN" gateway status 2>&1 || true)
    if echo "$status_out" | grep -qi "running"; then
      echo "  Gateway is running"
      break
    fi
    local elapsed=$(( $(date +%s) - started_at ))
    if (( elapsed >= HERMES_GATEWAY_START_TIMEOUT_SECONDS )); then
      die "Hermes prod gateway failed to start within ${HERMES_GATEWAY_START_TIMEOUT_SECONDS}s" "Hermes Restart" "Hermes"
    fi
    sleep "$HERMES_GATEWAY_START_POLL_SECONDS"
  done

  local prod_pid
  prod_pid=$(launchctl list | grep -w "ai.smartclaw.hermes.prod" | awk '{print $1}')
  local gw_count
  gw_count=$(echo "$prod_pid" | grep -c '[0-9]' || true)
  if [[ "$gw_count" -ne 1 ]]; then
    die "Hermes gateway instance count=$gw_count (expected 1) — possible orphan conflict" "Hermes Restart" "Hermes"
  else
    echo "  Single-instance check: OK (pid=$prod_pid)"
  fi
}

hermes_post_restart_validation() {
  section "Hermes Stage H4: Post-Restart Validation"

  local status_out
  status_out=$(HERMES_HOME="$HERMES_PROD_HOME" "$HERMES_BIN" gateway status 2>&1 || true)
  if echo "$status_out" | grep -qi "running"; then
    echo "  PASS: Gateway is running"
  else
    die "Gateway not running after restart: $status_out" "Hermes Validation" "Hermes"
  fi

  local hermes_status
  hermes_status=$(HERMES_HOME="$HERMES_PROD_HOME" "$HERMES_BIN" status 2>&1 || true)
  if echo "$hermes_status" | grep -i "Slack" | grep -q "✓"; then
    echo "  PASS: Slack is configured"
  else
    echo "  WARN: Slack status check inconclusive"
    echo "  Output: $(echo "$hermes_status" | grep -i "Slack" | head -1)"
  fi

  if [[ -f "$SCRIPT_DIR/hermes-monitor.sh" ]]; then
    echo "  Running hermes-monitor.sh..."
    if bash "$SCRIPT_DIR/hermes-monitor.sh" > "$HERMES_MONITOR_LOG" 2>&1; then
      echo "  PASS: hermes-monitor.sh exited 0"
    else
      echo "  FAIL: hermes-monitor.sh exited non-zero — see $HERMES_MONITOR_LOG"
      cat "$HERMES_MONITOR_LOG" | grep -E "FAIL|WARN" || true
      die "hermes-monitor.sh failed" "Hermes Validation" "Hermes"
    fi
  else
    echo "  SKIP: hermes-monitor.sh not found"
  fi

  local token_conflict
  token_conflict=$(echo "$status_out" | grep -qi "token already in use" && echo "yes" || echo "no")
  if [[ "$token_conflict" == "yes" ]]; then
    if echo "$status_out" | grep -i "token already in use" | grep -qi "slack"; then
      die "Slack token conflict detected" "Hermes Validation" "Hermes"
    else
      echo "  WARN: Non-Slack token conflict (acceptable)"
    fi
  fi

  echo ""
  echo "HERMES PROD PASSED — all validation checks green"
}

deploy_hermes() {
  section "═══ HERMES DEPLOY ═══"

  hermes_preflight

  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo ""
    echo "DRY RUN: Hermes preflight passed. Skipping restart."
    return 0
  fi

  if [[ "$PROD_ONLY" -eq 0 ]]; then
    hermes_validate_staging
  fi

  hermes_sync_config
  hermes_restart_prod
  hermes_post_restart_validation
}

# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

parse_args "$@"

section "Deploy Start"
echo "System:    Hermes (smartclaw)"
echo "Dry run:   $DRY_RUN"
echo "Prod only: $PROD_ONLY"
echo "Run ID:    $DEPLOY_RUN_ID"
echo "Staging:   $HERMES_STAGING_HOME"
echo "Prod:      $HERMES_PROD_HOME"

deploy_hermes

section "Deploy Complete"
echo "Branch:  $(git branch --show-current 2>/dev/null || echo 'unknown')"
echo "Commit:  $(git log --oneline -1 2>/dev/null || echo 'unknown')"
echo ""
echo "$(ts) — deploy finished successfully"
