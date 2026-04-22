#!/bin/bash
# Periodic proactive monitoring agent for OpenClaw

set -u

LOG_FILE="$HOME/.smartclaw/logs/monitor-agent.log"
LOG_DIR="$(dirname "$LOG_FILE")"
LOCK_DIR="$HOME/.smartclaw/locks/monitor-agent.lock"
LOCK_PID_FILE="$LOCK_DIR/pid"
LOCK_STALE_SECONDS="${OPENCLAW_MONITOR_LOCK_STALE_SECONDS:-7200}"

export PATH="$HOME/.nvm/versions/node/current/bin:$HOME/Library/pnpm:$HOME/.bun/bin:$HOME/.local/bin:$HOME/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

OPENCLAW_BIN="$(command -v openclaw || true)"
ALERT_SLACK_TARGET="${OPENCLAW_MONITOR_SLACK_TARGET:-${SLACK_CHANNEL_ID:-}}"
PROBE_SLACK_TARGET="${OPENCLAW_MONITOR_PROBE_SLACK_TARGET:-$ALERT_SLACK_TARGET}"
GATEWAY_PROBE_TARGET="${OPENCLAW_MONITOR_GATEWAY_PROBE_TARGET:-$PROBE_SLACK_TARGET}"
HTTP_GATEWAY_URL="${OPENCLAW_MONITOR_HTTP_GATEWAY_URL:-http://127.0.0.1:18789/health}"
SLACK_API_BASE="${OPENCLAW_MONITOR_SLACK_API_BASE:-https://slack.com/api}"
CANARY_TIMEOUT_SECONDS="${OPENCLAW_MONITOR_CANARY_TIMEOUT_SECONDS:-45}"
CANARY_POLL_INTERVAL_SECONDS="${OPENCLAW_MONITOR_CANARY_POLL_INTERVAL_SECONDS:-3}"
PHASE1_REMEDIATION_ENABLED="${OPENCLAW_MONITOR_PHASE1_REMEDIATION_ENABLE:-1}"
PHASE2_ENABLED="${OPENCLAW_MONITOR_PHASE2_ENABLE:-1}"
PHASE2_AUTOFIX_ENABLED="${OPENCLAW_MONITOR_PHASE2_AUTOFIX_ENABLE:-1}"
PHASE2_ALLOW_CONFIG_MUTATIONS="${OPENCLAW_MONITOR_PHASE2_ALLOW_CONFIG_MUTATIONS:-0}"
PHASE2_TIMEOUT_SECONDS="${OPENCLAW_MONITOR_PHASE2_TIMEOUT_SECONDS:-120}"
RUN_CANARY="${OPENCLAW_MONITOR_RUN_CANARY:-1}"
STATUS_BROADCAST_ENABLED="${OPENCLAW_MONITOR_STATUS_BROADCAST_ENABLE:-1}"
STATUS_BROADCAST_SLACK_TARGET="${OPENCLAW_MONITOR_STATUS_SLACK_TARGET:-${SLACK_CHANNEL_ID}}"
THREAD_REPLY_CHECK_ENABLED="${OPENCLAW_MONITOR_THREAD_REPLY_CHECK:-1}"
THREAD_REPLY_CHANNEL="${OPENCLAW_MONITOR_THREAD_REPLY_CHANNEL:-$ALERT_SLACK_TARGET}"
THREAD_REPLY_LOOKBACK_SECONDS="${OPENCLAW_MONITOR_THREAD_REPLY_LOOKBACK_SECONDS:-21600}"
THREAD_REPLY_GRACE_SECONDS="${OPENCLAW_MONITOR_THREAD_REPLY_GRACE_SECONDS:-120}"
THREAD_REPLY_MAX_THREADS="${OPENCLAW_MONITOR_THREAD_REPLY_MAX_THREADS:-12}"
THREAD_REPLY_WATCH_THREADS="${OPENCLAW_MONITOR_THREAD_REPLY_WATCH_THREADS:-}"
THREAD_REPLY_FAILURE_REGEX="${OPENCLAW_MONITOR_THREAD_REPLY_FAILURE_REGEX:-Agent failed before reply|all models failed|authentication_error|OAuth token refresh failed}"
THREAD_REPLY_FAILURE_MAX_AGE_SECONDS="${OPENCLAW_MONITOR_THREAD_REPLY_FAILURE_MAX_AGE_SECONDS:-900}"
THREAD_REPLY_BOT_USER_ID="${OPENCLAW_MONITOR_BOT_USER_ID:-}"
DOCTOR_SH_ENABLED="${OPENCLAW_MONITOR_DOCTOR_SH_ENABLE:-1}"
DOCTOR_SH_ALWAYS="${OPENCLAW_MONITOR_DOCTOR_SH_ALWAYS:-1}"
DOCTOR_SH_PATH_OVERRIDE="${OPENCLAW_MONITOR_DOCTOR_SH_PATH:-}"
INFERENCE_PROBE_ENABLED="${OPENCLAW_MONITOR_INFERENCE_PROBE_ENABLE:-1}"
INFERENCE_PROBE_TIMEOUT="${OPENCLAW_MONITOR_INFERENCE_PROBE_TIMEOUT:-30}"
# When doctor.sh always runs, it already includes an end-to-end LLM inference probe.
# Skip the monitor's own inference probe to avoid a redundant (slow) LLM call.
if [ "${OPENCLAW_MONITOR_INFERENCE_PROBE_ENABLE:-unset}" = "unset" ] \
   && [ "$DOCTOR_SH_ENABLED" = "1" ] \
   && [ "$DOCTOR_SH_ALWAYS" = "1" ]; then
  INFERENCE_PROBE_ENABLED="0"
fi

ts() {
  date '+%Y-%m-%d %H:%M:%S'
}

log() {
  mkdir -p "$LOG_DIR" 2>/dev/null || true
  printf '[%s] %s\n' "$(ts)" "$*" >> "$LOG_FILE"
}

is_placeholder_token() {
  local token="${1:-}"
  if [ -z "$token" ] || [ "$token" = "null" ] || [ "$token" = '${OPENCLAW_GATEWAY_TOKEN}' ] || [ "$token" = "your-local-auth-token-here" ]; then
    return 0
  fi
  case "$token" in
    PLACEHOLDER*|*PLACEHOLDER*)
      return 0
      ;;
  esac
  return 1
}

resolve_secret_ref() {
  local raw="${1:-}"
  if [[ "$raw" =~ ^\$\{([A-Z0-9_]+)\}$ ]]; then
    local var_name="${BASH_REMATCH[1]}"
    printf '%s' "${!var_name:-}"
    return 0
  fi
  printf '%s' "$raw"
}

resolve_bearer_token_ref() {
  local raw="${1:-}"
  if [[ "$raw" =~ ^Bearer[[:space:]]+\$\{([A-Z0-9_]+)\}$ ]]; then
    local var_name="${BASH_REMATCH[1]}"
    printf '%s' "${!var_name:-}"
    return 0
  fi
  if [[ "$raw" =~ ^Bearer[[:space:]]+(.+)$ ]]; then
    printf '%s' "${BASH_REMATCH[1]}"
    return 0
  fi
  printf '%s' "$(resolve_secret_ref "$raw")"
}

resolve_doctor_sh_path() {
  local candidate=""
  if [ -n "$DOCTOR_SH_PATH_OVERRIDE" ] && [ -f "$DOCTOR_SH_PATH_OVERRIDE" ]; then
    printf '%s' "$DOCTOR_SH_PATH_OVERRIDE"
    return 0
  fi
  for candidate in \
    "$PWD/doctor.sh" \
    "$HOME/.smartclaw/smartclaw/doctor.sh" \
    "$HOME/.smartclaw/doctor.sh"; do
    if [ -f "$candidate" ]; then
      printf '%s' "$candidate"
      return 0
    fi
  done
  if command -v doctor.sh >/dev/null 2>&1; then
    command -v doctor.sh
    return 0
  fi
  return 1
}

if [ -z "$OPENCLAW_BIN" ]; then
  log "openclaw CLI not found"
  exit 1
fi

mkdir -p "$(dirname "$LOCK_DIR")" 2>/dev/null || true

lock_mtime_epoch() {
  if stat -f %m "$LOCK_DIR" >/dev/null 2>&1; then
    stat -f %m "$LOCK_DIR"
  else
    stat -c %Y "$LOCK_DIR" 2>/dev/null || echo 0
  fi
}

acquire_lock() {
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    echo "$$" > "$LOCK_PID_FILE"
    return 0
  fi

  local pid=""
  if [ -f "$LOCK_PID_FILE" ]; then
    pid="$(cat "$LOCK_PID_FILE" 2>/dev/null || true)"
  fi

  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    log "Another monitor-agent run is active (pid=$pid); skipping."
    return 1
  fi

  local now age
  now="$(date +%s)"
  age=$(( now - $(lock_mtime_epoch) ))
  if [ "$age" -lt "$LOCK_STALE_SECONDS" ]; then
    log "Lock exists without live pid but is recent (${age}s < ${LOCK_STALE_SECONDS}s); skipping."
    return 1
  fi

  log "Removing stale lock (age=${age}s, pid='${pid:-unknown}')."
  rm -rf "$LOCK_DIR" 2>/dev/null || true
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    echo "$$" > "$LOCK_PID_FILE"
    return 0
  fi

  log "Failed to acquire lock after stale cleanup; skipping."
  return 1
}

if ! acquire_lock; then
  exit 0
fi
trap 'rm -f "$LOCK_PID_FILE" 2>/dev/null || true; rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT

if [ -f "$HOME/.profile" ]; then
  # Needed for SLACK_USER_TOKEN (xoxp) canary posts as the real user identity.
  # shellcheck disable=SC1090
  source "$HOME/.profile"
fi

# Tokens are hardcoded in ~/.smartclaw/openclaw.json — the gateway reads them directly.
# Only hydrate behavioral tunables (channel targets, feature flags) that may be
# overridden via .bashrc exports. Token env vars (bot/app/gateway tokens) are NOT
# read here; they live in openclaw.json and are not expected in plist or .bashrc.
read_bashrc_export() {
  local key="$1"
  local rc_file="$HOME/.bashrc"
  if [ ! -f "$rc_file" ]; then
    return 0
  fi
  sed -n "s/^export ${key}=\"\\(.*\\)\"/\\1/p" "$rc_file" | head -n1
}

set_env_var_if_nonempty() {
  local key="$1"
  local val="$2"
  if [ -n "$val" ]; then
    printf -v "$key" '%s' "$val"
    export "$key"
  fi
}

set_env_var_if_nonempty OPENCLAW_MONITOR_SLACK_TARGET "$(read_bashrc_export OPENCLAW_MONITOR_SLACK_TARGET)"
set_env_var_if_nonempty OPENCLAW_MONITOR_PROBE_SLACK_TARGET "$(read_bashrc_export OPENCLAW_MONITOR_PROBE_SLACK_TARGET)"
set_env_var_if_nonempty OPENCLAW_MONITOR_GATEWAY_PROBE_TARGET "$(read_bashrc_export OPENCLAW_MONITOR_GATEWAY_PROBE_TARGET)"
set_env_var_if_nonempty OPENCLAW_MONITOR_STATUS_SLACK_TARGET "$(read_bashrc_export OPENCLAW_MONITOR_STATUS_SLACK_TARGET)"
set_env_var_if_nonempty OPENCLAW_MONITOR_THREAD_REPLY_CHANNEL "$(read_bashrc_export OPENCLAW_MONITOR_THREAD_REPLY_CHANNEL)"
set_env_var_if_nonempty OPENCLAW_MONITOR_RUN_CANARY "$(read_bashrc_export OPENCLAW_MONITOR_RUN_CANARY)"

