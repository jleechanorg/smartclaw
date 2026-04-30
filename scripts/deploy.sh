#!/usr/bin/env bash
# deploy.sh — Hermes gateway deploy (with legacy OpenClaw support).
#
# Architecture:
#   Hermes (primary):
#     ~/.hermes/        = STAGING (git repo root)
#     ~/.hermes_prod/   = PRODUCTION (config.yaml, .env, auth.json, logs/)
#   OpenClaw (legacy, gated behind --system openclaw|all):
#     ~/.smartclaw/      = STAGING (the repo checkout, port 18810)
#     ~/.smartclaw_prod/ = PRODUCTION (separate dir, port 18789, symlinks to shared resources)
#
# Flow (default = hermes only):
#   Hermes: preflight → config sync → restart → validate (hermes-monitor.sh)
#   OpenClaw (legacy): preflight → staging canary → push → config sync → restart → validate
#
# Usage:
#   ./scripts/deploy.sh                        # deploy Hermes (default)
#   ./scripts/deploy.sh --system hermes        # Hermes only (explicit)
#   ./scripts/deploy.sh --system openclaw      # OpenClaw only (legacy)
#   ./scripts/deploy.sh --system all           # both
#   ./scripts/deploy.sh --dry-run              # preflight checks only
#   ./scripts/deploy.sh --skip-push            # skip git push (OpenClaw)
#   ./scripts/deploy.sh --prod-only            # skip staging validation
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Shared variables ──────────────────────────────────────────────────────────
DEPLOY_RUN_ID="$(date +%Y%m%d%H%M%S)-$$"
MONITOR_FAILURE_SLACK_TARGET="${OPENCLAW_DEPLOY_MONITOR_FAILURE_SLACK_TARGET:-${OPENCLAW_MONITOR_FAILURE_SLACK_TARGET:-${SLACK_CHANNEL_ID:-}}}"
# Guard: if entire chain resolves to empty, use C00000000 (null channel = no-op, no crash)
if [ -z "${MONITOR_FAILURE_SLACK_TARGET}" ]; then
  MONITOR_FAILURE_SLACK_TARGET="C00000000"
fi
SKIP_PUSH=0
PROD_ONLY=0
DRY_RUN=0
DEPLOY_SYSTEMS=()

# ── OpenClaw variables ────────────────────────────────────────────────────────
STAGING_DIR="$HOME/.smartclaw"
PROD_DIR="$HOME/.smartclaw_prod"
STAGING_PORT="${OPENCLAW_STAGING_PORT:-18810}"
PROD_PORT="${OPENCLAW_PROD_PORT:-18789}"
GATEWAY_START_TIMEOUT_SECONDS="${OPENCLAW_DEPLOY_GATEWAY_START_TIMEOUT_SECONDS:-150}"
GATEWAY_START_POLL_SECONDS="${OPENCLAW_DEPLOY_GATEWAY_START_POLL_SECONDS:-5}"
CANARY_MAX_ATTEMPTS="${OPENCLAW_DEPLOY_CANARY_MAX_ATTEMPTS:-3}"
CANARY_RETRY_COOLDOWN_SECONDS="${OPENCLAW_DEPLOY_CANARY_RETRY_COOLDOWN_SECONDS:-15}"
STAGING_CANARY_LOG="/tmp/staging-canary-${DEPLOY_RUN_ID}.log"
PROD_CANARY_LOG="/tmp/prod-canary-${DEPLOY_RUN_ID}.log"
STAGING_MONITOR_LOG="/tmp/staging-monitor-${DEPLOY_RUN_ID}.log"
STAGING_MONITOR_STDOUT="/tmp/staging-monitor-${DEPLOY_RUN_ID}.stdout"
STAGING_MONITOR_LOCK="/tmp/staging-monitor-${DEPLOY_RUN_ID}.lock"
PROD_MONITOR_LOG="/tmp/prod-monitor-${DEPLOY_RUN_ID}.log"
PROD_MONITOR_STDOUT="/tmp/prod-monitor-${DEPLOY_RUN_ID}.stdout"
PROD_MONITOR_LOCK="/tmp/prod-monitor-${DEPLOY_RUN_ID}.lock"

