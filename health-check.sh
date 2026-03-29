#!/bin/bash
# OpenClaw Health Check & Auto-Recovery Script
# Staged remediation: health probe -> restart -> doctor --fix -> reinstall -> escalate.

set -u

LOG_FILE="$HOME/.openclaw/logs/health-check.log"
LOG_DIR="$(dirname "$LOG_FILE")"
LOCK_DIR="$HOME/.openclaw/locks/health-check.lock"
LOCK_PID_FILE="$LOCK_DIR/pid"
LOCK_MAX_AGE_SECONDS="${OPENCLAW_HEALTH_LOCK_MAX_AGE_SECONDS:-900}"
STATE_DIR="$HOME/.openclaw/state"
ESCALATION_STAMP="$STATE_DIR/health-check-last-escalation.ts"
ALERT_STAMP_UNHEALTHY="$STATE_DIR/health-check-last-alert-unhealthy.ts"
ALERT_STAMP_RECOVERED="$STATE_DIR/health-check-last-alert-recovered.ts"

export PATH="$HOME/.nvm/versions/node/current/bin:$HOME/.nvm/versions/node/v22.22.0/bin:$HOME/Library/pnpm:$HOME/.bun/bin:$HOME/.local/bin:$HOME/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

OPENCLAW_BIN="$(command -v openclaw || true)"
AI_ORCH_BIN="$(command -v ai_orch || true)"
AGENTO_BIN="$(command -v agento || true)"
GOG_BIN="$(command -v gog || true)"
MAIL_BIN="$(command -v mail || true)"

GATEWAY_PORT="${OPENCLAW_GATEWAY_PORT:-18789}"
HEALTH_URL="${OPENCLAW_HEALTH_URL:-http://127.0.0.1:${GATEWAY_PORT}/health}"
CURL_TIMEOUT="${OPENCLAW_HEALTH_CURL_TIMEOUT:-8}"
POST_ACTION_WAIT="${OPENCLAW_POST_ACTION_WAIT_SECONDS:-3}"
ESCALATION_COOLDOWN_SECONDS="${OPENCLAW_ESCALATION_COOLDOWN_SECONDS:-3600}"
ALERT_COOLDOWN_SECONDS="${OPENCLAW_ALERT_COOLDOWN_SECONDS:-900}"
MAX_LOG_TAIL_LINES="${OPENCLAW_SELF_HEAL_LOG_TAIL_LINES:-80}"
DOCTOR_FIX_ENABLED="${OPENCLAW_HEALTH_ENABLE_DOCTOR_FIX:-0}"

ALERT_SLACK_TARGET="${OPENCLAW_ALERT_SLACK_TARGET:-}"
ALERT_EMAIL_TO="${OPENCLAW_ALERT_EMAIL_TO:-}"
ALERT_EMAIL_FROM="${OPENCLAW_ALERT_EMAIL_FROM:-}"

now_epoch() {
  date +%s
}

ts() {
  date '+%Y-%m-%d %H:%M:%S'
}

log() {
  mkdir -p "$LOG_DIR" 2>/dev/null || true
  printf '[%s] %s\n' "$(ts)" "$*" >> "$LOG_FILE"
}

command_ok() {
  "$@" >> "$LOG_FILE" 2>&1
  return $?
}

gateway_health_ok() {
  curl -fsS -m "$CURL_TIMEOUT" "$HEALTH_URL" >/dev/null 2>&1
}

service_loaded() {
  launchctl list | grep -q "ai.openclaw.gateway"
}

service_running_pid() {
  launchctl list | awk '/ai\.openclaw\.gateway/{print $1}'
}

is_placeholder_gateway_token() {
  local token="${1:-}"
  case "$token" in
    ""|null|'${OPENCLAW_GATEWAY_TOKEN}'|REDACTED|PLACEHOLDER*|*PLACEHOLDER*|your-*)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