# Recompute monitor channels after env hydration from launchd/profile/bashrc.
ALERT_SLACK_TARGET="${OPENCLAW_MONITOR_SLACK_TARGET:-$ALERT_SLACK_TARGET}"
PROBE_SLACK_TARGET="${OPENCLAW_MONITOR_PROBE_SLACK_TARGET:-$ALERT_SLACK_TARGET}"
GATEWAY_PROBE_TARGET="${OPENCLAW_MONITOR_GATEWAY_PROBE_TARGET:-$PROBE_SLACK_TARGET}"
STATUS_BROADCAST_SLACK_TARGET="${OPENCLAW_MONITOR_STATUS_SLACK_TARGET:-$STATUS_BROADCAST_SLACK_TARGET}"
THREAD_REPLY_CHANNEL="${OPENCLAW_MONITOR_THREAD_REPLY_CHANNEL:-$ALERT_SLACK_TARGET}"
RUN_CANARY="${OPENCLAW_MONITOR_RUN_CANARY:-$RUN_CANARY}"

# --- Initial probes (parallelized) ---
_PROBE_TMPDIR="$(mktemp -d /tmp/monitor-init-probes.XXXXXX)"

(
  out="$("$OPENCLAW_BIN" message read --channel slack --target "$PROBE_SLACK_TARGET" --limit 1 --json 2>&1)"
  rc=$?
  printf '%s\n' "$rc" > "$_PROBE_TMPDIR/read.rc"
  printf '%s\n' "$out" > "$_PROBE_TMPDIR/read.out"
) &
_PROBE_READ_PID=$!

(
  out="$("$OPENCLAW_BIN" message send --channel slack --target "$GATEWAY_PROBE_TARGET" \
    --message "OpenClaw monitor check started: $(date '+%Y-%m-%d %H:%M:%S %Z')" --json 2>&1)"
  rc=$?
  printf '%s\n' "$rc" > "$_PROBE_TMPDIR/send.rc"
  printf '%s\n' "$out" > "$_PROBE_TMPDIR/send.out"
) &
_PROBE_SEND_PID=$!

(
  out="$(curl -sS -X GET "$HTTP_GATEWAY_URL" \
    -H "X-OpenClaw-Monitor-Message: [monitor-http-probe] $(date '+%Y-%m-%d %H:%M:%S %Z')" \
    -H "Accept: application/json" \
    -w '\nHTTP_STATUS:%{http_code}' 2>&1)"
  rc=$?
  printf '%s\n' "$rc" > "$_PROBE_TMPDIR/http.rc"
  printf '%s\n' "$out" > "$_PROBE_TMPDIR/http.out"
) &
_PROBE_HTTP_PID=$!

wait "$_PROBE_READ_PID" "$_PROBE_SEND_PID" "$_PROBE_HTTP_PID" 2>/dev/null || true

PROBE_REQUEST_RC="$(cat "$_PROBE_TMPDIR/read.rc" 2>/dev/null || echo 1)"
PROBE_REQUEST_OUTPUT="$(cat "$_PROBE_TMPDIR/read.out" 2>/dev/null || true)"
PROBE_REQUEST_SUMMARY="$(printf '%s\n' "$PROBE_REQUEST_OUTPUT" | rg -m1 '"ts"|"timestampUtc"|"thread_ts"|^Error|^gateway connect failed' || true)"
if [ -z "$PROBE_REQUEST_SUMMARY" ]; then
  PROBE_REQUEST_SUMMARY="$(printf '%s\n' "$PROBE_REQUEST_OUTPUT" | head -n 1)"
fi
PROBE_REQUEST_SUMMARY="$(printf '%s\n' "$PROBE_REQUEST_SUMMARY" | tr '\n' ' ' | sed 's/[[:space:]]\+/ /g' | cut -c1-240)"

GATEWAY_PROBE_RC="$(cat "$_PROBE_TMPDIR/send.rc" 2>/dev/null || echo 1)"
GATEWAY_PROBE_OUTPUT="$(cat "$_PROBE_TMPDIR/send.out" 2>/dev/null || true)"
GATEWAY_PROBE_SUMMARY="$(printf '%s\n' "$GATEWAY_PROBE_OUTPUT" | rg -m1 '"messageId"|"ts"|"ok"|^Error|^gateway connect failed' || true)"
if [ -z "$GATEWAY_PROBE_SUMMARY" ]; then
  GATEWAY_PROBE_SUMMARY="$(printf '%s\n' "$GATEWAY_PROBE_OUTPUT" | head -n 1)"
fi
GATEWAY_PROBE_SUMMARY="$(printf '%s\n' "$GATEWAY_PROBE_SUMMARY" | tr '\n' ' ' | sed 's/[[:space:]]\+/ /g' | cut -c1-240)"

HTTP_GATEWAY_RC="$(cat "$_PROBE_TMPDIR/http.rc" 2>/dev/null || echo 1)"
HTTP_GATEWAY_OUTPUT="$(cat "$_PROBE_TMPDIR/http.out" 2>/dev/null || true)"
rm -rf "$_PROBE_TMPDIR"
HTTP_GATEWAY_SUMMARY="$(printf '%s\n' "$HTTP_GATEWAY_OUTPUT" | rg -m1 'HTTP_STATUS:|\"ok\"|\"status\"|^curl:|^Error' || true)"
if [ -z "$HTTP_GATEWAY_SUMMARY" ]; then
  HTTP_GATEWAY_SUMMARY="$(printf '%s\n' "$HTTP_GATEWAY_OUTPUT" | head -n 1)"
fi
HTTP_GATEWAY_SUMMARY="$(printf '%s\n' "$HTTP_GATEWAY_SUMMARY" | tr '\n' ' ' | sed 's/[[:space:]]\+/ /g' | cut -c1-240)"
HTTP_GATEWAY_STATUS="$(printf '%s\n' "$HTTP_GATEWAY_OUTPUT" | rg -o 'HTTP_STATUS:[0-9]+' | tail -n1 | cut -d: -f2)"
if [ -z "$HTTP_GATEWAY_STATUS" ]; then
  HTTP_GATEWAY_STATUS="0"
fi
if [ "$HTTP_GATEWAY_STATUS" -lt 200 ] || [ "$HTTP_GATEWAY_STATUS" -ge 300 ]; then
  HTTP_GATEWAY_RC=1
fi

TOKEN_PROBE_RC=0
TOKEN_PROBE_SUMMARY="token probes not run"

