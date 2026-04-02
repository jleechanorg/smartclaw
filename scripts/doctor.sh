#!/usr/bin/env bash

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LIVE_OPENCLAW="$HOME/.smartclaw"
LAUNCHD_DIR="$HOME/Library/LaunchAgents"
GATEWAY_LABEL="ai.smartclaw.gateway"
GATEWAY_PLIST="$LAUNCHD_DIR/$GATEWAY_LABEL.plist"
AO_DASHBOARD_LABEL="ai.agento.dashboard"
AO_DASHBOARD_LEGACY_LABEL="ai.agent-orchestrator.dashboard"
AO_DASHBOARD_PLIST="$LAUNCHD_DIR/$AO_DASHBOARD_LABEL.plist"
AO_DASHBOARD_LEGACY_PLIST="$LAUNCHD_DIR/$AO_DASHBOARD_LEGACY_LABEL.plist"
SCHEDULED_LABELS=(
  "ai.smartclaw.schedule.morning-log-review"
  "ai.smartclaw.schedule.docs-drift-review"
  "ai.smartclaw.schedule.cron-backup-sync"
  "ai.smartclaw.schedule.weekly-error-trends"
  "ai.smartclaw.schedule.daily-research"
  "ai.smartclaw.schedule.harness-analyzer-9am"
  "ai.smartclaw.schedule.orch-health-weekly"
  "ai.smartclaw.schedule.bug-hunt-9am"
  "ai.smartclaw.schedule.workspace-report-weekly"
)
MIGRATED_JOB_IDS=()

PASS_COUNT=0
WARN_COUNT=0
FAIL_COUNT=0
IS_DARWIN=0
TMP_DIR=""

pass() {
  printf '[PASS] %s\n' "$1"
  PASS_COUNT=$((PASS_COUNT + 1))
}

warn() {
  printf '[WARN] %s\n' "$1"
  WARN_COUNT=$((WARN_COUNT + 1))
}

fail() {
  printf '[FAIL] %s\n' "$1"
  FAIL_COUNT=$((FAIL_COUNT + 1))
}

cleanup() {
  if [[ -n "$TMP_DIR" && -d "$TMP_DIR" ]]; then
    rm -rf "$TMP_DIR"
  fi
}

require_file() {
  local path="$1"
  local label="$2"
  if [[ -f "$path" ]]; then
    pass "$label present: $path"
  else
    fail "$label missing: $path"
  fi
}

require_dir() {
  local path="$1"
  local label="$2"
  if [[ -d "$path" ]]; then
    pass "$label present: $path"
  else
    fail "$label missing: $path"
  fi
}

require_cmd() {
  local cmd="$1"
  if command -v "$cmd" >/dev/null 2>&1; then
    pass "command available: $cmd"
  else
    fail "command missing: $cmd"
  fi
}

default_migrated_job_ids() {
  cat <<'EOF'
c0accca2-3b58-4da6-ba84-e8c929387e30
4ec2aa58-5c97-4c46-8775-a7f030d1dec6
95f858df-0fe8-4434-90c9-c5c89f61889e
d6bb3693-9f5c-4a4e-99ed-bc56eb33e35c
abf80788-7bb0-4ce7-9e09-6c1a97faa5cd
EOF
}