resolve_gateway_token() {
  local token=""
  local var_name=""
  local cfg_path="$HOME/.openclaw/openclaw.json"
  local plist_path="$HOME/Library/LaunchAgents/ai.openclaw.gateway.plist"

  # Prefer the live plist token (most reliable under launchd) over the caller's shell env.
  # Shell env may hold a stale value that differs from what launchd actually injected.
  if [ -f "$plist_path" ]; then
    token="$(plutil -extract EnvironmentVariables.OPENCLAW_GATEWAY_TOKEN raw -o - "$plist_path" 2>/dev/null || true)"
  fi

  if is_placeholder_gateway_token "$token" && [ -f "$cfg_path" ] && command -v jq >/dev/null 2>&1; then
    token="$(jq -r '.gateway.auth.token // empty' "$cfg_path" 2>/dev/null || true)"
  fi

  # Fall back to caller's shell env last (may be stale).
  if is_placeholder_gateway_token "$token"; then
    token="${OPENCLAW_GATEWAY_TOKEN:-}"
  fi

  if [[ "$token" =~ ^\$\{([A-Z0-9_]+)\}$ ]]; then
    var_name="${BASH_REMATCH[1]}"
    token="${!var_name:-}"
  fi

  if is_placeholder_gateway_token "$token"; then
    return 1
  fi

  printf '%s' "$token"
}

restart_gateway() {
  if [ -n "$OPENCLAW_BIN" ] && command_ok "$OPENCLAW_BIN" gateway restart; then
    return 0
  fi

  log "openclaw CLI unavailable or restart failed; trying launchctl kickstart fallback."
  launchctl kickstart -k "gui/$(id -u)/ai.openclaw.gateway" >> "$LOG_FILE" 2>&1
}

install_gateway() {
  local gateway_token=""
  gateway_token="$(resolve_gateway_token || true)"

  if [ -n "$OPENCLAW_BIN" ]; then
    if is_placeholder_gateway_token "$gateway_token"; then
      log "Skipping gateway install --force because a stable OPENCLAW_GATEWAY_TOKEN could not be resolved."
      return 1
    fi
    if command_ok "$OPENCLAW_BIN" gateway install --force --token "$gateway_token" --port "$GATEWAY_PORT"; then
      return 0
    fi
  fi

  log "openclaw CLI unavailable or install failed; trying launchctl bootstrap fallback."
  launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/ai.openclaw.gateway.plist" >> "$LOG_FILE" 2>&1
}

doctor_fix() {
  if [ -z "$OPENCLAW_BIN" ]; then
    return 1
  fi
  command_ok "$OPENCLAW_BIN" doctor --fix
}

cooldown_allows() {
  local stamp_file="$1"
  local cooldown_seconds="$2"

  mkdir -p "$STATE_DIR" 2>/dev/null || true
  if [ ! -f "$stamp_file" ]; then
    return 0
  fi

  local last now delta
  last="$(cat "$stamp_file" 2>/dev/null || echo 0)"
  now="$(now_epoch)"
  delta=$((now - last))
  [ "$delta" -ge "$cooldown_seconds" ]
}

mark_stamp() {
  local stamp_file="$1"
  mkdir -p "$STATE_DIR" 2>/dev/null || true
  now_epoch > "$stamp_file"
}

send_slack_alert() {
  local message="$1"
  [ -n "$OPENCLAW_BIN" ] || return 1
  [ -n "$ALERT_SLACK_TARGET" ] || return 1

  "$OPENCLAW_BIN" message send \
    --channel slack \
    --target "$ALERT_SLACK_TARGET" \
    --message "$message" >> "$LOG_FILE" 2>&1
}

send_email_alert() {
  local subject="$1"
  local body="$2"

  [ -n "$ALERT_EMAIL_TO" ] || return 1

  if [ -n "$GOG_BIN" ]; then
    if [ -n "$ALERT_EMAIL_FROM" ]; then
      "$GOG_BIN" send --to "$ALERT_EMAIL_TO" --from "$ALERT_EMAIL_FROM" --subject "$subject" --body "$body" >> "$LOG_FILE" 2>&1
    else
      "$GOG_BIN" send --to "$ALERT_EMAIL_TO" --subject "$subject" --body "$body" >> "$LOG_FILE" 2>&1
    fi
    return $?
  fi

  if [ -n "$MAIL_BIN" ]; then
    printf '%s\n' "$body" | "$MAIL_BIN" -s "$subject" "$ALERT_EMAIL_TO" >> "$LOG_FILE" 2>&1
    return $?
  fi

  return 1
}