run_token_probes() {
  local cfg="$HOME/.smartclaw/openclaw.json"
  local timeout=10

  if ! command -v jq >/dev/null 2>&1; then
    TOKEN_PROBE_RC=1
    TOKEN_PROBE_SUMMARY="jq missing; token probes skipped"
    return 1
  fi
  if [ ! -f "$cfg" ]; then
    TOKEN_PROBE_RC=1
    TOKEN_PROBE_SUMMARY="missing $cfg; token probes skipped"
    return 1
  fi

  # Each subprobe writes a single line: PASS:key  FAIL:key:reason  WARN:key:reason
  local td
  td="$(mktemp -d /tmp/monitor-token-probes.XXXXXX)"

  # --- gateway token ---
  local gateway_token
  gateway_token="$(resolve_secret_ref "$(jq -r '.gateway.auth.token // empty' "$cfg" 2>/dev/null || true)")"
  (
    if is_placeholder_token "$gateway_token"; then
      printf 'FAIL:gateway.auth.token:missing/placeholder\n' > "$td/gateway"
    else
      code="$(curl -sS --max-time "$timeout" -o "$td/gateway.json" -w '%{http_code}' \
        -H "Authorization: Bearer $gateway_token" -H 'Accept: application/json' \
        http://127.0.0.1:18789/health 2>/dev/null || true)"
      if [ "$code" = "200" ] && jq -e '.ok == true' "$td/gateway.json" >/dev/null 2>&1; then
        printf 'PASS:gateway.auth.token\n' > "$td/gateway"
      else
        printf 'FAIL:gateway.auth.token:health_http=%s\n' "$code" > "$td/gateway"
      fi
    fi
  ) &

  # --- slack bot token ---
  local slack_bot_token
  slack_bot_token="$(resolve_secret_ref "$(jq -r '.channels.slack.botToken // empty' "$cfg" 2>/dev/null || true)")"
  (
    if is_placeholder_token "$slack_bot_token"; then
      printf 'FAIL:channels.slack.botToken:missing/placeholder\n' > "$td/slack_bot"
    else
      code="$(curl -sS --max-time "$timeout" -X POST \
        -H "Authorization: Bearer $slack_bot_token" \
        -H 'Content-Type: application/x-www-form-urlencoded' \
        -o "$td/slack_bot.json" -w '%{http_code}' \
        'https://slack.com/api/auth.test' 2>/dev/null || true)"
      if [ "$code" = "200" ] && jq -e '.ok == true' "$td/slack_bot.json" >/dev/null 2>&1; then
        printf 'PASS:channels.slack.botToken\n' > "$td/slack_bot"
      else
        printf 'FAIL:channels.slack.botToken:auth_test_http=%s\n' "$code" > "$td/slack_bot"
      fi
    fi
  ) &

  # --- slack app token ---
  local slack_app_token
  slack_app_token="$(resolve_secret_ref "$(jq -r '.channels.slack.appToken // empty' "$cfg" 2>/dev/null || true)")"
  (
    if is_placeholder_token "$slack_app_token"; then
      printf 'FAIL:channels.slack.appToken:missing/placeholder\n' > "$td/slack_app"
    else
      code="$(curl -sS --max-time "$timeout" -X POST \
        -H "Authorization: Bearer $slack_app_token" \
        -H 'Content-Type: application/x-www-form-urlencoded' \
        -o "$td/slack_app.json" -w '%{http_code}' \
        'https://slack.com/api/apps.connections.open' 2>/dev/null || true)"
      if [ "$code" = "200" ] && jq -e '.ok == true' "$td/slack_app.json" >/dev/null 2>&1; then
        printf 'PASS:channels.slack.appToken\n' > "$td/slack_app"
      else
        printf 'FAIL:channels.slack.appToken:apps_open_http=%s\n' "$code" > "$td/slack_app"
      fi
    fi
  ) &

  # --- minimax token ---
  local minimax_token
  minimax_token="$(resolve_secret_ref "$(jq -r '.models.providers."minimax-portal".apiKey // empty' "$cfg" 2>/dev/null || true)")"
  (
    if is_placeholder_token "$minimax_token"; then
      printf 'FAIL:models.providers.minimax-portal.apiKey:missing/placeholder\n' > "$td/minimax"
    else
      code="$(curl -sS --max-time "$timeout" \
        -H "Authorization: Bearer $minimax_token" \
        -H 'anthropic-version: 2023-06-01' \
        -H 'Content-Type: application/json' \
        -d '{"model":"MiniMax-M2.5","max_tokens":8,"messages":[{"role":"user","content":"ping"}]}' \
        -o "$td/minimax.json" -w '%{http_code}' \
        'https://api.minimax.io/anthropic/v1/messages' 2>/dev/null || true)"
      if [[ "$code" =~ ^2 ]]; then
        printf 'PASS:models.providers.minimax-portal.apiKey\n' > "$td/minimax"
      else
        printf 'FAIL:models.providers.minimax-portal.apiKey:http=%s\n' "$code" > "$td/minimax"
      fi
    fi
  ) &

  # --- openai / mem0 token ---
  local openai_token
  openai_token="$(resolve_secret_ref "$(jq -r '.plugins.entries."openclaw-mem0".config.oss.embedder.config.apiKey // empty' "$cfg" 2>/dev/null || true)")"
  (
    if is_placeholder_token "$openai_token"; then
      printf 'WARN:mem0.openai.apiKey:missing/placeholder\n' > "$td/openai"
    else
      code="$(curl -sS --max-time "$timeout" \
        -H "Authorization: Bearer $openai_token" \
        -o "$td/openai.json" -w '%{http_code}' \
        'https://api.openai.com/v1/models' 2>/dev/null || true)"
      if [ "$code" = "200" ]; then
        printf 'PASS:mem0.openai.apiKey\n' > "$td/openai"
      else
        printf 'FAIL:mem0.openai.apiKey:http=%s\n' "$code" > "$td/openai"
      fi
    fi
  ) &

  # --- xai token ---
  local xai_token
  xai_token="$(resolve_secret_ref "$(jq -r '.env.XAI_API_KEY // empty' "$cfg" 2>/dev/null || true)")"
  (
    if is_placeholder_token "$xai_token"; then
      printf 'WARN:env.XAI_API_KEY:missing/placeholder\n' > "$td/xai"
    else
      code="$(curl -sS --max-time "$timeout" \
        -H "Authorization: Bearer $xai_token" \
        -o "$td/xai.json" -w '%{http_code}' \
        'https://api.x.ai/v1/models' 2>/dev/null || true)"
      if [ "$code" = "200" ]; then
        printf 'PASS:env.XAI_API_KEY\n' > "$td/xai"
      else
        printf 'FAIL:env.XAI_API_KEY:http=%s\n' "$code" > "$td/xai"
      fi
    fi
  ) &

  # --- discord token ---
  local discord_token
  discord_token="$(resolve_secret_ref "$(jq -r '.channels.discord.token // empty' "$cfg" 2>/dev/null || true)")"
  (
    if is_placeholder_token "$discord_token"; then
      printf 'WARN:channels.discord.token:missing/placeholder\n' > "$td/discord"
    else
      code="$(curl -sS --max-time "$timeout" \
        -H "Authorization: Bot $discord_token" \
        -o "$td/discord.json" -w '%{http_code}' \
        'https://discord.com/api/v10/users/@me' 2>/dev/null || true)"
      if [ "$code" = "200" ]; then
        printf 'PASS:channels.discord.token\n' > "$td/discord"
      else
        printf 'FAIL:channels.discord.token:http=%s\n' "$code" > "$td/discord"
      fi
    fi
  ) &

  # --- mcp-agent-mail ---
  local mcp_mail_url mcp_mail_auth_raw mcp_mail_token
  mcp_mail_url="$(jq -r '.plugins.entries."openclaw-mcp-adapter".config.servers[]? | select(.name=="mcp-agent-mail") | .url // empty' "$cfg" 2>/dev/null | head -n1)"
  mcp_mail_auth_raw="$(jq -r '.plugins.entries."openclaw-mcp-adapter".config.servers[]? | select(.name=="mcp-agent-mail") | .headers.Authorization // empty' "$cfg" 2>/dev/null | head -n1)"
  mcp_mail_token="$(resolve_bearer_token_ref "$mcp_mail_auth_raw")"
  if [ -n "$mcp_mail_url" ]; then
    (
      local mail_body='{"jsonrpc":"2.0","id":"monitor-probe","method":"tools/list","params":{}}'
      if [ -z "$mcp_mail_auth_raw" ]; then
        code="$(curl -sS --max-time "$timeout" -H 'Content-Type: application/json' \
          -d "$mail_body" -o "$td/mcp_mail.json" -w '%{http_code}' \
          "$mcp_mail_url" 2>/dev/null || true)"
        if [ "$code" = "200" ]; then printf 'PASS:mcp-agent-mail.noauth\n' > "$td/mcp_mail"
        else printf 'FAIL:mcp-agent-mail.noauth:http=%s\n' "$code" > "$td/mcp_mail"; fi
      elif is_placeholder_token "$mcp_mail_token"; then
        printf 'WARN:mcp-agent-mail.Authorization:missing/placeholder\n' > "$td/mcp_mail"
      else
        code="$(curl -sS --max-time "$timeout" \
          -H "Authorization: Bearer $mcp_mail_token" \
          -H 'Content-Type: application/json' \
          -d "$mail_body" -o "$td/mcp_mail.json" -w '%{http_code}' \
          "$mcp_mail_url" 2>/dev/null || true)"
        if [ "$code" = "200" ]; then printf 'PASS:mcp-agent-mail.Authorization\n' > "$td/mcp_mail"
        else printf 'FAIL:mcp-agent-mail.Authorization:http=%s\n' "$code" > "$td/mcp_mail"; fi
      fi
    ) &
  fi

  # Wait for all probes to complete
  wait

  # Aggregate results from temp files
  local fail_count=0 warn_count=0 details=""
  local line key reason
  for f in "$td"/gateway "$td"/slack_bot "$td"/slack_app "$td"/minimax \
            "$td"/openai "$td"/xai "$td"/discord "$td"/mcp_mail; do
    [ -f "$f" ] || continue
    line="$(cat "$f")"
    case "$line" in
      PASS:*)
        details="${details} ${line};"
        ;;
      FAIL:*)
        fail_count=$((fail_count + 1))
        details="${details} ${line};"
        ;;
      WARN:*)
        warn_count=$((warn_count + 1))
        details="${details} ${line};"
        ;;
    esac
  done
  rm -rf "$td"

  if [ "$fail_count" -gt 0 ]; then
    TOKEN_PROBE_RC=1
  elif [ "$warn_count" -gt 0 ]; then
    TOKEN_PROBE_RC=2
  else
    TOKEN_PROBE_RC=0
  fi
  TOKEN_PROBE_SUMMARY="fails=$fail_count warns=$warn_count details:${details}"
  return "$TOKEN_PROBE_RC"
}

MEMORY_LOOKUP_RC=0
MEMORY_LOOKUP_SUMMARY="memory lookup check not run"

run_memory_lookup_probe() {
  MEMORY_LOOKUP_RC=0
  MEMORY_LOOKUP_SUMMARY="memory lookup check passed"

  if [ "${OPENCLAW_MONITOR_MEMORY_LOOKUP_ENABLE:-1}" != "1" ]; then
    MEMORY_LOOKUP_SUMMARY="memory lookup disabled"
    return 0
  fi

  if ! command -v openclaw >/dev/null 2>&1; then
    MEMORY_LOOKUP_RC=1
    MEMORY_LOOKUP_SUMMARY="openclaw CLI missing"
    return "$MEMORY_LOOKUP_RC"
  fi

  local memory_timeout=15
  local memory_output
  memory_output="$(timeout "$memory_timeout" openclaw memory search "test" 2>&1)"
  local memory_rc=$?

  # Check for NODE_MODULE_VERSION mismatch (better-sqlite3 native module issue)
  if printf '%s\n' "$memory_output" | grep -qi "NODE_MODULE_VERSION\|MODULE_VERSION\|better-sqlite3"; then
    MEMORY_LOOKUP_RC=2
    MEMORY_LOOKUP_SUMMARY="memory lookup failed: Node module version mismatch (better-sqlite3)"
    return "$MEMORY_LOOKUP_RC"
  fi

  if [ "$memory_rc" -ne 0 ]; then
    MEMORY_LOOKUP_RC=3
    MEMORY_LOOKUP_SUMMARY="memory lookup command failed (rc=$memory_rc)"
    return "$MEMORY_LOOKUP_RC"
  fi

  # Check if we got results (look for score prefix like "0.531" at line start)
  if printf '%s\n' "$memory_output" | grep -qE '^\s*[0-9]+\.'; then
    MEMORY_LOOKUP_RC=0
    MEMORY_LOOKUP_SUMMARY="memory lookup returned results"
  elif printf '%s\n' "$memory_output" | grep -qi "No matches"; then
    # "No matches" means search works but corpus is empty - this is OK
    MEMORY_LOOKUP_RC=0
    MEMORY_LOOKUP_SUMMARY="memory lookup functional (corpus empty)"
  else
    MEMORY_LOOKUP_RC=4
    MEMORY_LOOKUP_SUMMARY="memory lookup returned unexpected output"
    return "$MEMORY_LOOKUP_RC"
  fi

  return 0
}

THREAD_REPLY_RC=0
THREAD_REPLY_SUMMARY="thread reply check not run"