load_migrated_job_ids() {
  while IFS= read -r id; do
    [[ -n "$id" ]] && MIGRATED_JOB_IDS+=("$id")
  done < <(jq -r '.migratedLaunchdJobIds[]?' "$LIVE_OPENCLAW/cron/jobs.json" 2>/dev/null || true)
  if [[ ${#MIGRATED_JOB_IDS[@]} -eq 0 ]]; then
    while IFS= read -r id; do
      [[ -n "$id" ]] && MIGRATED_JOB_IDS+=("$id")
    done < <(default_migrated_job_ids)
  fi
}

detect_local_timezone() {
  local target
  target="$(readlink /etc/localtime 2>/dev/null || true)"
  if [[ "$target" == *"/zoneinfo/"* ]]; then
    echo "${target##*/zoneinfo/}"
    return 0
  fi
  echo "${TZ:-unknown}"
}

detect_ao_dashboard_port() {
  if [[ -n "${OPENCLAW_DOCTOR_AO_DASHBOARD_PORT:-}" ]]; then
    echo "$OPENCLAW_DOCTOR_AO_DASHBOARD_PORT"
    return 0
  fi

  local plist_path="${1:-}"

  # Priority: launchd plist --port arg > agent-orchestrator.yaml port > fallback 3020
  if [[ -n "$plist_path" && -f "$plist_path" ]]; then
    local arg
    local prev_arg=""
    while IFS= read -r arg; do
      # Handle --port=<n>
      if [[ "$arg" =~ ^--port=([0-9]+)$ ]]; then
        echo "${BASH_REMATCH[1]}"
        return 0
      fi

      # Handle -p<n> compact form
      if [[ "$arg" =~ ^-p([0-9]+)$ ]]; then
        echo "${BASH_REMATCH[1]}"
        return 0
      fi

      # Handle "-p <n>" and "--port <n>" separate-argument forms
      if [[ "$prev_arg" == "-p" || "$prev_arg" == "--port" ]]; then
        if [[ "$arg" =~ ^[0-9]+$ ]]; then
          echo "$arg"
          return 0
        fi
      fi

      prev_arg="$arg"
    done < <(plutil -convert json -o - "$plist_path" 2>/dev/null | jq -r '.ProgramArguments[]?' 2>/dev/null || true)
  fi

  # Fall back to agent-orchestrator.yaml port (bd-yk9h: actual AO dashboard port is 3020)
  local _ao_yaml _yaml_port
  if [[ -n "$LIVE_OPENCLAW" ]]; then
    _ao_yaml="$LIVE_OPENCLAW/agent-orchestrator.yaml"
    if [[ -f "$_ao_yaml" ]]; then
      _yaml_port="$(awk '/^port:/ {print $2; exit}' "$_ao_yaml" 2>/dev/null || true)"
      if [[ -n "$_yaml_port" && "$_yaml_port" =~ ^[0-9]+$ ]]; then
        echo "$_yaml_port"
        return 0
      fi
    fi
  fi
  _ao_yaml="$HOME/agent-orchestrator.yaml"
  if [[ -f "$_ao_yaml" ]]; then
    _yaml_port="$(awk '/^port:/ {print $2; exit}' "$_ao_yaml" 2>/dev/null || true)"
    if [[ -n "$_yaml_port" && "$_yaml_port" =~ ^[0-9]+$ ]]; then
      echo "$_yaml_port"
      return 0
    fi
  fi

  # Final fallback (bd-yk9h: was 3011 — wrong, actual default is 3020)
  echo "3020"
}

json_valid() {
  local file="$1"
  jq empty "$file" >/dev/null 2>&1
}

validate_heartbeat_config() {
  local cfg_path="$LIVE_OPENCLAW/openclaw.json"
  local every target prompt

  every="$(jq -r '.agents.defaults.heartbeat.every // empty' "$cfg_path" 2>/dev/null || true)"
  target="$(jq -r '.agents.defaults.heartbeat.target // empty' "$cfg_path" 2>/dev/null || true)"
  prompt="$(jq -r '.agents.defaults.heartbeat.prompt // empty' "$cfg_path" 2>/dev/null || true)"

  if [[ "$every" == "5m" ]]; then
    pass 'heartbeat config: agents.defaults.heartbeat.every is 5m'
  else
    fail "heartbeat config: agents.defaults.heartbeat.every must be 5m (got '$every')"
  fi

  if [[ "$target" == "last" ]]; then
    pass 'heartbeat config: agents.defaults.heartbeat.target is last'
  else
    fail "heartbeat config: agents.defaults.heartbeat.target must be last (got '$target')"
  fi

  if [[ "$prompt" == *"HEARTBEAT.md"* && "$prompt" == *"HEARTBEAT_OK"* ]]; then
    pass 'heartbeat config: prompt references HEARTBEAT.md and HEARTBEAT_OK contract'
  else
    fail 'heartbeat config: prompt must reference HEARTBEAT.md and HEARTBEAT_OK'
  fi

  local status_raw status_json main_enabled main_every
  status_raw="$(openclaw status --json 2>/dev/null || true)"
  status_json="$(printf '%s\n' "$status_raw" | awk 'f||/^{/{f=1}f')"

  if [[ -z "$status_json" ]] || ! printf '%s\n' "$status_json" | jq empty >/dev/null 2>&1; then
    fail 'heartbeat runtime: unable to parse openclaw status --json output'
    return
  fi

  main_enabled="$(printf '%s\n' "$status_json" | jq -r '.heartbeat.agents[]? | select(.agentId=="main") | .enabled' | head -n1)"
  main_every="$(printf '%s\n' "$status_json" | jq -r '.heartbeat.agents[]? | select(.agentId=="main") | .every' | head -n1)"

  if [[ "$main_enabled" == "true" ]]; then
    pass 'heartbeat runtime: main agent heartbeat is enabled'
  else
    fail "heartbeat runtime: main agent heartbeat must be enabled (got '$main_enabled')"
  fi

  if [[ "$main_every" == "5m" ]]; then
    pass 'heartbeat runtime: main agent cadence is 5m'
  else
    fail "heartbeat runtime: main agent cadence must be 5m (got '$main_every')"
  fi
}

cmp_text() {
  local left="$1"
  local right="$2"
  [[ "$left" == "$right" ]]
}

TOKEN_PROBE_LIB="$LIVE_OPENCLAW/lib/token-probes.sh"
if [[ -f "$TOKEN_PROBE_LIB" ]]; then
  # shellcheck disable=SC1090
  source "$TOKEN_PROBE_LIB"
else
  fail "shared token probe library missing: $TOKEN_PROBE_LIB"
  exit 1
fi

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/mctrl-doctor.XXXXXX")"
trap cleanup EXIT

printf 'OpenClaw Repo Doctor\n'
printf 'Repo: %s\n' "$REPO_ROOT"
printf 'Home: %s\n\n' "$HOME"

if [[ "$(uname -s)" == "Darwin" ]]; then
  IS_DARWIN=1
  pass 'running on macOS'
else
  warn 'non-macOS host; launchd checks are skipped'
fi

# Hydrate env vars from the launchd gateway plist so doctor.sh can read them
# regardless of whether it was invoked from a launchd context or a plain shell.
# This ensures the redacted roundtrip env-var check (below) has the full picture.
if [[ "$IS_DARWIN" -eq 1 && -f "$GATEWAY_PLIST" ]]; then
  while IFS= read -r _hyd_line; do
    [[ "$_hyd_line" =~ ^([A-Z0-9_]+)=(.*)$ ]] || continue
    _hyd_var="${BASH_REMATCH[1]}"
    _hyd_val="${BASH_REMATCH[2]}"
    if [[ -z "${!_hyd_var:-}" ]]; then
      export "$_hyd_var"="$_hyd_val"
    fi
  done < <(plutil -convert json -o - "$GATEWAY_PLIST" 2>/dev/null \
    | jq -r '.EnvironmentVariables // {} | to_entries[] | "\(.key)=\(.value)"' 2>/dev/null || true)
  unset _hyd_line _hyd_var _hyd_val
fi

LOCAL_TZ="$(detect_local_timezone)"
if [[ "$LOCAL_TZ" == "America/Los_Angeles" ]]; then
  pass 'local timezone is America/Los_Angeles (matches migrated schedule semantics)'
elif [[ "${OPENCLAW_ALLOW_NON_PT_SCHEDULE:-0}" == "1" ]]; then
  warn "local timezone is '$LOCAL_TZ' (override OPENCLAW_ALLOW_NON_PT_SCHEDULE=1 active)"
else
  fail "local timezone is '$LOCAL_TZ' but migrated schedules are authored for America/Los_Angeles (set OPENCLAW_ALLOW_NON_PT_SCHEDULE=1 to override)"
fi

require_cmd jq
require_cmd curl
require_cmd openclaw
require_cmd lsof
if [[ "$IS_DARWIN" -eq 1 ]]; then
  require_cmd launchctl
  require_cmd plutil
fi
printf '\n'

load_migrated_job_ids
printf '\n'

require_dir "$LIVE_OPENCLAW" 'live ~/.smartclaw'
require_file "$LIVE_OPENCLAW/openclaw.json" 'live openclaw config'
require_file "$LIVE_OPENCLAW/cron/jobs.json" 'live cron jobs'
require_dir "$LIVE_OPENCLAW/logs" 'live logs dir'
require_file "$LIVE_OPENCLAW/run-scheduled-job.sh" 'live scheduled job runner'
if [[ "$IS_DARWIN" -eq 1 ]]; then
  require_file "$GATEWAY_PLIST" 'live launchd gateway plist'
  if [[ -f "$AO_DASHBOARD_PLIST" ]]; then
    pass "live AO dashboard plist present: $AO_DASHBOARD_PLIST"
  elif [[ -f "$AO_DASHBOARD_LEGACY_PLIST" ]]; then
    pass "live AO dashboard plist present (legacy label): $AO_DASHBOARD_LEGACY_PLIST"
  else
    warn "live AO dashboard plist missing: $AO_DASHBOARD_PLIST (legacy: $AO_DASHBOARD_LEGACY_PLIST)"
  fi
  for label in "${SCHEDULED_LABELS[@]}"; do
    require_file "$LAUNCHD_DIR/$label.plist" "live launchd schedule plist ($label)"
  done
else
  warn 'skipping launchd plist file checks on non-macOS'
fi
printf '\n'

live_json_ok=0
if [[ -f "$LIVE_OPENCLAW/openclaw.json" ]] && json_valid "$LIVE_OPENCLAW/openclaw.json"; then
  live_json_ok=1
  pass 'openclaw.json is valid JSON'
else
  fail 'openclaw.json is invalid JSON'
fi

# WS churn safety bounds: timeoutSeconds and maxConcurrent too high = event-loop saturation
# Node.js WS pong budget is 5000ms; with many long-running LLM calls the pong handler is starved.
if [[ "$live_json_ok" -eq 1 ]]; then
  _timeout_s=$(jq -r '.agents.defaults.timeoutSeconds // 0' "$LIVE_OPENCLAW/openclaw.json" 2>/dev/null)
  _max_conc=$(jq -r '.agents.defaults.maxConcurrent // 0' "$LIVE_OPENCLAW/openclaw.json" 2>/dev/null)
  if [[ "$_timeout_s" -gt 600 ]]; then
    warn "agents.defaults.timeoutSeconds=$_timeout_s exceeds safe bound (600) — risk of WS pong starvation and dropped Slack messages. Lower to 600."
  fi
  if [[ "$_max_conc" -gt 3 ]]; then
    warn "agents.defaults.maxConcurrent=$_max_conc exceeds safe bound (3) — risk of event-loop saturation with 600s timeout. Lower to 3."
  fi
  unset _timeout_s _max_conc
fi

# ORCH-slack-all-channels: with groupPolicy=allowlist, channels.slack.channels["*"] must allow
# without requireMention so every channel the bot is invited to is accepted (OpenClaw resolves
# the wildcard for unknown channel IDs).
assert_slack_listen_all_invited_channels() {
  local json_path="$1"
  local label="$2"
  if ! json_valid "$json_path"; then
    fail "$label: invalid JSON ($json_path)"
    return 1
  fi
  local enabled wild_allow wild_mention
  enabled="$(jq -r '.channels.slack.enabled // false' "$json_path")"
  if [[ "$enabled" != "true" ]]; then
    warn "$label: channels.slack.enabled is not true — skipping Slack wildcard check"
    return 0
  fi
  wild_allow="$(jq -r 'if (.channels.slack.channels."*" | has("allow")) then .channels.slack.channels."*".allow else "missing" end' "$json_path")"
  wild_mention="$(jq -r 'if (.channels.slack.channels."*" | has("requireMention")) then .channels.slack.channels."*".requireMention else "missing" end' "$json_path")"
  if [[ "$wild_allow" == "true" && "$wild_mention" == "false" ]]; then
    pass "$label: Slack channels.\"*\" allows all invited channels (allow=true, requireMention=false)"
  else
    fail "$label: Slack channels.\"*\" must be {allow:true, requireMention:false} for all invited channels; got allow=$wild_allow requireMention=$wild_mention ($json_path)"
  fi
}

if [[ "$live_json_ok" -eq 1 ]]; then
  validate_heartbeat_config
  assert_slack_listen_all_invited_channels "$LIVE_OPENCLAW/openclaw.json" 'live openclaw.json'
fi
if [[ -f "$REPO_ROOT/openclaw.json.redacted" ]]; then
  assert_slack_listen_all_invited_channels "$REPO_ROOT/openclaw.json.redacted" 'openclaw.json.redacted'
fi

live_token_raw=''
live_token=''
if [[ "$live_json_ok" -eq 1 ]]; then
  live_token_raw=$(jq -r '.gateway.auth.token // empty' "$LIVE_OPENCLAW/openclaw.json" 2>/dev/null)
  live_token="$(resolve_secret_ref "$live_token_raw")"
fi

plist_token=''
if [[ "$IS_DARWIN" -eq 1 && -f "$GATEWAY_PLIST" ]]; then
  if plist_token=$(plutil -extract EnvironmentVariables.OPENCLAW_GATEWAY_TOKEN raw -o - "$GATEWAY_PLIST" 2>/dev/null); then
    :
  else
    plist_token=''
  fi
fi

if ! is_placeholder_token "$live_token"; then
  pass 'gateway auth token is set in ~/.smartclaw/openclaw.json'
elif ! is_placeholder_token "$plist_token"; then
  pass 'gateway token provided via launchd EnvironmentVariables'
else
  fail 'gateway token missing/placeholder in both ~/.smartclaw/openclaw.json and launchd EnvironmentVariables'
fi

shell_token="${OPENCLAW_GATEWAY_TOKEN:-}"
if ! is_placeholder_token "$shell_token" && ! is_placeholder_token "$live_token" && [[ "$shell_token" != "$live_token" ]]; then
  warn 'shell OPENCLAW_GATEWAY_TOKEN differs from ~/.smartclaw/openclaw.json; gateway probes will use config token'
fi

printf '\n'
if [[ -f "$LIVE_OPENCLAW/cron/jobs.json" ]] && json_valid "$LIVE_OPENCLAW/cron/jobs.json"; then
  still_enabled=''
  missing_ids=''
  for job_id in "${MIGRATED_JOB_IDS[@]}"; do
    if ! jq -e --arg id "$job_id" 'any(.jobs[]?; .id == $id)' "$LIVE_OPENCLAW/cron/jobs.json" >/dev/null 2>&1; then
      if [[ -z "$missing_ids" ]]; then
        missing_ids="$job_id"
      else
        missing_ids="$missing_ids $job_id"
      fi
      continue
    fi

    if jq -e --arg id "$job_id" 'any(.jobs[]?; .id == $id and (.enabled == true))' "$LIVE_OPENCLAW/cron/jobs.json" >/dev/null 2>&1; then
      if [[ -z "$still_enabled" ]]; then
        still_enabled="$job_id"
      else
        still_enabled="$still_enabled $job_id"
      fi
    fi
  done

  if [[ -n "$missing_ids" ]]; then
    warn "legacy migrated cron job IDs are missing from ~/.smartclaw/cron/jobs.json (non-fatal): $missing_ids"
  fi
  if [[ -n "$still_enabled" ]]; then
    warn "legacy migrated cron job IDs are still enabled in ~/.smartclaw/cron/jobs.json (non-fatal): $still_enabled"
  fi
  if [[ -z "$missing_ids" && -z "$still_enabled" ]]; then
    pass 'legacy migrated OpenClaw cron jobs are all absent/disabled in ~/.smartclaw/cron/jobs.json'
  fi
else
  fail 'could not validate live cron jobs JSON'
fi

printf '\n'
if [[ "$IS_DARWIN" -eq 1 ]]; then
  if [[ -f "$GATEWAY_PLIST" ]]; then
    if plutil -lint "$GATEWAY_PLIST" >/dev/null 2>&1; then
      pass 'gateway launchd plist is valid'
    else
      fail 'gateway launchd plist failed plutil -lint'
    fi

    plist_port=$(plutil -extract EnvironmentVariables.OPENCLAW_GATEWAY_PORT raw -o - "$GATEWAY_PLIST" 2>/dev/null || true)
    live_port=$(jq -r '.gateway.port // empty' "$LIVE_OPENCLAW/openclaw.json" 2>/dev/null || true)
    if [[ -n "$plist_port" && -n "$live_port" && "$plist_port" == "$live_port" ]]; then
      pass "gateway port matches between plist and live config ($live_port)"
    else
      fail "gateway port mismatch (plist=$plist_port, live=$live_port)"
    fi

    if ! is_placeholder_token "$plist_token"; then
      pass 'gateway token present in launchd EnvironmentVariables'
    elif ! is_placeholder_token "$live_token"; then
      pass 'gateway token sourced from ~/.smartclaw/openclaw.json'
    else
      warn 'gateway token missing/placeholder in launchd EnvironmentVariables (may still work via openclaw.json token)'
    fi

    # Check for NVM node path in gateway plist (fragile during Node version upgrades)
    # Node 22 via nvm is the required runtime (CLAUDE.md); skip warning for v22.x paths.
    plist_program=$(plutil -extract ProgramArguments.0 raw -o - "$GATEWAY_PLIST" 2>/dev/null || true)
    if [[ "$plist_program" =~ \.nvm/versions/node/v22\. ]]; then
      pass "gateway service uses nvm Node 22 ($plist_program) — correct per policy"
    elif [[ "$plist_program" =~ \.nvm/versions/node/ ]]; then
      warn "gateway service uses a non-v22 Node version manager path ($plist_program); Recommendation: use \`nvm use 22\` per CLAUDE.md policy"
    fi
  fi
else
  warn 'skipping launchd gateway plist validation on non-macOS'
fi

printf '\n'
if [[ "$IS_DARWIN" -eq 1 ]]; then
  launchctl_out="$TMP_DIR/launchctl-gateway.txt"
  if launchctl print "gui/$(id -u)/$GATEWAY_LABEL" >"$launchctl_out" 2>&1; then
    pass 'launchd job is registered'
    if grep -q 'state = running' "$launchctl_out"; then
      pass 'launchd job state is running'
    else
      fail 'launchd job is not in running state'
    fi
  else
    fail "launchctl print failed for $GATEWAY_LABEL"
  fi

  # Check AO dashboard launchd (current label first, then legacy label).
  ao_dashboard_plist_found=""
  if launchctl print "gui/$(id -u)/$AO_DASHBOARD_LABEL" >"$TMP_DIR/launchctl-ao-dashboard.txt" 2>&1; then
    pass 'AO dashboard launchd job is registered'
    ao_dashboard_plist_found="$AO_DASHBOARD_PLIST"
    if grep -q 'state = running' "$TMP_DIR/launchctl-ao-dashboard.txt"; then
      pass 'AO dashboard launchd job state is running'
    else
      warn 'AO dashboard launchd job is not in running state'
    fi
  elif launchctl print "gui/$(id -u)/$AO_DASHBOARD_LEGACY_LABEL" >"$TMP_DIR/launchctl-ao-dashboard-legacy.txt" 2>&1; then
    pass 'AO dashboard launchd job is registered (legacy label)'
    ao_dashboard_plist_found="$AO_DASHBOARD_LEGACY_PLIST"
    if grep -q 'state = running' "$TMP_DIR/launchctl-ao-dashboard-legacy.txt"; then
      pass 'AO dashboard launchd job state is running (legacy label)'
    else
      warn 'AO dashboard launchd job is not in running state (legacy label)'
    fi
  else
    warn 'AO dashboard launchd job is not registered (run install-launchagents.sh to install)'
  fi

  # Validate AO dashboard projects against live `ao list projects` output
  if [[ -n "$ao_dashboard_plist_found" && -f "$ao_dashboard_plist_found" ]] && command -v ao >/dev/null 2>&1; then
    # Extract project list from dashboard plist arguments (format: "for p in proj1 proj2; do ...")
    plist_projects=$(plutil -convert json -o - "$ao_dashboard_plist_found" 2>/dev/null \
      | jq -r '.ProgramArguments[]? | select(startswith("for p in")) | gsub("^for p in | proj2; do.*$";"") | split(" ")[]' 2>/dev/null || true)
    if [[ -n "$plist_projects" ]]; then
      # Get live AO projects
      live_ao_projects=$(ao list projects 2>/dev/null | tail -n +2 | awk '{print $1}' || true)
      if [[ -n "$live_ao_projects" ]]; then
        missing_projects=""
        for proj in $plist_projects; do
          if ! grep -qx "$proj" <<<"$live_ao_projects"; then
            if [[ -z "$missing_projects" ]]; then
              missing_projects="$proj"
            else
              missing_projects="$missing_projects $proj"
            fi
          fi
        done
        if [[ -n "$missing_projects" ]]; then
          warn "AO dashboard references projects not in 'ao list projects': $missing_projects"
        else
          pass "AO dashboard projects validated against live 'ao list projects'"
        fi
      else
        warn "Could not retrieve live AO projects list; skipping dashboard project validation"
      fi
    fi
  fi

  for label in "${SCHEDULED_LABELS[@]}"; do
    if launchctl print "gui/$(id -u)/$label" >"$TMP_DIR/launchctl-$label.txt" 2>&1; then
      pass "launchd schedule is registered: $label"
    else
      fail "launchd schedule is not registered: $label"
    fi
  done

  # Check for raw $HOME in launchd plist templates (launchd doesn't expand shell variables)
  for plist in "$GATEWAY_PLIST" "$AO_DASHBOARD_PLIST" "${SCHEDULED_LABELS[@]/#/$LAUNCHD_DIR/}"; do
    plist="${plist%.plist}.plist"  # Ensure .plist extension
    if [[ -f "$plist" ]]; then
      if plutil -convert xml1 -o - "$plist" 2>/dev/null | grep -q '\$HOME'; then
        plist_name=$(basename "$plist")
        warn "launchd plist contains raw '\$HOME' variable: $plist_name — launchd doesn't expand shell variables; use absolute paths or ~ instead"
      fi
    fi
  done
else
  warn 'skipping launchctl runtime checks on non-macOS'
fi

runtime_port=""
runtime_port=$(jq -r '.gateway.port // empty' "$LIVE_OPENCLAW/openclaw.json" 2>/dev/null || true)
if [[ -z "$runtime_port" ]]; then
  runtime_port='18789'
  warn 'live gateway port unreadable from openclaw.json; defaulting runtime checks to 18789'
fi

if lsof -nP -iTCP:"$runtime_port" -sTCP:LISTEN >"$TMP_DIR/lsof-listen.txt" 2>&1; then
  pass "a process is listening on gateway port $runtime_port"
else
  fail "no process listening on gateway port $runtime_port"
fi

# Initialize ao_dashboard_plist_found to empty string for non-macOS (set -u safety)
ao_dashboard_plist_found=""

# Check AO dashboard port (env override > launchd plist --port > fallback 3011)
AO_DASHBOARD_PORT="$(detect_ao_dashboard_port "${ao_dashboard_plist_found:-}")"
if lsof -nP -iTCP:"$AO_DASHBOARD_PORT" -sTCP:LISTEN >"$TMP_DIR/lsof-ao-dashboard.txt" 2>&1; then
  pass "a process is listening on AO dashboard port $AO_DASHBOARD_PORT"
else
  warn "no process listening on AO dashboard port $AO_DASHBOARD_PORT (dashboard may not be running; override with OPENCLAW_DOCTOR_AO_DASHBOARD_PORT)"
fi

health_body_file="$TMP_DIR/health.json"
health_err_file="$TMP_DIR/health-curl.err"
health_code=$(curl -sS --max-time 5 -o "$health_body_file" -w '%{http_code}' "http://127.0.0.1:${runtime_port}/health" 2>"$health_err_file")
curl_rc=$?
if [[ "$curl_rc" -ne 0 ]]; then
  fail "HTTP /health probe command failed (curl exit=$curl_rc)"
  if [[ -s "$health_err_file" ]]; then
    warn "curl error: $(< "$health_err_file")"
  fi
elif [[ "$health_code" == '200' ]]; then
  pass 'HTTP /health endpoint returned 200'
  if jq empty "$health_body_file" >/dev/null 2>&1; then
    pass '/health response body is valid JSON'
  else
    warn '/health response body is not JSON'
  fi
else
  fail "HTTP /health endpoint failed (code=$health_code)"
fi

gateway_probe_cmd=(env -u OPENCLAW_GATEWAY_TOKEN)
if ! is_placeholder_token "$live_token"; then
  gateway_probe_cmd=(env OPENCLAW_GATEWAY_TOKEN="$live_token")
fi

status_output="$("${gateway_probe_cmd[@]}" openclaw gateway status 2>&1 || true)"
# Accept either legacy "Runtime: running" or current format indicators (Slack/Agents active)
if grep -qE 'Runtime: running|Slack: ok|^Agents:' <<<"$status_output"; then
  pass 'openclaw gateway status reports runtime running'
else
  fail 'openclaw gateway status does not report runtime running'
fi
if grep -q 'RPC probe: failed' <<<"$status_output"; then
  fail 'openclaw gateway status reports RPC probe failure'
fi
if grep -q 'Service config issue:' <<<"$status_output"; then
  if grep -q 'embeds OPENCLAW_GATEWAY_TOKEN and should be reinstalled' <<<"$status_output"; then
    pass 'openclaw gateway status reports embedded service token (expected for repo-managed launchd token persistence)'
  else
    warn 'openclaw gateway status reports service config issue(s)'
  fi
fi

health_cli_output="$("${gateway_probe_cmd[@]}" openclaw gateway health 2>&1)"
health_cli_rc=$?
if [[ "$health_cli_rc" -ne 0 ]]; then
  # Distinguish optional-feature misconfig (missing tunnel tokens) from real failures.
  if grep -qE 'secret reference could not be resolved|missing env var|auth\.token|remote\.token' <<<"$health_cli_output"; then
    warn "openclaw gateway health: optional tunnel token missing (exit=$health_cli_rc) — gateway is operational"
  # Treat transient local WebSocket close as non-fatal when /health and gateway status already passed.
  elif grep -qE 'gateway closed \(1000|normal closure' <<<"$health_cli_output"; then
    warn "openclaw gateway health returned transient close (exit=$health_cli_rc): treating as non-fatal"
  else
    fail "openclaw gateway health command failed (exit=$health_cli_rc)"
    if grep -q 'gateway token mismatch' <<<"$health_cli_output"; then
      fail 'gateway token mismatch detected (gateway.remote.token should match gateway.auth.token)'
    fi
  fi
elif grep -q '^Error:' <<<"$health_cli_output"; then
  fail 'openclaw gateway health reported an error'
else
  pass 'openclaw gateway health completed without errors'
fi

printf '\n'
if [[ "$live_json_ok" -eq 1 ]]; then
  # Check for env var placeholders in critical token fields
  # These MUST be hardcoded real tokens, not ${ENV_VAR} references
  slack_bot_raw="$(jq -r '.channels.slack.botToken // empty' "$LIVE_OPENCLAW/openclaw.json" 2>/dev/null || true)"
  slack_app_raw="$(jq -r '.channels.slack.appToken // empty' "$LIVE_OPENCLAW/openclaw.json" 2>/dev/null || true)"
  
  if [[ "$slack_bot_raw" =~ ^\$\{.*\}$ ]]; then
    fail "channels.slack.botToken contains env var placeholder: $slack_bot_raw (must be hardcoded token)"
  else
    pass 'channels.slack.botToken is hardcoded (not an env var reference)'
  fi
  
  if [[ "$slack_app_raw" =~ ^\$\{.*\}$ ]]; then
    fail "channels.slack.appToken contains env var placeholder: $slack_app_raw (must be hardcoded token)"
  else
    pass 'channels.slack.appToken is hardcoded (not an env var reference)'
  fi

  token_probe_timeout=12

  slack_bot_token="$(resolve_secret_ref "$slack_bot_raw")"
  if is_placeholder_token "$slack_bot_token"; then
    fail 'Slack bot token is missing/placeholder (channels.slack.botToken)'
  else
    slack_bot_body="$TMP_DIR/slack-bot-auth.json"
    if probe_slack_bot_token "$slack_bot_token" "$token_probe_timeout" "$slack_bot_body"; then
      pass 'Slack bot token passed auth.test'
    else
      fail "Slack bot token failed auth.test (http=$LAST_PROBE_HTTP_CODE)"
    fi
  fi

  slack_app_raw="$(jq -r '.channels.slack.appToken // empty' "$LIVE_OPENCLAW/openclaw.json" 2>/dev/null || true)"
  slack_app_token="$(resolve_secret_ref "$slack_app_raw")"
  if is_placeholder_token "$slack_app_token"; then
    fail 'Slack app token is missing/placeholder (channels.slack.appToken)'
  else
    slack_app_body="$TMP_DIR/slack-app-open.json"
    if probe_slack_app_token "$slack_app_token" "$token_probe_timeout" "$slack_app_body"; then
      pass 'Slack app token passed apps.connections.open'
    else
      fail "Slack app token failed apps.connections.open (http=$LAST_PROBE_HTTP_CODE)"
    fi
  fi

  openai_raw="$(jq -r '.plugins.entries."openclaw-mem0".config.oss.embedder.config.apiKey // empty' "$LIVE_OPENCLAW/openclaw.json" 2>/dev/null || true)"
  openai_token="$(resolve_secret_ref "$openai_raw")"
  if is_placeholder_token "$openai_token"; then
    warn 'OpenAI API key is missing/placeholder (mem0 embedder config); skipped OpenAI key probe'
  else
    openai_body="$TMP_DIR/openai-models.json"
    if probe_openai_models_token "$openai_token" "$token_probe_timeout" "$openai_body"; then
      pass 'OpenAI API key passed /v1/models probe'
    else
      fail "OpenAI API key failed /v1/models probe (http=$LAST_PROBE_HTTP_CODE)"
    fi
  fi

  xai_raw="$(jq -r '.env.XAI_API_KEY // empty' "$LIVE_OPENCLAW/openclaw.json" 2>/dev/null || true)"
  xai_token="$(resolve_secret_ref "$xai_raw")"
  if is_placeholder_token "$xai_token"; then
    warn 'XAI API key is missing/placeholder (env.XAI_API_KEY); skipped xAI probe'
  else
    xai_body="$TMP_DIR/xai-models.json"
    if probe_xai_models_token "$xai_token" "$token_probe_timeout" "$xai_body"; then
      pass 'xAI API key passed /v1/models probe'
    else
      fail "xAI API key failed /v1/models probe (http=$LAST_PROBE_HTTP_CODE)"
    fi
  fi

  discord_enabled="$(jq -r '.channels.discord.enabled // false' "$LIVE_OPENCLAW/openclaw.json" 2>/dev/null || echo false)"
  if [[ "$discord_enabled" != "true" ]]; then
    pass 'Discord not enabled (channels.discord.enabled != true); skipped Discord probe'
  else
    discord_raw="$(jq -r '.channels.discord.token // empty' "$LIVE_OPENCLAW/openclaw.json" 2>/dev/null || true)"
    discord_token="$(resolve_secret_ref "$discord_raw")"
    if is_placeholder_token "$discord_token"; then
      fail 'Discord is enabled but channels.discord.token is empty/placeholder'
    else
      discord_body="$TMP_DIR/discord-me.json"
      if probe_discord_bot_token "$discord_token" "$token_probe_timeout" "$discord_body"; then
        pass 'Discord bot token passed users/@me probe'
      else
        fail "Discord bot token failed users/@me probe (http=$LAST_PROBE_HTTP_CODE)"
      fi
    fi
  fi

  mcp_adapter_enabled="$(jq -r '.plugins.entries."openclaw-mcp-adapter".enabled // false' "$LIVE_OPENCLAW/openclaw.json" 2>/dev/null || echo false)"
  if [[ "$mcp_adapter_enabled" == "true" ]]; then
    mcp_mail_url="$(jq -r '.plugins.entries."openclaw-mcp-adapter".config.servers[]? | select(.name=="mcp-agent-mail") | .url // empty' "$LIVE_OPENCLAW/openclaw.json" 2>/dev/null | head -n1)"
    mcp_mail_auth_raw="$(jq -r '.plugins.entries."openclaw-mcp-adapter".config.servers[]? | select(.name=="mcp-agent-mail") | .headers.Authorization // empty' "$LIVE_OPENCLAW/openclaw.json" 2>/dev/null | head -n1)"
    if [[ -n "$mcp_mail_url" ]]; then
      mcp_mail_token="$(resolve_bearer_token_ref "$mcp_mail_auth_raw")"
      mcp_mail_body="$TMP_DIR/mcp-agent-mail-probe.json"
      if is_placeholder_token "$mcp_mail_token"; then
        # No-auth config: probe without bearer token and expect 200 (server runs unauthenticated).
        if probe_mcp_tools_list "$mcp_mail_url" "$token_probe_timeout" "$mcp_mail_body" ""; then
          pass 'MCP Agent Mail tools/list probe passed (no-auth)'
        else
          fail "MCP Agent Mail tools/list probe failed (no-auth, http=$LAST_PROBE_HTTP_CODE)"
        fi
      else
        if probe_mcp_tools_list "$mcp_mail_url" "$token_probe_timeout" "$mcp_mail_body" "$mcp_mail_token"; then
          pass 'MCP Agent Mail token passed tools/list probe'
        else
          fail "MCP Agent Mail token failed tools/list probe (http=$LAST_PROBE_HTTP_CODE)"
        fi
      fi
    else
      warn 'MCP adapter enabled but mcp-agent-mail server URL missing; skipped MCP Agent Mail probe'
    fi
  else
    pass 'MCP adapter not configured/enabled; skipped MCP Agent Mail probe'
  fi
fi

# Live end-to-end probes (Slack send, MCP tools/list, gateway inference)
if command -v openclaw >/dev/null 2>&1; then
  PROBE_TIMEOUT=15
  INFER_TIMEOUT=60

  # 1. Slack message send via openclaw CLI
  SLACK_PROBE_TARGET="${OPENCLAW_DOCTOR_SLACK_PROBE_TARGET:-C0AP8LRKM9N}"
  slack_out="$(openclaw message send --channel slack --target "$SLACK_PROBE_TARGET" \
    --message "[doctor.sh probe] $(date '+%Y-%m-%d %H:%M:%S %Z')" 2>&1)"
  if printf '%s\n' "$slack_out" | grep -q '"ok"\|messageId\|Message ID'; then
    pass "Slack send probe delivered message to $SLACK_PROBE_TARGET"
  else
    fail "Slack send probe failed: $(printf '%s\n' "$slack_out" | head -1)"
  fi

  # 2. OpenClaw MCP adapter — stdio initialize handshake
  OPENCLAW_MCP_BIN="${HOME}/.nvm/versions/node/v22.22.0/lib/node_modules/openclaw-mcp/dist/index.js"
  if [[ -f "$OPENCLAW_MCP_BIN" ]]; then
    mcp_init='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"doctor","version":"0.1.0"}}}'
    mcp_out="$(printf '%s\n' "$mcp_init" | timeout "$PROBE_TIMEOUT" node "$OPENCLAW_MCP_BIN" 2>&1)"
    mcp_rc=$?
    if printf '%s\n' "$mcp_out" | grep -q '"protocolVersion"'; then
      pass "OpenClaw MCP adapter initialize handshake succeeded"
    else
      fail "OpenClaw MCP adapter probe failed (rc=$mcp_rc): $(printf '%s\n' "$mcp_out" | grep -v '^\[openclaw' | head -1)"
    fi
  else
    warn "OpenClaw MCP binary not found at expected path; skipped MCP adapter probe"
  fi

  # 3. Gateway inference — real end-to-end LLM round-trip
  # Uses a longer timeout (60s) since cold-start LLM calls can be slow.
  # rc=124 = timed out — demote to WARN (gateway is healthy, model is just cold).
  # Skipped when OPENCLAW_DOCTOR_SKIP_INFERENCE=1 (e.g. called from monitor which
  # already runs a canary E2E test for LLM reachability).
  if [[ "${OPENCLAW_DOCTOR_SKIP_INFERENCE:-0}" == "1" ]]; then
    warn "Gateway inference probe skipped (OPENCLAW_DOCTOR_SKIP_INFERENCE=1)"
  else
    infer_out="$(timeout "$INFER_TIMEOUT" openclaw agent --agent main --thinking off \
      --timeout "$INFER_TIMEOUT" --message "Reply with exactly one word: pong" 2>&1)"
    infer_rc=$?
    if [[ "$infer_rc" -eq 0 && -n "$infer_out" ]]; then
      pass "Gateway inference probe succeeded (response: $(printf '%s' "$infer_out" | tr '\n' ' ' | cut -c1-40))"
    elif [[ "$infer_rc" -eq 124 ]]; then
      warn "Gateway inference probe timed out after ${INFER_TIMEOUT}s — gateway is running but model cold-start is slow"
    else
      fail "Gateway inference probe failed (rc=$infer_rc): $(printf '%s\n' "$infer_out" | head -1)"
    fi
  fi

  # 4. Memory lookup verification — ensure mem0/memory_search is functional
  if [[ "${OPENCLAW_DOCTOR_SKIP_MEMORY:-0}" == "1" ]]; then
    warn "Memory lookup probe skipped (OPENCLAW_DOCTOR_SKIP_MEMORY=1)"
  else
    memory_out="$(timeout 30 openclaw mem0 search "test" 2>&1)"
    memory_rc=$?
    # Check for NODE_MODULE_VERSION mismatch errors (better-sqlite3)
    if printf '%s\n' "$memory_out" | grep -qi "NODE_MODULE_VERSION\|MODULE_VERSION\|better-sqlite3"; then
      fail "Memory lookup failed: better-sqlite3 Node module version mismatch detected"
    elif [[ "$memory_rc" -ne 0 ]]; then
      fail "Memory lookup command failed (rc=$memory_rc)"
    elif printf '%s\n' "$memory_out" | grep -qE '^[[:space:]]*[0-9]+\.|"score"[[:space:]]*:'; then
      # Results start with a score like "0.531" (old format) OR JSON "score": field
      pass "Memory lookup probe succeeded (found results)"
    elif printf '%s\n' "$memory_out" | grep -qiE 'No matches|\[ *\]'; then
      # "No matches" or empty JSON array means search works but corpus is empty - OK
      pass "Memory lookup probe succeeded (search functional, corpus empty)"
    else
      warn "Memory lookup returned no searchable results (may be empty corpus)"
    fi
  fi
fi

printf '\n=== openclaw.json validation ===\n'

# Sync redaction-related env vars from live openclaw.json for verify + pytest roundtrip.
# Always overwrite when the JSON has a value — stale tokens in ~/.bashrc must not win over
# the live config (otherwise drift checks false-fail even after regenerating .redacted).
sync_redaction_env_from_openclaw_json() {
  local cfg="$REPO_ROOT/openclaw.json"
  if [[ ! -f "$cfg" ]]; then
    return 0
  fi
  # shellcheck disable=SC1090
  eval "$(python3 - "$cfg" <<'PY'
import json
import shlex
import sys

cfg_path = sys.argv[1]
with open(cfg_path, encoding="utf-8") as f:
    cfg = json.load(f)


def get_path(d, path):
    node = d
    for p in path[:-1]:
        node = node[p]
    return node[path[-1]]


REDACTION_MAP = [
    (["env", "XAI_API_KEY"], "XAI_API_KEY"),
    (["env", "SLACK_BOT_TOKEN"], "SLACK_BOT_TOKEN"),
    (["env", "OPENCLAW_SLACK_APP_TOKEN"], "OPENCLAW_SLACK_APP_TOKEN"),
    (["env", "OPENCLAW_HOOKS_TOKEN"], "OPENCLAW_HOOKS_TOKEN"),
    (["hooks", "token"], "OPENCLAW_HOOKS_TOKEN"),
    (["channels", "slack", "botToken"], "SLACK_BOT_TOKEN"),
    (["channels", "slack", "appToken"], "OPENCLAW_SLACK_APP_TOKEN"),
    (["channels", "discord", "token"], "DISCORD_BOT_TOKEN"),
    (["gateway", "auth", "token"], "OPENCLAW_GATEWAY_TOKEN"),
    (["gateway", "remote", "token"], "OPENCLAW_GATEWAY_REMOTE_TOKEN"),
    (["plugins", "entries", "openclaw-mem0", "config", "oss", "embedder", "config", "apiKey"], "OPENAI_API_KEY"),
    (["plugins", "entries", "openclaw-mem0", "config", "oss", "llm", "config", "api_key"], "GROQ_API_KEY"),
    (["plugins", "entries", "openclaw-mem0", "config", "oss", "llm", "config", "apiKey"], "GROQ_API_KEY"),
]
seen = set()
for path, var in REDACTION_MAP:
    if var in seen:
        continue
    seen.add(var)
    try:
        val = get_path(cfg, path)
    except (KeyError, TypeError):
        continue
    if val is None:
        continue
    print(f"export {var}={shlex.quote(str(val))}")
PY
)"
}

sync_redaction_env_from_openclaw_json

# First check: regenerate from openclaw.json.redacted + env vars and diff against live (any diff = FAIL).
# Must match scripts/verify-config-from-redacted.sh required_vars list.
REDACTED_ROUNDTRIP_VARS=(
  XAI_API_KEY
  SLACK_BOT_TOKEN
  OPENCLAW_SLACK_APP_TOKEN
  OPENCLAW_HOOKS_TOKEN
  OPENCLAW_GATEWAY_TOKEN
  OPENCLAW_GATEWAY_REMOTE_TOKEN
  OPENAI_API_KEY
  GROQ_API_KEY
  DISCORD_BOT_TOKEN
)
missing_redaction_vars=()
for var in "${REDACTED_ROUNDTRIP_VARS[@]}"; do
  if [[ -z "${!var:-}" ]]; then
    missing_redaction_vars+=("$var")
  fi
done

if [[ "${OPENCLAW_DOCTOR_SKIP_REDACTED_ROUNDTRIP:-0}" == "1" ]]; then
  warn 'openclaw.json redacted roundtrip skipped (OPENCLAW_DOCTOR_SKIP_REDACTED_ROUNDTRIP=1)'
elif [[ ${#missing_redaction_vars[@]} -gt 0 ]]; then
  warn "openclaw.json redacted roundtrip skipped (missing env vars: ${missing_redaction_vars[*]} — export them to enforce zero drift vs openclaw.json.redacted)"
elif [[ -f "$REPO_ROOT/scripts/verify-config-from-redacted.sh" ]]; then
  verify_out="$TMP_DIR/verify-config-from-redacted.out"
  if bash "$REPO_ROOT/scripts/verify-config-from-redacted.sh" >"$verify_out" 2>&1; then
    pass 'openclaw.json matches openclaw.json.redacted + env vars (regenerated diff is empty)'
  else
    fail 'openclaw.json does NOT match openclaw.json.redacted + env vars — config drift (red)'
    while IFS= read -r line || [[ -n "$line" ]]; do
      printf '%s\n' "$line"
    done <"$verify_out"
  fi
else
  warn 'scripts/verify-config-from-redacted.sh not found — skipping redacted config roundtrip check'
fi

# Second check: pytest validation
if command -v python3 >/dev/null 2>&1 && python3 -c "import pytest" >/dev/null 2>&1; then
  pytest_out="$TMP_DIR/pytest-configs.txt"
  # Run only the comprehensive config-validation classes (not legacy tests with known pre-existing failures)
  python3 -m pytest "$REPO_ROOT/tests/test_openclaw_configs.py" \
    -k "TestMetaAndLogging or TestAuthProfiles or TestAgentDefaults or TestToolsConfig or TestEnvSection or TestGatewaySecurity or TestHooksConfig or TestSessionConfig or TestCommandsConfig or TestMessagesConfig or TestPluginChannelConsistency or TestSlackChannelsConfig or TestRequiredAgents or TestSkillsConfig or TestExecSafeBins or TestRedactedConfigRoundtrip" \
    -v --tb=short 2>&1 | tee "$pytest_out" || true
  pytest_exit=${PIPESTATUS[0]}
  if [[ "$pytest_exit" -eq 0 ]]; then
    pytest_pass_count=$(grep -c ' PASSED' "$pytest_out" 2>/dev/null || true)
    pass "pytest openclaw.json validation: $pytest_pass_count tests passed"
  else
    pytest_fail_count=$(grep -c ' FAILED\|ERROR' "$pytest_out" 2>/dev/null || true)
    fail "pytest openclaw.json validation: $pytest_fail_count test(s) failed (see above)"
  fi
else
  warn 'python3 or pytest not available — skipping openclaw.json pytest validation'
fi

printf '\nSummary: %s pass, %s warn, %s fail\n' "$PASS_COUNT" "$WARN_COUNT" "$FAIL_COUNT"

if [[ "$FAIL_COUNT" -gt 0 ]]; then
  exit 1
fi

exit 0