# ── Hermes variables ──────────────────────────────────────────────────────────
HERMES_BIN="${HERMES_BIN:-/opt/homebrew/bin/hermes}"
HERMES_STAGING_HOME="${HERMES_STAGING_HOME:-$HOME/.hermes}"
HERMES_PROD_HOME="${HERMES_PROD_HOME:-$HOME/.hermes_prod}"
HERMES_PROD_LABEL="ai.hermes.prod"
HERMES_GATEWAY_START_TIMEOUT_SECONDS="${HERMES_DEPLOY_GATEWAY_START_TIMEOUT_SECONDS:-90}"
HERMES_GATEWAY_START_POLL_SECONDS="${HERMES_DEPLOY_GATEWAY_START_POLL_SECONDS:-3}"
HERMES_MONITOR_LOG="/tmp/hermes-monitor-${DEPLOY_RUN_ID}.log"

# ── Argument parsing ──────────────────────────────────────────────────────────

parse_args() {
  local system_arg=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --system)
        shift
        system_arg="${1:-}"
        [[ -n "$system_arg" ]] || { echo "Error: --system requires a value (openclaw|hermes|all)"; exit 1; }
        ;;
      --skip-push) SKIP_PUSH=1 ;;
      --prod-only) PROD_ONLY=1 ;;
      --dry-run)   DRY_RUN=1 ;;
      -h|--help)
        echo "Usage: $0 [--system openclaw|hermes|all] [--skip-push] [--prod-only] [--dry-run]"
        exit 0
        ;;
      *) echo "Unknown arg: $1"; exit 1 ;;
    esac
    shift
  done

  case "${system_arg:-hermes}" in
    openclaw) DEPLOY_SYSTEMS=(openclaw) ;;
    hermes)   DEPLOY_SYSTEMS=(hermes) ;;
    all)      DEPLOY_SYSTEMS=(hermes openclaw) ;;
    *)        echo "Error: --system must be openclaw, hermes, or all"; exit 1 ;;
  esac
}

# ── Shared utility functions ──────────────────────────────────────────────────

ts() { date '+%Y-%m-%d %H:%M:%S'; }
section() { echo ""; echo "=== $1 ==="; echo "$(ts)"; echo ""; }

die() {
  local msg="$1"
  local stage="${2:-}"
  local system="${3:-OpenClaw}"
  echo "DEPLOY FAILED: $msg" >&2

  local alert_subject="[$system Deploy] Stage failed: $msg"
  local alert_body="Deploy aborted at stage: $stage

Reason: $msg

Time: $(ts)
System: $system
Branch: $(git branch --show-current 2>/dev/null || echo 'unknown')
Commit: $(git log --oneline -1 2>/dev/null || echo 'unknown')

Next steps:
1. Fix the reported issue
2. Re-run deploy.sh --system $(echo "$system" | tr '[:upper:]' '[:lower:]')"

  # Alert to Slack
  local slack_target="${MONITOR_FAILURE_SLACK_TARGET}"
  local slack_msg="[DEPLOY FAILED] System: $system | Stage: $stage | Reason: $msg | Time: $(ts)"
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
  local system="${3:-OpenClaw}"
  local alert_subject="[$system Deploy] Success: $stage passed"
  local health_info=""
  if [[ -n "$port" && "$port" != "N/A" ]]; then
    health_info="$(curl -sf --max-time 5 "http://127.0.0.1:${port}/health" 2>/dev/null || echo 'N/A (no HTTP endpoint)')"
  else
    health_info="CLI-based (no HTTP endpoint)"
  fi
  local alert_body="Deploy $stage validation passed.

Time: $(ts)
System: $system
Stage: $stage
Gateway health: $health_info
Branch: $(git branch --show-current 2>/dev/null || echo 'unknown')
Commit: $(git log --oneline -1 2>/dev/null || echo 'unknown')"

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
  local system="${3:-OpenClaw}"
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
    return 0  # staging not running is non-fatal for prod deploy
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

  # Explicit skips
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

  # Verify no duplicate gateway processes (strict — matches OpenClaw Stage 4)
  # Use launchd to get the prod job's PID — this is prod-specific and ignores
  # any staging Hermes instances that may be running concurrently.
  local prod_pid
  prod_pid=$(launchctl list | grep -w "ai.hermes.prod" | awk '{print $1}')
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

  # 1. Gateway status
  local status_out
  status_out=$(HERMES_HOME="$HERMES_PROD_HOME" "$HERMES_BIN" gateway status 2>&1 || true)
  if echo "$status_out" | grep -qi "running"; then
    echo "  PASS: Gateway is running"
  else
    die "Gateway not running after restart: $status_out" "Hermes Validation" "Hermes"
  fi

  # 2. Hermes status — Slack configured
  local hermes_status
  hermes_status=$(HERMES_HOME="$HERMES_PROD_HOME" "$HERMES_BIN" status 2>&1 || true)
  if echo "$hermes_status" | grep -i "Slack" | grep -q "✓"; then
    echo "  PASS: Slack is configured"
  else
    echo "  WARN: Slack status check inconclusive"
    echo "  Output: $(echo "$hermes_status" | grep -i "Slack" | head -1)"
  fi

  # 3. Run hermes-monitor.sh
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
    echo "  SKIP: hermes-monitor.sh not found (will be available after merge to main)"
  fi

  # 4. Token conflict check
  if echo "$status_out" | grep -qi "token already in use"; then
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

  send_deploy_success_alert "Hermes Production" "N/A" "Hermes" 2>/dev/null || true
}