send_alert() {
  local summary="$1"
  local detail="$2"
  local state="${3:-unhealthy}"  # default to unhealthy for backwards compatibility
  local alert_stamp
  local alert_message
  local email_subject
  local email_body

  # Select the appropriate stamp file based on state.
  if [ "$state" = "recovered" ]; then
    alert_stamp="$ALERT_STAMP_RECOVERED"
  else
    alert_stamp="$ALERT_STAMP_UNHEALTHY"
  fi

  if ! cooldown_allows "$alert_stamp" "$ALERT_COOLDOWN_SECONDS"; then
    log "Alert suppressed by cooldown (${ALERT_COOLDOWN_SECONDS}s)."
    return 0
  fi

  alert_message=":warning: OpenClaw gateway self-heal alert\n${summary}\n${detail}"
  email_subject="[OpenClaw] Gateway self-heal alert"
  email_body="${summary}\n\n${detail}\n\nHost: $(hostname)\nTime: $(ts)\nHealth URL: ${HEALTH_URL}"

  local alert_sent=false
  if send_slack_alert "$alert_message"; then
    log "Slack alert sent to ${ALERT_SLACK_TARGET}."
    alert_sent=true
  else
    log "Slack alert failed."
  fi

  if send_email_alert "$email_subject" "$email_body"; then
    log "Email alert sent to ${ALERT_EMAIL_TO}."
    alert_sent=true
  else
    log "Email alert failed or unavailable."
  fi

  # Update cooldown stamp if alert was sent OR if targets are unconfigured (to prevent spam).
  if [ "$alert_sent" = true ] || { [ -z "$ALERT_SLACK_TARGET" ] && [ -z "$ALERT_EMAIL_TO" ]; }; then
    mark_stamp "$alert_stamp"
  fi
}

escalate_to_agent() {
  local reason="$1"
  local task
  task="OpenClaw gateway self-heal escalation: ${reason}. Investigate gateway health, recover service, and leave findings in ~/.openclaw/logs/health-check.log and ~/.openclaw/logs/gateway.err.log."

  if ! cooldown_allows "$ESCALATION_STAMP" "$ESCALATION_COOLDOWN_SECONDS"; then
    log "Escalation suppressed by cooldown (${ESCALATION_COOLDOWN_SECONDS}s)."
    return 0
  fi

  if [ -n "$AI_ORCH_BIN" ]; then
    if "$AI_ORCH_BIN" run --async --agent-cli codex "$task" >> "$LOG_FILE" 2>&1; then
      mark_stamp "$ESCALATION_STAMP"
      log "Escalation dispatched via ai_orch."
      return 0
    fi
    log "ai_orch escalation dispatch failed."
  fi

  if [ -n "$AGENTO_BIN" ]; then
    if "$AGENTO_BIN" "$task" >> "$LOG_FILE" 2>&1; then
      mark_stamp "$ESCALATION_STAMP"
      log "Escalation dispatched via agento."
      return 0
    fi
    log "agento escalation dispatch failed."
  fi

  log "No escalation tool available or dispatch failed."
  return 1
}

if [ -z "$OPENCLAW_BIN" ]; then
  log "openclaw CLI not found in PATH=$PATH (continuing with launchctl fallbacks)."
fi

# Single-run lock to prevent overlapping launchd invocations.
mkdir -p "$(dirname "$LOCK_DIR")" 2>/dev/null || true
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  now="$(now_epoch)"
  lock_mtime="$(stat -f %m "$LOCK_DIR" 2>/dev/null || echo 0)"
  lock_age=$((now - lock_mtime))
  lock_pid="$(cat "$LOCK_PID_FILE" 2>/dev/null || echo "")"

  if [ "$lock_age" -ge "$LOCK_MAX_AGE_SECONDS" ]; then
    if [ -n "$lock_pid" ] && kill -0 "$lock_pid" 2>/dev/null; then
      log "Active lock held by pid=${lock_pid}; skipping."
      exit 0
    fi

    log "Stale lock detected (age=${lock_age}s, pid=${lock_pid:-unknown}); clearing."
    rmdir "$LOCK_DIR" 2>/dev/null || rm -rf "$LOCK_DIR" 2>/dev/null || true
    if ! mkdir "$LOCK_DIR" 2>/dev/null; then
      log "Unable to acquire lock after stale-lock cleanup; skipping."
      exit 0
    fi
  else
    log "Another health-check run is active; skipping."
    exit 0
  fi