run_thread_reply_probe() {
  THREAD_REPLY_RC=0
  THREAD_REPLY_SUMMARY="thread reply check passed"

  if [ "$THREAD_REPLY_CHECK_ENABLED" != "1" ]; then
    THREAD_REPLY_SUMMARY="thread reply check disabled"
    return 0
  fi
  if [ -z "${SLACK_USER_TOKEN:-}" ]; then
    THREAD_REPLY_RC=3
    THREAD_REPLY_SUMMARY="SLACK_USER_TOKEN missing for thread reply probe"
    return "$THREAD_REPLY_RC"
  fi
  if ! command -v jq >/dev/null 2>&1; then
    THREAD_REPLY_RC=8
    THREAD_REPLY_SUMMARY="jq missing; thread reply probe skipped"
    return "$THREAD_REPLY_RC"
  fi

  local now oldest_ts history_output history_ok history_error
  now="$(date +%s)"
  oldest_ts=$(( now - THREAD_REPLY_LOOKBACK_SECONDS ))
  history_output="$(
    curl -sS -G "$SLACK_API_BASE/conversations.history" \
      -H "Authorization: Bearer $SLACK_USER_TOKEN" \
      --data-urlencode "channel=$THREAD_REPLY_CHANNEL" \
      --data-urlencode "oldest=$oldest_ts" \
      --data-urlencode "inclusive=true" \
      --data-urlencode "limit=200" 2>&1
  )"
  history_ok="$(printf '%s\n' "$history_output" | jq -r '.ok // false' 2>/dev/null || printf 'false')"
  if [ "$history_ok" != "true" ]; then
    history_error="$(printf '%s\n' "$history_output" | jq -r '.error // empty' 2>/dev/null || true)"
    [ -z "$history_error" ] && history_error="$(printf '%s\n' "$history_output" | tr '\n' ' ' | sed 's/[[:space:]]\+/ /g' | cut -c1-180)"
    THREAD_REPLY_RC=4
    THREAD_REPLY_SUMMARY="thread history failed channel=$THREAD_REPLY_CHANNEL error=$history_error"
    return "$THREAD_REPLY_RC"
  fi

  local resolved_bot_user bot_auth_output
  resolved_bot_user="$THREAD_REPLY_BOT_USER_ID"
  if [ -z "$resolved_bot_user" ] && [ -n "${SLACK_BOT_TOKEN:-}" ]; then
    bot_auth_output="$(
      curl -sS -X POST "$SLACK_API_BASE/auth.test" \
        -H "Authorization: Bearer $SLACK_BOT_TOKEN" \
        -H "Content-Type: application/x-www-form-urlencoded" 2>&1
    )"
    resolved_bot_user="$(printf '%s\n' "$bot_auth_output" | jq -r '.user_id // empty' 2>/dev/null || true)"
  fi

  local thread_candidates
  thread_candidates="$(
    {
      if [ -n "$THREAD_REPLY_WATCH_THREADS" ]; then
        printf '%s\n' "$THREAD_REPLY_WATCH_THREADS" | tr ',' '\n'
      fi
      printf '%s\n' "$history_output" | jq -r '.messages[]? | select((.reply_count // 0) > 0) | (.thread_ts // .ts // empty)' 2>/dev/null
    } | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' | awk 'NF && !seen[$0]++'
  )"

  if [ -z "$thread_candidates" ]; then
    THREAD_REPLY_RC=0
    THREAD_REPLY_SUMMARY="no active threaded conversations in channel=$THREAD_REPLY_CHANNEL lookback=${THREAD_REPLY_LOOKBACK_SECONDS}s"
    return 0
  fi

  local checked=0 issues=0 details=""
  local thread_ts replies_output replies_ok replies_error
  local latest_human_ts latest_bot_ts latest_failure_ts
  local human_age failure_age needs_reply failure_is_latest
  while IFS= read -r thread_ts; do
    [ -z "$thread_ts" ] && continue
    if [ "$checked" -ge "$THREAD_REPLY_MAX_THREADS" ]; then
      break
    fi
    checked=$((checked + 1))

    replies_output="$(
      curl -sS -G "$SLACK_API_BASE/conversations.replies" \
        -H "Authorization: Bearer $SLACK_USER_TOKEN" \
        --data-urlencode "channel=$THREAD_REPLY_CHANNEL" \
        --data-urlencode "ts=$thread_ts" \
        --data-urlencode "limit=80" 2>&1
    )"
    replies_ok="$(printf '%s\n' "$replies_output" | jq -r '.ok // false' 2>/dev/null || printf 'false')"
    if [ "$replies_ok" != "true" ]; then
      replies_error="$(printf '%s\n' "$replies_output" | jq -r '.error // empty' 2>/dev/null || true)"
      [ -z "$replies_error" ] && replies_error="$(printf '%s\n' "$replies_output" | tr '\n' ' ' | sed 's/[[:space:]]\+/ /g' | cut -c1-160)"
      issues=$((issues + 1))
      details="${details} thread=$thread_ts api_error=$replies_error;"
      continue
    fi

    latest_human_ts="$(printf '%s\n' "$replies_output" | jq -r --arg bot "$resolved_bot_user" '
      [ .messages[]?
        | select((.ts // "") != "")
        | select((.subtype // "") != "bot_message")
        | select((.bot_id // "") == "")
        | select((.user // "") != "")
        | select($bot == "" or .user != $bot)
        | (.ts | tonumber)
      ] | max // 0
    ' 2>/dev/null || printf '0')"
    latest_bot_ts="$(printf '%s\n' "$replies_output" | jq -r --arg bot "$resolved_bot_user" '
      [ .messages[]?
        | select((.ts // "") != "")
        | select((.bot_id // "") != "" or (.subtype // "") == "bot_message" or ($bot != "" and (.user // "") == $bot))
        | (.ts | tonumber)
      ] | max // 0
    ' 2>/dev/null || printf '0')"
    latest_failure_ts="$(printf '%s\n' "$replies_output" | jq -r --arg rx "$THREAD_REPLY_FAILURE_REGEX" '
      [ .messages[]?
        | select((.ts // "") != "")
        | select(((.text // "") | test($rx; "i")))
        | (.ts | tonumber)
      ] | max // 0
    ' 2>/dev/null || printf '0')"

    failure_is_latest="$(awk -v f="$latest_failure_ts" -v b="$latest_bot_ts" 'BEGIN { if (f > 0 && f >= b) print "1"; else print "0"; }')"
    if [ "$failure_is_latest" = "1" ]; then
      failure_age=$(( now - ${latest_failure_ts%.*} ))
      if [ "$failure_age" -le "$THREAD_REPLY_FAILURE_MAX_AGE_SECONDS" ]; then
        issues=$((issues + 1))
        details="${details} thread=$thread_ts recent_failure_marker_age_s=$failure_age;"
      fi
    fi

    needs_reply="$(awk -v h="$latest_human_ts" -v b="$latest_bot_ts" 'BEGIN { if (h > b) print "1"; else print "0"; }')"

    if [ "$needs_reply" = "1" ]; then
      human_age=$(( now - ${latest_human_ts%.*} ))
      if [ "$human_age" -gt "$THREAD_REPLY_GRACE_SECONDS" ]; then
        issues=$((issues + 1))
        details="${details} thread=$thread_ts unanswered_human_age_s=$human_age;"
      fi
    fi
  done <<< "$thread_candidates"

  if [ "$issues" -gt 0 ]; then
    THREAD_REPLY_RC=7
    THREAD_REPLY_SUMMARY="issues=$issues checked=$checked channel=$THREAD_REPLY_CHANNEL details:$(printf '%s\n' "$details" | cut -c1-300)"
  else
    THREAD_REPLY_RC=0
    THREAD_REPLY_SUMMARY="checked=$checked channel=$THREAD_REPLY_CHANNEL unresolved=0"
  fi
  return "$THREAD_REPLY_RC"
}

run_token_probes || true
run_thread_reply_probe || true
run_memory_lookup_probe || true

PHASE1_REMEDIATION_ACTIONS=()
if [ "$PHASE1_REMEDIATION_ENABLED" = "1" ]; then
  if [ "$HTTP_GATEWAY_RC" -ne 0 ]; then
    # SAFE: use launchctl directly — never 'gateway restart/install' which may regenerate plist and wipe real secrets
    launchctl unload "$HOME/Library/LaunchAgents/ai.smartclaw.gateway.plist" >> "$LOG_FILE" 2>&1 || true
    sleep 1
    if launchctl load "$HOME/Library/LaunchAgents/ai.smartclaw.gateway.plist" >> "$LOG_FILE" 2>&1; then
      PHASE1_REMEDIATION_ACTIONS+=("gateway_restart_ok")
    else
      PHASE1_REMEDIATION_ACTIONS+=("gateway_restart_failed")
    fi
    sleep 3
  fi

  if [ "$PROBE_REQUEST_RC" -ne 0 ] || [ "$GATEWAY_PROBE_RC" -ne 0 ]; then
    if launchctl kickstart -k "gui/$(id -u)/ai.smartclaw.gateway" >> "$LOG_FILE" 2>&1; then
      PHASE1_REMEDIATION_ACTIONS+=("launchctl_kickstart_gateway_ok")
    else
      PHASE1_REMEDIATION_ACTIONS+=("launchctl_kickstart_gateway_failed")
    fi
    sleep 3
  fi
fi

if [ "${#PHASE1_REMEDIATION_ACTIONS[@]}" -gt 0 ]; then
  PROBE_REQUEST_OUTPUT="$("$OPENCLAW_BIN" message read --channel slack --target "$PROBE_SLACK_TARGET" --limit 1 --json 2>&1)"
  PROBE_REQUEST_RC=$?
  PROBE_REQUEST_SUMMARY="$(printf '%s\n' "$PROBE_REQUEST_OUTPUT" | rg -m1 '"ts"|"timestampUtc"|"thread_ts"|^Error|^gateway connect failed' || true)"
  if [ -z "$PROBE_REQUEST_SUMMARY" ]; then
    PROBE_REQUEST_SUMMARY="$(printf '%s\n' "$PROBE_REQUEST_OUTPUT" | head -n 1)"
  fi
  PROBE_REQUEST_SUMMARY="$(printf '%s\n' "$PROBE_REQUEST_SUMMARY" | tr '\n' ' ' | sed 's/[[:space:]]\+/ /g' | cut -c1-240)"

  GATEWAY_PROBE_TEXT="OpenClaw monitor recheck after phase 1: $(date '+%Y-%m-%d %H:%M:%S %Z')"
  GATEWAY_PROBE_OUTPUT="$("$OPENCLAW_BIN" message send --channel slack --target "$GATEWAY_PROBE_TARGET" --message "$GATEWAY_PROBE_TEXT" --json 2>&1)"
  GATEWAY_PROBE_RC=$?
  GATEWAY_PROBE_SUMMARY="$(printf '%s\n' "$GATEWAY_PROBE_OUTPUT" | rg -m1 '"messageId"|"ts"|"ok"|^Error|^gateway connect failed' || true)"
  if [ -z "$GATEWAY_PROBE_SUMMARY" ]; then
    GATEWAY_PROBE_SUMMARY="$(printf '%s\n' "$GATEWAY_PROBE_OUTPUT" | head -n 1)"
  fi
  GATEWAY_PROBE_SUMMARY="$(printf '%s\n' "$GATEWAY_PROBE_SUMMARY" | tr '\n' ' ' | sed 's/[[:space:]]\+/ /g' | cut -c1-240)"

  HTTP_GATEWAY_OUTPUT="$(
    curl -sS -X GET "$HTTP_GATEWAY_URL" \
      -H "X-OpenClaw-Monitor-Message: [monitor-http-probe-post-phase1] $(date '+%Y-%m-%d %H:%M:%S %Z')" \
      -H "Accept: application/json" \
      -w '\nHTTP_STATUS:%{http_code}' 2>&1
  )"
  HTTP_GATEWAY_RC=$?
  HTTP_GATEWAY_SUMMARY="$(printf '%s\n' "$HTTP_GATEWAY_OUTPUT" | rg -m1 'HTTP_STATUS:|\"ok\"|\"status\"|^curl:|^Error' || true)"
  if [ -z "$HTTP_GATEWAY_SUMMARY" ]; then
    HTTP_GATEWAY_SUMMARY="$(printf '%s\n' "$HTTP_GATEWAY_OUTPUT" | head -n 1)"
  fi
  HTTP_GATEWAY_SUMMARY="$(printf '%s\n' "$HTTP_GATEWAY_SUMMARY" | tr '\n' ' ' | sed 's/[[:space:]]\+/ /g' | cut -c1-240)"
  HTTP_GATEWAY_STATUS="$(printf '%s\n' "$HTTP_GATEWAY_OUTPUT" | rg -o 'HTTP_STATUS:[0-9]+' | tail -n1 | cut -d: -f2)"
  if [ -z "$HTTP_GATEWAY_STATUS" ]; then
    HTTP_GATEWAY_STATUS="0"
  fi
  if [ "$HTTP_GATEWAY_STATUS" -lt 200 ] || [ "$HTTP_GATEWAY_STATUS" -ge 300 ]; then
    HTTP_GATEWAY_RC=1
  fi

  run_token_probes || true
  run_thread_reply_probe || true
  run_memory_lookup_probe || true
fi

SLACK_CANARY_TEXT="[monitor-e2e-canary] $(date '+%Y-%m-%d %H:%M:%S %Z')"
SLACK_CANARY_RC=0
SLACK_CANARY_SUMMARY="canary skipped"
SLACK_CANARY_THREAD_TS=""

if [ "$RUN_CANARY" = "1" ]; then
  if [ -z "${SLACK_USER_TOKEN:-}" ]; then
    SLACK_CANARY_RC=2
    SLACK_CANARY_SUMMARY="SLACK_USER_TOKEN missing; cannot run inbound Slack E2E canary"
  else
    SLACK_POST_OUTPUT="$(
      curl -sS -X POST "$SLACK_API_BASE/chat.postMessage" \
        -H "Authorization: Bearer $SLACK_USER_TOKEN" \
        -H "Content-Type: application/json; charset=utf-8" \
        -d "{\"channel\":\"$ALERT_SLACK_TARGET\",\"text\":\"$SLACK_CANARY_TEXT\"}" 2>&1
    )"
    SLACK_POST_OK="$(printf '%s\n' "$SLACK_POST_OUTPUT" | rg -o '"ok":[^,]+' | head -n1 | cut -d: -f2 | tr -d ' ')"
    SLACK_CANARY_THREAD_TS="$(printf '%s\n' "$SLACK_POST_OUTPUT" | rg -o '"ts":"[^"]+"' | head -n1 | cut -d'"' -f4)"

    if [ "$SLACK_POST_OK" != "true" ] || [ -z "$SLACK_CANARY_THREAD_TS" ]; then
      SLACK_CANARY_RC=3
      SLACK_CANARY_SUMMARY="canary post failed: $(printf '%s\n' "$SLACK_POST_OUTPUT" | tr '\n' ' ' | sed 's/[[:space:]]\+/ /g' | cut -c1-220)"
    else
      CANARY_DEADLINE=$(( $(date +%s) + CANARY_TIMEOUT_SECONDS ))
      SLACK_CANARY_RC=4
      SLACK_CANARY_SUMMARY="canary posted (ts=$SLACK_CANARY_THREAD_TS), waiting for agent thread reply timed out"

      while [ "$(date +%s)" -lt "$CANARY_DEADLINE" ]; do
        SLACK_REPLIES_OUTPUT="$(
          curl -sS -G "$SLACK_API_BASE/conversations.replies" \
            -H "Authorization: Bearer $SLACK_USER_TOKEN" \
            --data-urlencode "channel=$ALERT_SLACK_TARGET" \
            --data-urlencode "ts=$SLACK_CANARY_THREAD_TS" \
            --data-urlencode "limit=20" 2>&1
        )"

        if printf '%s\n' "$SLACK_REPLIES_OUTPUT" | rg -q '"ok":true'; then
          if printf '%s\n' "$SLACK_REPLIES_OUTPUT" | rg -q 'Agent failed before reply|all models failed|authentication_error|OAuth token refresh failed'; then
            SLACK_CANARY_RC=5
            SLACK_CANARY_SUMMARY="canary thread has agent failure reply"
            break
          fi

          REPLY_COUNT="$(printf '%s\n' "$SLACK_REPLIES_OUTPUT" | rg -o '"ts":"[^"]+"' | wc -l | tr -d ' ')"
          if [ "${REPLY_COUNT:-0}" -gt 1 ]; then
            SLACK_CANARY_RC=0
            SLACK_CANARY_SUMMARY="canary reply received in thread (ts=$SLACK_CANARY_THREAD_TS)"
            break
          fi
        fi

        sleep "$CANARY_POLL_INTERVAL_SECONDS"
      done
    fi
  fi
fi

FORCE_REASONS=()
collect_force_reasons() {
  FORCE_REASONS=()
  [ "$PROBE_REQUEST_RC" -ne 0 ] && FORCE_REASONS+=("slack_read_probe rc=$PROBE_REQUEST_RC")
  [ "$GATEWAY_PROBE_RC" -ne 0 ] && FORCE_REASONS+=("slack_send_probe rc=$GATEWAY_PROBE_RC")
  [ "$HTTP_GATEWAY_RC" -ne 0 ] && FORCE_REASONS+=("http_gateway_probe rc=$HTTP_GATEWAY_RC status=$HTTP_GATEWAY_STATUS")
  [ "$SLACK_CANARY_RC" -ne 0 ] && FORCE_REASONS+=("slack_inbound_e2e rc=$SLACK_CANARY_RC summary=$SLACK_CANARY_SUMMARY")
  [ "$THREAD_REPLY_RC" -ne 0 ] && FORCE_REASONS+=("slack_thread_reply_probe rc=$THREAD_REPLY_RC summary=$THREAD_REPLY_SUMMARY")
  [ "$TOKEN_PROBE_RC" -eq 1 ] && FORCE_REASONS+=("token_probes rc=$TOKEN_PROBE_RC summary=$TOKEN_PROBE_SUMMARY")
  # Memory lookup: only critical failures (module mismatch, command failed) trigger alert
  # Empty results (RC=4) is a warning but not a hard failure (corpus may be empty)
  [ "$MEMORY_LOOKUP_RC" -eq 2 ] && FORCE_REASONS+=("memory_lookup rc=$MEMORY_LOOKUP_RC summary=$MEMORY_LOOKUP_SUMMARY")
  [ "$MEMORY_LOOKUP_RC" -eq 3 ] && FORCE_REASONS+=("memory_lookup rc=$MEMORY_LOOKUP_RC summary=$MEMORY_LOOKUP_SUMMARY")
}

collect_force_reasons
FORCE_PROBLEM=0
if [ "${#FORCE_REASONS[@]}" -gt 0 ]; then
  FORCE_PROBLEM=1
fi

PHASE2_RC=0
PHASE2_OUTPUT=""
PHASE2_REMEDIATION_ACTIONS=()
if [ "$FORCE_PROBLEM" -eq 1 ] && [ "$PHASE2_ENABLED" = "1" ]; then
  PHASE2_MODE="diagnose_only"
  if [ "$PHASE2_AUTOFIX_ENABLED" = "1" ]; then
    PHASE2_MODE="diagnose_and_fix"
  fi

  PHASE2_CONFIG_RULE="Do NOT run config-mutating commands (openclaw doctor, openclaw config set, cp/mv/jq edits on ~/.smartclaw/openclaw.json)."
  if [ "$PHASE2_ALLOW_CONFIG_MUTATIONS" = "1" ]; then
    PHASE2_CONFIG_RULE="Config mutation is allowed, but only if directly required for the unresolved failures."
  fi

  PHASE2_PROMPT="You are Phase 2 monitor remediation.
Phase 1 is deterministic and already ran. Only work on unresolved issues below.

Unresolved issues:
${FORCE_REASONS[*]}

Current probe evidence:
- slack_read_probe rc=$PROBE_REQUEST_RC summary=$PROBE_REQUEST_SUMMARY
- slack_send_probe rc=$GATEWAY_PROBE_RC summary=$GATEWAY_PROBE_SUMMARY
- http_gateway_probe rc=$HTTP_GATEWAY_RC status=$HTTP_GATEWAY_STATUS summary=$HTTP_GATEWAY_SUMMARY
- slack_thread_reply_probe rc=$THREAD_REPLY_RC summary=$THREAD_REPLY_SUMMARY
- token_probes rc=$TOKEN_PROBE_RC summary=$TOKEN_PROBE_SUMMARY
- slack_canary rc=$SLACK_CANARY_RC summary=$SLACK_CANARY_SUMMARY

Mode: $PHASE2_MODE
$PHASE2_CONFIG_RULE

If mode is diagnose_and_fix, you may run non-deterministic remediation commands, then return:
1) root cause summary
2) exact commands run
3) what improved vs still failing

If mode is diagnose_only, return:
1) root cause hypotheses ranked
2) deterministic next checks
3) minimal safe fix plan"

  # Run Phase2 in background so doctor.sh / AO doctor can run in parallel.
  _PHASE2_TMPDIR="$(mktemp -d /tmp/monitor-phase2.XXXXXX)"
  (
    out="$(timeout "$PHASE2_TIMEOUT_SECONDS" ai_orch run --agent-cli claude "$PHASE2_PROMPT" 2>&1)"
    rc=$?
    printf '%s\n' "$rc"  > "$_PHASE2_TMPDIR/rc"
    printf '%s\n' "$out" > "$_PHASE2_TMPDIR/out"
  ) &
  _PHASE2_BG_PID=$!
fi

if [ "$FORCE_PROBLEM" -eq 1 ] && [ "$PHASE2_ENABLED" = "1" ] && [ "$PHASE2_AUTOFIX_ENABLED" = "1" ] && [ "$PHASE2_RC" -eq 0 ]; then
  PROBE_REQUEST_OUTPUT="$("$OPENCLAW_BIN" message read --channel slack --target "$PROBE_SLACK_TARGET" --limit 1 --json 2>&1)"
  PROBE_REQUEST_RC=$?
  PROBE_REQUEST_SUMMARY="$(printf '%s\n' "$PROBE_REQUEST_OUTPUT" | rg -m1 '"ts"|"timestampUtc"|"thread_ts"|^Error|^gateway connect failed' || true)"
  if [ -z "$PROBE_REQUEST_SUMMARY" ]; then
    PROBE_REQUEST_SUMMARY="$(printf '%s\n' "$PROBE_REQUEST_OUTPUT" | head -n 1)"
  fi
  PROBE_REQUEST_SUMMARY="$(printf '%s\n' "$PROBE_REQUEST_SUMMARY" | tr '\n' ' ' | sed 's/[[:space:]]\+/ /g' | cut -c1-240)"

  GATEWAY_PROBE_TEXT="OpenClaw monitor recheck after phase 2: $(date '+%Y-%m-%d %H:%M:%S %Z')"
  GATEWAY_PROBE_OUTPUT="$("$OPENCLAW_BIN" message send --channel slack --target "$GATEWAY_PROBE_TARGET" --message "$GATEWAY_PROBE_TEXT" --json 2>&1)"
  GATEWAY_PROBE_RC=$?
  GATEWAY_PROBE_SUMMARY="$(printf '%s\n' "$GATEWAY_PROBE_OUTPUT" | rg -m1 '"messageId"|"ts"|"ok"|^Error|^gateway connect failed' || true)"
  if [ -z "$GATEWAY_PROBE_SUMMARY" ]; then
    GATEWAY_PROBE_SUMMARY="$(printf '%s\n' "$GATEWAY_PROBE_OUTPUT" | head -n 1)"
  fi
  GATEWAY_PROBE_SUMMARY="$(printf '%s\n' "$GATEWAY_PROBE_SUMMARY" | tr '\n' ' ' | sed 's/[[:space:]]\+/ /g' | cut -c1-240)"

  HTTP_GATEWAY_OUTPUT="$(
    curl -sS -X GET "$HTTP_GATEWAY_URL" \
      -H "X-OpenClaw-Monitor-Message: [monitor-http-probe-post-phase2] $(date '+%Y-%m-%d %H:%M:%S %Z')" \
      -H "Accept: application/json" \
      -w '\nHTTP_STATUS:%{http_code}' 2>&1
  )"
  HTTP_GATEWAY_RC=$?
  HTTP_GATEWAY_SUMMARY="$(printf '%s\n' "$HTTP_GATEWAY_OUTPUT" | rg -m1 'HTTP_STATUS:|\"ok\"|\"status\"|^curl:|^Error' || true)"
  if [ -z "$HTTP_GATEWAY_SUMMARY" ]; then
    HTTP_GATEWAY_SUMMARY="$(printf '%s\n' "$HTTP_GATEWAY_OUTPUT" | head -n 1)"
  fi
  HTTP_GATEWAY_SUMMARY="$(printf '%s\n' "$HTTP_GATEWAY_SUMMARY" | tr '\n' ' ' | sed 's/[[:space:]]\+/ /g' | cut -c1-240)"
  HTTP_GATEWAY_STATUS="$(printf '%s\n' "$HTTP_GATEWAY_OUTPUT" | rg -o 'HTTP_STATUS:[0-9]+' | tail -n1 | cut -d: -f2)"
  if [ -z "$HTTP_GATEWAY_STATUS" ]; then
    HTTP_GATEWAY_STATUS="0"
  fi
  if [ "$HTTP_GATEWAY_STATUS" -lt 200 ] || [ "$HTTP_GATEWAY_STATUS" -ge 300 ]; then
    HTTP_GATEWAY_RC=1
  fi

  run_token_probes || true
  run_thread_reply_probe || true
  run_memory_lookup_probe || true
  collect_force_reasons
  FORCE_PROBLEM=0
  if [ "${#FORCE_REASONS[@]}" -gt 0 ]; then
    FORCE_PROBLEM=1
  fi
fi

DOCTOR_SH_RAN=0
DOCTOR_SH_RC=0
DOCTOR_SH_PATH=""
DOCTOR_SH_LEVEL="skipped"
DOCTOR_SH_SUMMARY="doctor.sh skipped in this cycle"
DOCTOR_SH_OUTPUT=""
if [ "$DOCTOR_SH_ENABLED" = "1" ]; then
  SHOULD_RUN_DOCTOR_SH=0
  if [ "$DOCTOR_SH_ALWAYS" = "1" ] || [ "$FORCE_PROBLEM" -eq 1 ] || [ "$TOKEN_PROBE_RC" -eq 2 ]; then
    SHOULD_RUN_DOCTOR_SH=1
  fi

  if [ "$SHOULD_RUN_DOCTOR_SH" -eq 1 ]; then
    if DOCTOR_SH_PATH="$(resolve_doctor_sh_path)"; then
      DOCTOR_SH_RAN=1
      # Skip inference probe: monitor already runs a canary E2E test for LLM reachability.
      DOCTOR_SH_OUTPUT="$(OPENCLAW_DOCTOR_SKIP_INFERENCE=1 bash "$DOCTOR_SH_PATH" 2>&1)"
      DOCTOR_SH_RC=$?
      if [ "$DOCTOR_SH_RC" -eq 0 ]; then
        DOCTOR_SH_LEVEL="good"
      elif printf '%s\n' "$DOCTOR_SH_OUTPUT" | rg -qi '\[FAIL\]|Doctor errors|fatal|invalid_auth'; then
        DOCTOR_SH_LEVEL="bad"
      else
        DOCTOR_SH_LEVEL="warn"
      fi
      # Prefer FAIL lines for actionable summary; fall back to Summary line, then first line
      DOCTOR_SH_SUMMARY="$(printf '%s\n' "$DOCTOR_SH_OUTPUT" | rg -i '\[FAIL\]' | head -3 | tr '\n' ' ' || true)"
      if [ -z "$DOCTOR_SH_SUMMARY" ]; then
        DOCTOR_SH_SUMMARY="$(printf '%s\n' "$DOCTOR_SH_OUTPUT" | rg -m1 'Summary:' || true)"
      fi
      if [ -z "$DOCTOR_SH_SUMMARY" ]; then
        DOCTOR_SH_SUMMARY="$(printf '%s\n' "$DOCTOR_SH_OUTPUT" | head -n 1)"
      fi
      DOCTOR_SH_SUMMARY="$(printf '%s\n' "$DOCTOR_SH_SUMMARY" | tr '\n' ' ' | sed 's/[[:space:]]\+/ /g' | cut -c1-240)"
    else
      DOCTOR_SH_RAN=1
      DOCTOR_SH_RC=127
      DOCTOR_SH_LEVEL="bad"
      DOCTOR_SH_SUMMARY="doctor.sh not found. Set OPENCLAW_MONITOR_DOCTOR_SH_PATH."
    fi
  fi
fi

if [ "$DOCTOR_SH_RAN" -eq 1 ] && [ "$DOCTOR_SH_LEVEL" = "bad" ]; then
  FORCE_PROBLEM=1
  FORCE_REASONS+=("doctor_sh rc=$DOCTOR_SH_RC summary=$DOCTOR_SH_SUMMARY")
fi

# ao doctor: Agent Orchestrator environment health check
AO_DOCTOR_ENABLED="${OPENCLAW_MONITOR_AO_DOCTOR_ENABLE:-1}"
AO_DOCTOR_RAN=0
AO_DOCTOR_RC=0
AO_DOCTOR_LEVEL="skipped"
AO_DOCTOR_SUMMARY="ao doctor skipped in this cycle"
AO_DOCTOR_OUTPUT=""
if [ "$AO_DOCTOR_ENABLED" = "1" ]; then
  AO_BIN="${AO_BIN:-$HOME/bin/ao}"
  if command -v "$AO_BIN" >/dev/null 2>&1 || [ -x "$AO_BIN" ]; then
    AO_DOCTOR_RAN=1
    AO_DOCTOR_OUTPUT="$("$AO_BIN" doctor 2>&1)"
    AO_DOCTOR_RC=$?
    if [ "$AO_DOCTOR_RC" -ne 0 ] || printf '%s\n' "$AO_DOCTOR_OUTPUT" | grep -q "^FAIL"; then
      AO_DOCTOR_LEVEL="bad"
    elif printf '%s\n' "$AO_DOCTOR_OUTPUT" | grep -q "^WARN"; then
      AO_DOCTOR_LEVEL="warn"
    else
      AO_DOCTOR_LEVEL="good"
    fi
    AO_DOCTOR_SUMMARY="$(printf '%s\n' "$AO_DOCTOR_OUTPUT" | grep -E '^(FAIL|WARN|Results:)' | tr '\n' ' ' | cut -c1-240)"
    [ -z "$AO_DOCTOR_SUMMARY" ] && AO_DOCTOR_SUMMARY="$(printf '%s\n' "$AO_DOCTOR_OUTPUT" | tail -1)"
  else
    AO_DOCTOR_RAN=0
    AO_DOCTOR_LEVEL="skipped"
    AO_DOCTOR_SUMMARY="ao binary not found at $AO_BIN — skipping"
  fi
fi

if [ "$AO_DOCTOR_RAN" -eq 1 ] && [ "$AO_DOCTOR_LEVEL" = "bad" ]; then
  FORCE_PROBLEM=1
  FORCE_REASONS+=("ao_doctor rc=$AO_DOCTOR_RC summary=$AO_DOCTOR_SUMMARY")
fi

# Collect Phase2 results (was started in background while doctor.sh / AO doctor ran).
if [ "${_PHASE2_BG_PID:-}" != "" ]; then
  wait "$_PHASE2_BG_PID" 2>/dev/null || true
  PHASE2_RC="$(cat "$_PHASE2_TMPDIR/rc" 2>/dev/null || echo 124)"
  PHASE2_OUTPUT="$(cat "$_PHASE2_TMPDIR/out" 2>/dev/null || true)"
  rm -rf "$_PHASE2_TMPDIR"
  if [ "$PHASE2_RC" -eq 0 ]; then
    PHASE2_REMEDIATION_ACTIONS+=("phase2_invoked_ok mode=$PHASE2_MODE via=ai_orch")
  else
    PHASE2_REMEDIATION_ACTIONS+=("phase2_invoked_failed rc=$PHASE2_RC via=ai_orch")
  fi
fi

# Inference probe: real end-to-end LLM call through gateway
INFERENCE_PROBE_RC=0
INFERENCE_PROBE_OUTPUT=""
INFERENCE_PROBE_SUMMARY="skipped"
if [ "$INFERENCE_PROBE_ENABLED" = "1" ] && [ -n "$OPENCLAW_BIN" ]; then
  INFERENCE_PROBE_OUTPUT="$(timeout "$INFERENCE_PROBE_TIMEOUT" \
    "$OPENCLAW_BIN" agent --agent main --thinking off --timeout "$INFERENCE_PROBE_TIMEOUT" \
    --message "Reply with exactly one word: pong" 2>&1)"
  INFERENCE_PROBE_RC=$?
  if [ "$INFERENCE_PROBE_RC" -eq 0 ] && [ -n "$INFERENCE_PROBE_OUTPUT" ]; then
    INFERENCE_PROBE_SUMMARY="ok response=$(printf '%s' "$INFERENCE_PROBE_OUTPUT" | tr '\n' ' ' | cut -c1-80)"
  else
    INFERENCE_PROBE_SUMMARY="failed rc=$INFERENCE_PROBE_RC output=$(printf '%s' "$INFERENCE_PROBE_OUTPUT" | head -1 | cut -c1-120)"
    FORCE_PROBLEM=1
    FORCE_REASONS+=("inference_probe rc=$INFERENCE_PROBE_RC")
  fi
  log "Inference probe: rc=$INFERENCE_PROBE_RC summary=$INFERENCE_PROBE_SUMMARY"
fi

STATUS="GOOD"
if [ "$FORCE_PROBLEM" -eq 1 ]; then
  STATUS="PROBLEM"
fi

HUMAN_SUMMARY_LINES=()
if [ "$STATUS" = "GOOD" ]; then
  HUMAN_SUMMARY_LINES+=("All monitored checks are passing right now.")
  if [ "$TOKEN_PROBE_RC" -eq 2 ]; then
    HUMAN_SUMMARY_LINES+=("Non-blocking token warnings detected. Details are in token_probes evidence.")
  fi
  if [ "$DOCTOR_SH_RAN" -eq 1 ] && [ "$DOCTOR_SH_LEVEL" = "warn" ]; then
    HUMAN_SUMMARY_LINES+=("doctor.sh reported warnings (rc=$DOCTOR_SH_RC).")
  elif [ "$DOCTOR_SH_RAN" -eq 1 ] && [ "$DOCTOR_SH_LEVEL" = "bad" ]; then
    HUMAN_SUMMARY_LINES+=("doctor.sh reported failures (rc=$DOCTOR_SH_RC).")
  fi
else
  HUMAN_SUMMARY_LINES+=("One or more active checks are failing right now.")
  [ "$PROBE_REQUEST_RC" -ne 0 ] && HUMAN_SUMMARY_LINES+=("OpenClaw could not read recent Slack messages from channel $PROBE_SLACK_TARGET.")
  [ "$GATEWAY_PROBE_RC" -ne 0 ] && HUMAN_SUMMARY_LINES+=("OpenClaw could not send a Slack probe message to channel $GATEWAY_PROBE_TARGET.")
  [ "$HTTP_GATEWAY_RC" -ne 0 ] && HUMAN_SUMMARY_LINES+=("Gateway HTTP health probe failed (status=$HTTP_GATEWAY_STATUS).")
  [ "$THREAD_REPLY_RC" -ne 0 ] && HUMAN_SUMMARY_LINES+=("Thread reply check found an unanswered human message or a recent failure marker in channel $THREAD_REPLY_CHANNEL.")
  [ "$TOKEN_PROBE_RC" -eq 1 ] && HUMAN_SUMMARY_LINES+=("At least one required token probe failed. See token_probes evidence for the exact token path.")
  [ "$SLACK_CANARY_RC" -ne 0 ] && HUMAN_SUMMARY_LINES+=("Slack inbound E2E canary failed in this run.")
  [ "$DOCTOR_SH_RAN" -eq 1 ] && [ "$DOCTOR_SH_LEVEL" = "warn" ] && HUMAN_SUMMARY_LINES+=("doctor.sh reported warnings (rc=$DOCTOR_SH_RC).")
  [ "$DOCTOR_SH_RAN" -eq 1 ] && [ "$DOCTOR_SH_LEVEL" = "bad" ] && HUMAN_SUMMARY_LINES+=("doctor.sh reported failures (rc=$DOCTOR_SH_RC).")
  [ "$AO_DOCTOR_RAN" -eq 1 ] && [ "$AO_DOCTOR_LEVEL" = "warn" ] && HUMAN_SUMMARY_LINES+=("ao doctor reported warnings: $AO_DOCTOR_SUMMARY")
  [ "$AO_DOCTOR_RAN" -eq 1 ] && [ "$AO_DOCTOR_LEVEL" = "bad" ] && HUMAN_SUMMARY_LINES+=("ao doctor reported failures: $AO_DOCTOR_SUMMARY")
fi

REPORT="STATUS=$STATUS
PHASE1_MODE=deterministic
PHASE1_REMEDIATION_ENABLED=$PHASE1_REMEDIATION_ENABLED
PHASE2_ENABLED=$PHASE2_ENABLED
PHASE2_AUTOFIX_ENABLED=$PHASE2_AUTOFIX_ENABLED
HUMAN SUMMARY:"
for human_line in "${HUMAN_SUMMARY_LINES[@]}"; do
  REPORT="${REPORT}
- $human_line"
done

REPORT="${REPORT}
ACTIVE EVIDENCE:
- slack_read_probe rc=$PROBE_REQUEST_RC summary=$PROBE_REQUEST_SUMMARY
- slack_send_probe rc=$GATEWAY_PROBE_RC summary=$GATEWAY_PROBE_SUMMARY
- http_gateway_probe rc=$HTTP_GATEWAY_RC status=$HTTP_GATEWAY_STATUS summary=$HTTP_GATEWAY_SUMMARY
- slack_thread_reply_probe rc=$THREAD_REPLY_RC summary=$THREAD_REPLY_SUMMARY
- token_probes rc=$TOKEN_PROBE_RC summary=$TOKEN_PROBE_SUMMARY
- memory_lookup rc=$MEMORY_LOOKUP_RC summary=$MEMORY_LOOKUP_SUMMARY
- slack_canary rc=$SLACK_CANARY_RC summary=$SLACK_CANARY_SUMMARY
- doctor_sh ran=$DOCTOR_SH_RAN level=$DOCTOR_SH_LEVEL rc=$DOCTOR_SH_RC summary=$DOCTOR_SH_SUMMARY
- ao_doctor ran=$AO_DOCTOR_RAN level=$AO_DOCTOR_LEVEL rc=$AO_DOCTOR_RC summary=$AO_DOCTOR_SUMMARY
PHASE1 ACTIONS:"
if [ "${#PHASE1_REMEDIATION_ACTIONS[@]}" -eq 0 ]; then
  REPORT="${REPORT}
- none"
else
  for action in "${PHASE1_REMEDIATION_ACTIONS[@]}"; do
    REPORT="${REPORT}
- $action"
  done
fi

REPORT="${REPORT}
PHASE2 ACTIONS:"
if [ "${#PHASE2_REMEDIATION_ACTIONS[@]}" -eq 0 ]; then
  REPORT="${REPORT}
- none"
else
  for action in "${PHASE2_REMEDIATION_ACTIONS[@]}"; do
    REPORT="${REPORT}
- $action"
  done
fi

REPORT="${REPORT}
ACTIVE PROBLEMS:"
if [ "${#FORCE_REASONS[@]}" -eq 0 ]; then
  REPORT="${REPORT}
- none"
else
  for reason in "${FORCE_REASONS[@]}"; do
    REPORT="${REPORT}
- $reason"
  done
fi

if [ -n "$PHASE2_OUTPUT" ]; then
  PHASE2_OUTPUT_TRIMMED="$(printf '%s\n' "$PHASE2_OUTPUT" | tail -c 4000)"
  REPORT="${REPORT}
PHASE2 OUTPUT:
$PHASE2_OUTPUT_TRIMMED"
fi

SLACK_REPORT_TIME="$(date '+%Y-%m-%d %H:%M:%S %Z')"

ICON_GREEN="🟢"
ICON_YELLOW="🟡"
ICON_RED="🔴"

SLACK_READ_STATUS="${ICON_GREEN} OK"
[ "$PROBE_REQUEST_RC" -ne 0 ] && SLACK_READ_STATUS="${ICON_RED} FAILED (rc=$PROBE_REQUEST_RC)"

SLACK_SEND_STATUS="${ICON_GREEN} OK"
[ "$GATEWAY_PROBE_RC" -ne 0 ] && SLACK_SEND_STATUS="${ICON_RED} FAILED (rc=$GATEWAY_PROBE_RC)"

HTTP_GATEWAY_STATUS_TEXT="${ICON_GREEN} OK"
[ "$HTTP_GATEWAY_RC" -ne 0 ] && HTTP_GATEWAY_STATUS_TEXT="${ICON_RED} FAILED (rc=$HTTP_GATEWAY_RC, http=$HTTP_GATEWAY_STATUS)"

THREAD_REPLY_STATUS_TEXT="${ICON_GREEN} OK"
[ "$THREAD_REPLY_RC" -ne 0 ] && THREAD_REPLY_STATUS_TEXT="${ICON_RED} FAILED (rc=$THREAD_REPLY_RC)"

TOKEN_PROBE_STATUS_TEXT="${ICON_GREEN} OK"
if [ "$TOKEN_PROBE_RC" -eq 1 ]; then
  TOKEN_PROBE_STATUS_TEXT="${ICON_RED} FAILED (required token missing/placeholder)"
elif [ "$TOKEN_PROBE_RC" -eq 2 ]; then
  TOKEN_PROBE_STATUS_TEXT="${ICON_YELLOW} WARNINGS ONLY"
fi

CANARY_STATUS_TEXT="${ICON_YELLOW} Skipped"
if [ "$RUN_CANARY" = "1" ]; then
  if [ "$SLACK_CANARY_RC" -eq 0 ]; then
    CANARY_STATUS_TEXT="${ICON_GREEN} OK"
  else
    CANARY_STATUS_TEXT="${ICON_RED} FAILED (rc=$SLACK_CANARY_RC)"
  fi
fi

DOCTOR_SH_STATUS_TEXT="${ICON_YELLOW} Skipped"
if [ "$DOCTOR_SH_RAN" -eq 1 ]; then
  if [ "$DOCTOR_SH_LEVEL" = "good" ]; then
    DOCTOR_SH_STATUS_TEXT="${ICON_GREEN} OK"
  elif [ "$DOCTOR_SH_LEVEL" = "warn" ]; then
    DOCTOR_SH_STATUS_TEXT="${ICON_YELLOW} WARNINGS (rc=$DOCTOR_SH_RC)"
  else
    DOCTOR_SH_STATUS_TEXT="${ICON_RED} FAILED (rc=$DOCTOR_SH_RC)"
  fi
fi

WARNING_STATE=0
if [ "$TOKEN_PROBE_RC" -eq 2 ]; then
  WARNING_STATE=1
fi
if [ "$DOCTOR_SH_LEVEL" = "warn" ]; then
  WARNING_STATE=1
fi

OVERALL_STATUS_TEXT="${ICON_GREEN} GOOD"
if [ "$STATUS" = "PROBLEM" ]; then
  OVERALL_STATUS_TEXT="${ICON_RED} PROBLEM"
elif [ "$WARNING_STATE" -eq 1 ]; then
  OVERALL_STATUS_TEXT="${ICON_YELLOW} WARNING"
fi

ISSUE_LINES=()
ACTION_LINES=()

if [ "$PROBE_REQUEST_RC" -ne 0 ]; then
  ISSUE_LINES+=("${ICON_RED} OpenClaw could not read recent Slack messages from $PROBE_SLACK_TARGET.")
  ACTION_LINES+=("${ICON_RED} Verify Slack auth and gateway connectivity for Slack reads.")
fi
if [ "$GATEWAY_PROBE_RC" -ne 0 ]; then
  ISSUE_LINES+=("${ICON_RED} OpenClaw could not send Slack probe messages to $GATEWAY_PROBE_TARGET.")
  ACTION_LINES+=("${ICON_RED} Verify Slack auth and gateway connectivity for Slack sends.")
fi
if [ "$HTTP_GATEWAY_RC" -ne 0 ]; then
  ISSUE_LINES+=("${ICON_RED} Gateway health endpoint probe failed (http=$HTTP_GATEWAY_STATUS).")
  ACTION_LINES+=("${ICON_RED} Inspect gateway process and logs, then re-run health probes.")
fi
if [ "$THREAD_REPLY_RC" -ne 0 ]; then
  ISSUE_LINES+=("${ICON_RED} At least one human thread in $THREAD_REPLY_CHANNEL is waiting for a reply.")
  ACTION_LINES+=("${ICON_RED} Reply in the flagged thread(s) shown in thread probe details.")
fi
if [ "$TOKEN_PROBE_RC" -eq 1 ]; then
  ISSUE_LINES+=("${ICON_RED} A required token is missing or placeholder.")
  ACTION_LINES+=("${ICON_RED} Set required tokens listed in token probe details.")
elif [ "$TOKEN_PROBE_RC" -eq 2 ]; then
  ISSUE_LINES+=("${ICON_YELLOW} Non-blocking token warnings are present.")
  ACTION_LINES+=("${ICON_YELLOW} Fill optional token values if those features are needed.")
fi
if [ "$RUN_CANARY" = "1" ] && [ "$SLACK_CANARY_RC" -ne 0 ]; then
  ISSUE_LINES+=("${ICON_RED} Slack inbound canary failed.")
  ACTION_LINES+=("${ICON_RED} Check inbound Slack routing and agent reply path.")
fi
if [ "$DOCTOR_SH_RAN" -eq 1 ] && [ "$DOCTOR_SH_LEVEL" = "bad" ]; then
  ISSUE_LINES+=("${ICON_RED} doctor.sh diagnostics failed.")
  ACTION_LINES+=("${ICON_RED} Review doctor.sh output and remediate the reported checks.")
elif [ "$DOCTOR_SH_RAN" -eq 1 ] && [ "$DOCTOR_SH_LEVEL" = "warn" ]; then
  ISSUE_LINES+=("${ICON_YELLOW} doctor.sh returned warnings.")
  ACTION_LINES+=("${ICON_YELLOW} Review doctor.sh warnings and decide whether to address now.")
fi
if [ "$PHASE2_ENABLED" = "1" ] && [ "$PHASE2_RC" -ne 0 ]; then
  ISSUE_LINES+=("${ICON_RED} Phase 2 remediation did not run successfully.")
  ACTION_LINES+=("${ICON_RED} Fix monitor phase 2 invocation and rerun monitor cycle.")
fi

# Build sorted check tables: RED → YELLOW → GREEN (important only)
RED_ROWS=()
YELLOW_ROWS=()
GREEN_ROWS=()

_row() { printf '%-22s  %s' "$1" "$2"; }

[ "$HTTP_GATEWAY_RC"   -ne 0 ] && RED_ROWS+=("$(_row "Gateway health"    "$HTTP_GATEWAY_STATUS_TEXT")")  || GREEN_ROWS+=("$(_row "Gateway health"    "$HTTP_GATEWAY_STATUS_TEXT")")
[ "$PROBE_REQUEST_RC"  -ne 0 ] && RED_ROWS+=("$(_row "Slack read"        "$SLACK_READ_STATUS")")         || GREEN_ROWS+=("$(_row "Slack read"        "$SLACK_READ_STATUS")")
[ "$GATEWAY_PROBE_RC"  -ne 0 ] && RED_ROWS+=("$(_row "Slack send"        "$SLACK_SEND_STATUS")")         || GREEN_ROWS+=("$(_row "Slack send"        "$SLACK_SEND_STATUS")")
[ "$THREAD_REPLY_RC"   -ne 0 ] && RED_ROWS+=("$(_row "Thread replies"    "$THREAD_REPLY_STATUS_TEXT")")  || GREEN_ROWS+=("$(_row "Thread replies"    "$THREAD_REPLY_STATUS_TEXT")")
if [ "$TOKEN_PROBE_RC" -eq 1 ]; then
  RED_ROWS+=("$(_row "Token probes" "$TOKEN_PROBE_STATUS_TEXT")")
elif [ "$TOKEN_PROBE_RC" -eq 2 ]; then
  YELLOW_ROWS+=("$(_row "Token probes" "$TOKEN_PROBE_STATUS_TEXT")")
else
  GREEN_ROWS+=("$(_row "Token probes" "$TOKEN_PROBE_STATUS_TEXT")")
fi
if [ "$DOCTOR_SH_RAN" -eq 1 ]; then
  if [ "$DOCTOR_SH_LEVEL" = "good" ]; then
    GREEN_ROWS+=("$(_row "doctor.sh" "$DOCTOR_SH_STATUS_TEXT")")
  elif [ "$DOCTOR_SH_LEVEL" = "warn" ]; then
    YELLOW_ROWS+=("$(_row "doctor.sh" "$DOCTOR_SH_STATUS_TEXT")")
  else
    RED_ROWS+=("$(_row "doctor.sh" "$DOCTOR_SH_STATUS_TEXT")")
  fi
fi
if [ "$RUN_CANARY" = "1" ]; then
  [ "$SLACK_CANARY_RC" -ne 0 ] && RED_ROWS+=("$(_row "Canary" "$CANARY_STATUS_TEXT")") || GREEN_ROWS+=("$(_row "Canary" "$CANARY_STATUS_TEXT")")
fi
# AO Doctor (Agent Orchestrator) check - explicit RED/YELLOW/GREEN rows
AO_DOCTOR_STATUS_TEXT="${ICON_YELLOW} Skipped"
if [ "$AO_DOCTOR_RAN" -eq 1 ]; then
  if [ "$AO_DOCTOR_LEVEL" = "good" ]; then
    AO_DOCTOR_STATUS_TEXT="${ICON_GREEN} OK"
    GREEN_ROWS+=("$(_row "AO (agento)" "$AO_DOCTOR_STATUS_TEXT")")
  elif [ "$AO_DOCTOR_LEVEL" = "warn" ]; then
    AO_DOCTOR_STATUS_TEXT="${ICON_YELLOW} WARNINGS (rc=$AO_DOCTOR_RC)"
    YELLOW_ROWS+=("$(_row "AO (agento)" "$AO_DOCTOR_STATUS_TEXT")")
  else
    AO_DOCTOR_STATUS_TEXT="${ICON_RED} FAILED (rc=$AO_DOCTOR_RC)"
    RED_ROWS+=("$(_row "AO (agento)" "$AO_DOCTOR_STATUS_TEXT")")
  fi
fi

# Memory check - explicit RED/YELLOW/GREEN rows
MEMORY_STATUS_TEXT="${ICON_YELLOW} Skipped"
if [ "$MEMORY_LOOKUP_RC" -eq 0 ]; then
  MEMORY_STATUS_TEXT="${ICON_GREEN} OK"
  GREEN_ROWS+=("$(_row "Memory" "$MEMORY_STATUS_TEXT")")
elif [ "$MEMORY_LOOKUP_RC" -eq 4 ]; then
  # RC=4 means unexpected output but not critical - show as warning
  MEMORY_STATUS_TEXT="${ICON_YELLOW} WARN (unexpected output)"
  YELLOW_ROWS+=("$(_row "Memory" "$MEMORY_STATUS_TEXT")")
else
  MEMORY_STATUS_TEXT="${ICON_RED} FAILED (rc=$MEMORY_LOOKUP_RC)"
  RED_ROWS+=("$(_row "Memory" "$MEMORY_STATUS_TEXT")")
fi

if [ "$INFERENCE_PROBE_ENABLED" = "1" ]; then
  INFERENCE_STATUS_TEXT="${ICON_GREEN} OK"
  [ "$INFERENCE_PROBE_RC" -ne 0 ] && INFERENCE_STATUS_TEXT="${ICON_RED} FAILED (rc=$INFERENCE_PROBE_RC)"
  [ "$INFERENCE_PROBE_RC" -ne 0 ] && RED_ROWS+=("$(_row "Inference (LLM)" "$INFERENCE_STATUS_TEXT")") || GREEN_ROWS+=("$(_row "Inference (LLM)" "$INFERENCE_STATUS_TEXT")")
fi

SLACK_REPORT="*OpenClaw Monitor*  ·  $SLACK_REPORT_TIME
*Overall: $OVERALL_STATUS_TEXT*"

# RED table
if [ "${#RED_ROWS[@]}" -gt 0 ]; then
  SLACK_REPORT="${SLACK_REPORT}

*🔴 Failing*
\`\`\`"
  for row in "${RED_ROWS[@]}"; do
    SLACK_REPORT="${SLACK_REPORT}
${row}"
  done
  SLACK_REPORT="${SLACK_REPORT}
\`\`\`"
fi

# YELLOW table
if [ "${#YELLOW_ROWS[@]}" -gt 0 ]; then
  SLACK_REPORT="${SLACK_REPORT}

*🟡 Warnings*
\`\`\`"
  for row in "${YELLOW_ROWS[@]}"; do
    SLACK_REPORT="${SLACK_REPORT}
${row}"
  done
  SLACK_REPORT="${SLACK_REPORT}
\`\`\`"
fi

# GREEN table (show only when there are failing checks to provide context)
if [ "${#GREEN_ROWS[@]}" -gt 0 ] && [ "${#RED_ROWS[@]}" -gt 0 -o "${#YELLOW_ROWS[@]}" -gt 0 ]; then
  SLACK_REPORT="${SLACK_REPORT}

*🟢 Passing*
\`\`\`"
  for row in "${GREEN_ROWS[@]}"; do
    SLACK_REPORT="${SLACK_REPORT}
${row}"
  done
  SLACK_REPORT="${SLACK_REPORT}
\`\`\`"
fi

# Actions (only when there are issues)
if [ "${#ACTION_LINES[@]}" -gt 0 ]; then
  SLACK_REPORT="${SLACK_REPORT}

*Next actions*"
  for action in "${ACTION_LINES[@]}"; do
    SLACK_REPORT="${SLACK_REPORT}
• $action"
  done
fi

# Diagnostics detail (only on problems)
if [ "$STATUS" = "PROBLEM" ]; then
  SLACK_REPORT="${SLACK_REPORT}

*Diagnostics*
• HTTP: status=$HTTP_GATEWAY_STATUS $HTTP_GATEWAY_SUMMARY
• Slack read: $PROBE_REQUEST_SUMMARY
• Slack send: $GATEWAY_PROBE_SUMMARY
• Threads: $THREAD_REPLY_SUMMARY
• Tokens: $TOKEN_PROBE_SUMMARY
• Memory: $MEMORY_LOOKUP_SUMMARY"
  if [ "${#PHASE1_REMEDIATION_ACTIONS[@]}" -gt 0 ]; then
    SLACK_REPORT="${SLACK_REPORT}
• Phase 1: ${PHASE1_REMEDIATION_ACTIONS[*]}"
  fi
  if [ "${#PHASE2_REMEDIATION_ACTIONS[@]}" -gt 0 ]; then
    SLACK_REPORT="${SLACK_REPORT}
• Phase 2: ${PHASE2_REMEDIATION_ACTIONS[*]}"
  fi
  if [ -n "$PHASE2_OUTPUT" ]; then
    PHASE2_OUTPUT_BRIEF="$(printf '%s\n' "$PHASE2_OUTPUT" | tr '\n' ' ' | sed 's/[[:space:]]\+/ /g' | cut -c1-320)"
    SLACK_REPORT="${SLACK_REPORT}
• Phase 2 output: $PHASE2_OUTPUT_BRIEF"
  fi
  if [ -n "$DOCTOR_SH_OUTPUT" ]; then
    DOCTOR_SH_OUTPUT_BRIEF="$(printf '%s\n' "$DOCTOR_SH_OUTPUT" | tr '\n' ' ' | sed 's/[[:space:]]\+/ /g' | cut -c1-320)"
    SLACK_REPORT="${SLACK_REPORT}
• doctor.sh: $DOCTOR_SH_OUTPUT_BRIEF"
  fi
fi

printf '%s\n' "$REPORT" >> "$LOG_FILE"

send_report_to_slack() {
  local target="$1"
  local label="$2"
  if [ -z "$target" ]; then
    return 2
  fi
  if "$OPENCLAW_BIN" message send --channel slack --target "$target" --message "$SLACK_REPORT" >> "$LOG_FILE" 2>&1; then
    log "Phase1/Phase2 monitor delivered STATUS=$STATUS to ${label} Slack target ${target}."
    return 0
  fi
  log "Phase1/Phase2 monitor failed to deliver STATUS=$STATUS to ${label} Slack target ${target}."
  return 1
}

PRIMARY_ALERT_DELIVERED=0
if [ "$STATUS" = "PROBLEM" ]; then
  if [ -z "$ALERT_SLACK_TARGET" ]; then
    log "STATUS=PROBLEM but OPENCLAW_MONITOR_SLACK_TARGET is unset; Slack delivery skipped."
    exit 0
  fi
  if send_report_to_slack "$ALERT_SLACK_TARGET" "primary-alert"; then
    PRIMARY_ALERT_DELIVERED=1
  else
    exit 1
  fi
else
  log "Phase1/Phase2 monitor reported non-PROBLEM status; Slack delivery suppressed."
fi

if [ "$STATUS_BROADCAST_ENABLED" = "1" ] && [ -n "$STATUS_BROADCAST_SLACK_TARGET" ]; then
  if [ "$PRIMARY_ALERT_DELIVERED" -eq 1 ] && [ "$STATUS_BROADCAST_SLACK_TARGET" = "$ALERT_SLACK_TARGET" ]; then
    log "Skipping broadcast delivery because it matches the primary alert target and was already sent."
  else
    send_report_to_slack "$STATUS_BROADCAST_SLACK_TARGET" "status-broadcast" || true
  fi
fi

exit 0