# ══════════════════════════════════════════════════════════════════════════════
# OPENCLAW DEPLOY PIPELINE (existing logic, wrapped in function)
# ══════════════════════════════════════════════════════════════════════════════

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
    label="ai.smartclaw.staging"
    plist="$HOME/Library/LaunchAgents/ai.smartclaw.staging.plist"
  elif [[ "$port" == "$PROD_PORT" ]]; then
    label="ai.smartclaw.gateway"
    plist="$HOME/Library/LaunchAgents/ai.smartclaw.gateway.plist"
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

deploy_openclaw() {
  section "═══ OPENCLAW DEPLOY ═══"

  # ── Preflight ──────────────────────────────────────────────────────────────
  section "OpenClaw Preflight"

  cd "$REPO_DIR"
  BRANCH="$(git branch --show-current)"
  REMOTE="$(git remote get-url origin)"
  echo "Branch:      $BRANCH"
  echo "Remote:      $REMOTE"
  echo "Staging dir: $STAGING_DIR"
  echo "Prod dir:    $PROD_DIR"

  if [[ "$REMOTE" != *"smartclaw"* ]]; then
    die "origin does not point to smartclaw: $REMOTE" "Preflight" "OpenClaw"
  fi

  if [[ ! -d "$PROD_DIR" ]]; then
    die "Prod directory does not exist: $PROD_DIR (run scripts/install.sh first)" "Preflight" "OpenClaw"
  fi

  echo ""
  echo "Running gateway preflight..."
  bash "$SCRIPT_DIR/gateway-preflight.sh" || die "gateway-preflight.sh failed" "Preflight" "OpenClaw"

  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo ""
    echo "DRY RUN: OpenClaw preflight passed. Skipping stages 1-4."
    return 0
  fi

  # ── Stage 1: Staging validation ────────────────────────────────────────────
  if [[ "$PROD_ONLY" -eq 0 ]]; then
    section "Stage 1: Staging Gateway Validation (port $STAGING_PORT)"

    STAGING_HEALTH=$(curl -sf --max-time 8 "http://127.0.0.1:${STAGING_PORT}/health" 2>&1 || echo "")
    if [[ -z "$STAGING_HEALTH" ]]; then
      echo "Staging gateway not responding — recovering launchd job..."
      ensure_gateway_up_for_port "$STAGING_PORT" 1 || true
      STAGING_HEALTH=$(curl -sf --max-time 8 "http://127.0.0.1:${STAGING_PORT}/health" 2>&1 || echo "")
      [[ -n "$STAGING_HEALTH" ]] || die "Staging gateway failed to start on port $STAGING_PORT" "Stage 1: Gateway Start" "OpenClaw"
    fi
    echo "Staging gateway healthy: $STAGING_HEALTH"

    echo ""
    echo "Running staging canary..."
    post_monitor_canary_with_retry "$STAGING_PORT" "$STAGING_CANARY_LOG" 0 \
      || die "Staging canary FAILED — see $STAGING_CANARY_LOG" "Stage 1: Canary" "OpenClaw"

    echo ""
    echo "Running monitor-agent against staging..."
    env -u OPENCLAW_GATEWAY_TOKEN -u OPENCLAW_GATEWAY_REMOTE_TOKEN \
      OPENCLAW_MONITOR_HTTP_GATEWAY_URL="http://127.0.0.1:${STAGING_PORT}/health" \
      OPENCLAW_STATE_DIR="$PROD_DIR" \
      OPENCLAW_CONFIG_PATH="$PROD_DIR/openclaw.json" \
      OPENCLAW_MONITOR_GATEWAY_PLIST_PATH="$HOME/Library/LaunchAgents/ai.smartclaw.staging.plist" \
      OPENCLAW_MONITOR_LOG_FILE="$STAGING_MONITOR_LOG" \
      OPENCLAW_MONITOR_LOCK_DIR="$STAGING_MONITOR_LOCK" \
      OPENCLAW_MONITOR_SLACK_TARGET="" \
      OPENCLAW_MONITOR_FAILURE_SLACK_TARGET="$MONITOR_FAILURE_SLACK_TARGET" \
      OPENCLAW_MONITOR_SLACK_READ_PROBE_ENABLE=0 \
      OPENCLAW_MONITOR_SLACK_E2E_MATRIX_ENABLE=0 \
      OPENCLAW_MONITOR_GATEWAY_PROBE_MESSAGE_ENABLE=0 \
      OPENCLAW_MONITOR_THREAD_REPLY_CHECK=0 \
      OPENCLAW_MONITOR_FAIL_CLOSED_CONFIG_SIGNATURES_ENABLE=0 \
      OPENCLAW_MONITOR_TOKEN_PROBES_ENABLE=0 \
      OPENCLAW_MONITOR_MEMORY_LOOKUP_ENABLE=0 \
      OPENCLAW_MONITOR_DOCTOR_SH_ENABLE=0 \
      OPENCLAW_MONITOR_INFERENCE_PROBE_ENABLE=0 \
      OPENCLAW_MONITOR_PHASE2_ENABLE=0 \
      OPENCLAW_MONITOR_PHASE1_REMEDIATION_ENABLE=0 \
      OPENCLAW_MONITOR_RUN_CANARY=0 \
      bash "$HOME/.smartclaw/monitor-agent.sh" > "$STAGING_MONITOR_STDOUT" 2>&1 \
      || die "Monitor-agent FAILED on staging — see $STAGING_MONITOR_LOG and $STAGING_MONITOR_STDOUT" "Stage 1: Monitor" "OpenClaw"
    assert_monitor_status_good "Stage 1: Monitor" "$STAGING_MONITOR_LOG" "OpenClaw"

    post_monitor_canary_with_retry "$STAGING_PORT" "$STAGING_CANARY_LOG" 0 \
      || die "Post-monitor canary FAILED — see $STAGING_CANARY_LOG" "Stage 1: Canary (re-check)" "OpenClaw"

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
      git merge "$BRANCH" --no-edit || die "Merge conflict — resolve manually" "Stage 2: Merge" "OpenClaw"
      git push origin main || die "Push to origin/main failed" "Stage 2: Push" "OpenClaw"
      echo "Pushed to origin/main"
    else
      echo "Already on main — pulling latest..."
      git pull origin main || die "Pull failed" "Stage 2: Pull" "OpenClaw"
      AHEAD=$(git rev-list origin/main..HEAD --count 2>/dev/null || echo "0")
      if [[ "$AHEAD" -gt 0 ]]; then
        echo "Pushing $AHEAD commit(s) to origin/main..."
        git push origin main || die "Push to origin/main failed" "Stage 2: Push" "OpenClaw"
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

  if is_stub_main_config "$STAGING_DIR/openclaw.json"; then
    if [[ -f "$PROD_DIR/openclaw.json" ]]; then
      echo "  WARN: staging openclaw.json is an incomplete repo stub; preserving existing prod openclaw.json"
    else
      die "Staging openclaw.json is incomplete and prod openclaw.json is missing" "Stage 3: Config Sync" "OpenClaw"
    fi
  else
    cp "$STAGING_DIR/openclaw.json" "$PROD_DIR/openclaw.json"
    echo "  openclaw.json synced"
  fi

  if [[ -f "$STAGING_DIR/cron/jobs.json" ]]; then
    mkdir -p "$PROD_DIR/cron"
    cp "$STAGING_DIR/cron/jobs.json" "$PROD_DIR/cron/jobs.json"
    echo "  cron/jobs.json synced"
  fi

  if [[ -d "$STAGING_DIR/scripts" ]]; then
    rsync -av --delete \
      --exclude '__pycache__' \
      --exclude '*.pyc' \
      --exclude '*.pyo' \
      --exclude '.git' \
      "$STAGING_DIR/scripts/" "$PROD_DIR/scripts/"
    echo "  scripts/ synced"
  fi

  if [[ -d "$STAGING_DIR/lib" ]]; then
    mkdir -p "$PROD_DIR/lib"
    rsync -av --delete \
      --exclude '.git' \
      "$STAGING_DIR/lib/" "$PROD_DIR/lib/"
    echo "  lib/ synced"
  fi

  if [[ -f "$STAGING_DIR/run-scheduled-job.sh" ]]; then
    cp -p "$STAGING_DIR/run-scheduled-job.sh" "$PROD_DIR/run-scheduled-job.sh"
    chmod +x "$PROD_DIR/run-scheduled-job.sh"
    echo "  run-scheduled-job.sh synced"
  fi

  if [[ -f "$STAGING_DIR/monitor-agent.sh" ]]; then
    cp -p "$STAGING_DIR/monitor-agent.sh" "$PROD_DIR/monitor-agent.sh"
    chmod +x "$PROD_DIR/monitor-agent.sh"
    echo "  monitor-agent.sh synced"
  fi

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

  if [[ -d "$STAGING_DIR/memory" ]]; then
    rsync -av --delete \
      --exclude '*.sqlite' \
      --exclude '*.sqlite.backup-*' \
      --exclude '*.sqlite.tmp-*' \
      --exclude 'extraction-state.lock' \
      "$STAGING_DIR/memory/" "$PROD_DIR/memory/"
    echo "  memory/ synced"
  fi

  for target in SOUL.md TOOLS.md HEARTBEAT.md extensions agents credentials lcm.db skills; do
    src="$STAGING_DIR/$target"
    dst="$PROD_DIR/$target"
    if [[ -e "$src" ]] && [[ ! -L "$dst" ]]; then
      ln -sf "$src" "$dst"
      echo "  symlinked $target"
    fi
  done

  echo "Config sync complete"

  PROD_AUTH="$PROD_DIR/agents/main/agent/auth-profiles.json"
  STAGING_AUTH="$STAGING_DIR/agents/main/agent/auth-profiles.json"
  if [[ ! -f "$PROD_AUTH" ]]; then
    if [[ -f "$STAGING_AUTH" ]]; then
      mkdir -p "$(dirname "$PROD_AUTH")"
      cp "$STAGING_AUTH" "$PROD_AUTH"
      echo "  Seeded auth-profiles.json into prod state dir (was missing)"
    else
      die "auth-profiles.json missing from both staging ($STAGING_AUTH) and prod ($PROD_AUTH) — agent cannot authenticate" "Stage 3: Auth Profiles" "OpenClaw"
    fi
  else
    echo "  auth-profiles.json present in prod"
  fi

  # ── Stage 3.5: Sync launchd plist templates ────────────────────────────────
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

  bash "$STAGING_DIR/scripts/install-launchagents.sh" > /tmp/install-launchagents.log 2>&1 \
    || echo "  install-launchagents.sh exited $? — see /tmp/install-launchagents.log"
  echo "  install-launchagents.sh complete"

  # ── Stage 4: Production gateway restart + validation ──────────────────────
  section "Stage 4: Production Gateway Validation (port $PROD_PORT)"

  echo "Restarting production gateway..."
  launchctl stop "gui/$(id -u)/ai.smartclaw.gateway" 2>/dev/null || true
  launchctl bootout "gui/$(id -u)/ai.smartclaw.gateway" 2>/dev/null || true
  sleep 3

  # Kill orphaned openclaw-gateway processes
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

  # Clear stale session lock files
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
  _clear_stale_locks "$STAGING_DIR/agents/main/sessions"

  ensure_gateway_up_for_port "$PROD_PORT" 1 \
    || die "Production gateway failed to start on port $PROD_PORT under label ai.smartclaw.gateway" "Stage 4: Gateway Start" "OpenClaw"
  PROD_HEALTH=""
  for _health_attempt in 1 2 3; do
    PROD_HEALTH="$(curl -sf --max-time 8 "http://127.0.0.1:${PROD_PORT}/health" 2>&1 || true)"
    [[ -n "$PROD_HEALTH" ]] && break
    sleep 3
  done
  [[ -n "$PROD_HEALTH" ]] \
    || die "Production gateway /health unavailable after startup on port $PROD_PORT" "Stage 4: Gateway Health" "OpenClaw"
  echo "Production gateway healthy: $PROD_HEALTH"

  # Assert exactly 1 gateway process listening on the prod port.
  _running_gw="$(
    { lsof -i ":${PROD_PORT}" -sTCP:LISTEN -t 2>/dev/null || true; } \
      | sort -u | wc -l | tr -d ' '
  )"
  if [[ "$_running_gw" -ne 1 ]]; then
    die "Post-restart gateway instance count=$_running_gw on port $PROD_PORT (expected 1) — possible orphan conflict" "Stage 4: Single-instance check" "OpenClaw"
  fi
  echo "  Single-instance check: 1 openclaw-gateway process confirmed on port $PROD_PORT"

  # Assert canonical label is loaded and legacy label is NOT loaded.
  if ! launchctl print "gui/$(id -u)/ai.smartclaw.gateway" >/dev/null 2>&1; then
    die "Canonical label ai.smartclaw.gateway not loaded after restart" "Stage 4: Label assertion" "OpenClaw"
  fi
  if launchctl print "gui/$(id -u)/com.smartclaw.gateway" >/dev/null 2>&1; then
    die "Legacy label com.smartclaw.gateway is still loaded — remove duplicate plist" "Stage 4: Label assertion" "OpenClaw"
  fi
  echo "  Label assertion: ai.smartclaw.gateway loaded, com.smartclaw.gateway absent"

  echo ""
  echo "Running production canary..."
  post_monitor_canary_with_retry "$PROD_PORT" "$PROD_CANARY_LOG" 1 \
    || die "Production canary FAILED — see $PROD_CANARY_LOG" "Stage 4: Canary" "OpenClaw"

  echo ""
  echo "Running monitor-agent against production (~/.smartclaw_prod via gateway plist)..."
  env -u OPENCLAW_GATEWAY_TOKEN -u OPENCLAW_GATEWAY_REMOTE_TOKEN \
    OPENCLAW_MONITOR_HTTP_GATEWAY_URL="http://127.0.0.1:${PROD_PORT}/health" \
    OPENCLAW_STATE_DIR="$PROD_DIR" \
    OPENCLAW_CONFIG_PATH="$PROD_DIR/openclaw.json" \
    OPENCLAW_MONITOR_GATEWAY_PLIST_PATH="$HOME/Library/LaunchAgents/ai.smartclaw.gateway.plist" \
    OPENCLAW_MONITOR_LOG_FILE="$PROD_MONITOR_LOG" \
    OPENCLAW_MONITOR_LOCK_DIR="$PROD_MONITOR_LOCK" \
    OPENCLAW_MONITOR_SLACK_TARGET="" \
    OPENCLAW_MONITOR_FAILURE_SLACK_TARGET="$MONITOR_FAILURE_SLACK_TARGET" \
    OPENCLAW_MONITOR_SLACK_E2E_MATRIX_ENABLE=0 \
    OPENCLAW_MONITOR_RUN_CANARY=0 \
    bash "$HOME/.smartclaw/monitor-agent.sh" > "$PROD_MONITOR_STDOUT" 2>&1 \
    || die "Monitor-agent FAILED on production — see $PROD_MONITOR_LOG and $PROD_MONITOR_STDOUT" "Stage 4: Monitor" "OpenClaw"
  assert_monitor_status_good "Stage 4: Monitor" "$PROD_MONITOR_LOG" "OpenClaw"

  post_monitor_canary_with_retry "$PROD_PORT" "$PROD_CANARY_LOG" 1 \
    || die "Post-monitor canary FAILED — see $PROD_CANARY_LOG" "Stage 4: Canary (re-check)" "OpenClaw"

  echo ""
  echo "OPENCLAW PROD PASSED — all checks green on port $PROD_PORT"

  send_deploy_success_alert "Production" "$PROD_PORT" "OpenClaw" 2>/dev/null || true
}

# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

parse_args "$@"

section "Deploy Start"
echo "Systems:  ${DEPLOY_SYSTEMS[*]}"
echo "Dry run:  $DRY_RUN"
echo "Prod only: $PROD_ONLY"
echo "Skip push: $SKIP_PUSH"
echo "Run ID:   $DEPLOY_RUN_ID"

for system in "${DEPLOY_SYSTEMS[@]}"; do
  case "$system" in
    hermes)   deploy_hermes ;;
    openclaw) deploy_openclaw ;;
  esac
done

# ── Done ──────────────────────────────────────────────────────────────────────

section "Deploy Complete"
echo "Systems deployed: ${DEPLOY_SYSTEMS[*]}"
echo "Branch:  $(git branch --show-current 2>/dev/null || echo 'unknown')"
echo "Commit:  $(git log --oneline -1 2>/dev/null || echo 'unknown')"
echo ""
echo "$(ts) — deploy finished successfully"