fi

printf '%s\n' "$$" > "$LOCK_PID_FILE"
trap 'rm -f "$LOCK_PID_FILE" 2>/dev/null || true; rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT

log "Health-check start (port=${GATEWAY_PORT}, url=${HEALTH_URL})."

if gateway_health_ok; then
  pid="$(service_running_pid)"
  log "Gateway healthy (pid=${pid:-unknown})."
  exit 0
fi

log "Gateway unhealthy: initial probe failed."

if ! service_loaded; then
  log "Gateway service not loaded; installing service."
  install_gateway || log "Gateway install failed during service-load remediation."
  sleep "$POST_ACTION_WAIT"
fi

if gateway_health_ok; then
  log "Gateway recovered after service-load remediation."
  send_alert "Gateway recovered" "Recovered after service-load remediation." "recovered"
  exit 0
fi

log "Attempting remediation step 1: gateway restart."
restart_gateway || log "Gateway restart command failed."
sleep "$POST_ACTION_WAIT"

if gateway_health_ok; then
  log "Gateway recovered after restart."
  send_alert "Gateway recovered" "Recovered after gateway restart." "recovered"
  exit 0
fi

if [ "$DOCTOR_FIX_ENABLED" = "1" ]; then
  log "Attempting remediation step 2: openclaw doctor --fix (enabled by OPENCLAW_HEALTH_ENABLE_DOCTOR_FIX=1)."
  doctor_fix || log "Doctor --fix failed."
  sleep "$POST_ACTION_WAIT"

  if gateway_health_ok; then
    log "Gateway recovered after doctor --fix."
    send_alert "Gateway recovered" "Recovered after openclaw doctor --fix." "recovered"
    exit 0
  fi
else
  log "Skipping remediation step 2: openclaw doctor --fix is disabled by default (set OPENCLAW_HEALTH_ENABLE_DOCTOR_FIX=1 to enable)."
fi

log "Attempting remediation step 3: force reinstall + restart gateway."
install_gateway || log "Gateway force install failed."
restart_gateway || log "Gateway restart after install failed."
sleep "$POST_ACTION_WAIT"

if gateway_health_ok; then
  log "Gateway recovered after force reinstall + restart."
  send_alert "Gateway recovered" "Recovered after force reinstall + restart." "recovered"
  exit 0
fi

log "Gateway still unhealthy after all remediation steps."
if [ -f "$HOME/.openclaw/logs/gateway.err.log" ]; then
  log "Recent gateway.err.log tail:"
  tail -n "$MAX_LOG_TAIL_LINES" "$HOME/.openclaw/logs/gateway.err.log" >> "$LOG_FILE" 2>/dev/null || true
fi

if [ "$DOCTOR_FIX_ENABLED" = "1" ]; then
  send_alert "Gateway unhealthy" "Health probe failed after restart, doctor --fix, and force reinstall."
else
  send_alert "Gateway unhealthy" "Health probe failed after restart and force reinstall (doctor --fix disabled by default)."
fi
if [ "$DOCTOR_FIX_ENABLED" = "1" ]; then
  escalate_to_agent "health probe failed after restart/doctor/reinstall"
else
  escalate_to_agent "health probe failed after restart/reinstall (doctor-fix disabled)"
fi

if gateway_health_ok; then
  log "Gateway became healthy after escalation dispatch."
  send_alert "Gateway recovered" "Recovered after escalation dispatch." "recovered"
  exit 0
fi

log "Final status: unhealthy. Manual intervention required."
send_alert "Gateway still unhealthy" "Manual intervention required. Self-heal and escalation did not restore health."
exit 1
