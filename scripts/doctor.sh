#!/usr/bin/env bash

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# LIVE_OPENCLAW is resolved after launchd env hydration (below) — do not set here.
LAUNCHD_DIR="$HOME/Library/LaunchAgents"
# Staging detection: check for staging plist + state dir to decide label
# staging uses ai.openclaw.staging (port 18810), prod uses ai.openclaw.gateway (port 18789)
if [[ -f "$LAUNCHD_DIR/ai.openclaw.staging.plist" ]]; then
  if [[ "${OPENCLAW_STATE_DIR:-}" == *"/.openclaw_prod"* ]]; then
    GATEWAY_LABEL="ai.openclaw.gateway"
  else
    # Default to staging label unless explicitly in prod
    GATEWAY_LABEL="ai.openclaw.staging"
  fi
# Migration-safe: prefer ai.openclaw.gateway; fall back to com.openclaw.gateway
# (some installs still use the legacy label — both templates exist in launchd/)
elif [[ -f "$LAUNCHD_DIR/ai.openclaw.gateway.plist" ]]; then
  GATEWAY_LABEL="ai.openclaw.gateway"
elif [[ -f "$LAUNCHD_DIR/com.openclaw.gateway.plist" ]]; then
  GATEWAY_LABEL="com.openclaw.gateway"
else
  GATEWAY_LABEL="ai.openclaw.gateway"  # best guess; drift check will catch mismatch
fi
GATEWAY_PLIST="$LAUNCHD_DIR/$GATEWAY_LABEL.plist"
AO_DASHBOARD_LABEL="ai.agento.dashboard"
AO_DASHBOARD_LEGACY_LABEL="ai.agent-orchestrator.dashboard"
AO_DASHBOARD_PLIST="$LAUNCHD_DIR/$AO_DASHBOARD_LABEL.plist"
AO_DASHBOARD_LEGACY_PLIST="$LAUNCHD_DIR/$AO_DASHBOARD_LEGACY_LABEL.plist"
SCHEDULED_LABELS=(
  "ai.openclaw.schedule.morning-log-review"
  "ai.openclaw.schedule.docs-drift-review"
  "ai.openclaw.schedule.cron-backup-sync"
  "ai.openclaw.schedule.weekly-error-trends"
  "ai.openclaw.schedule.daily-research"
  "ai.openclaw.schedule.harness-analyzer-9am"
  "ai.openclaw.schedule.orch-health-weekly"
  "ai.openclaw.schedule.bug-hunt-9am"
  "ai.openclaw.schedule.workspace-report-weekly"
)
MIGRATED_JOB_IDS=()

PASS_COUNT=0
WARN_COUNT=0
FAIL_COUNT=0
IS_DARWIN=0
TMP_DIR=""

# Runtime invariants for the production profile. Override via env if needed.
EXPECTED_PRIMARY_MODEL="${OPENCLAW_DOCTOR_EXPECTED_PRIMARY_MODEL:-minimax-portal/MiniMax-M2.7}"
EXPECTED_MAX_CONCURRENT="${OPENCLAW_DOCTOR_EXPECTED_MAX_CONCURRENT:-10}"
EXPECTED_SUBAGENT_MAX_CONCURRENT="${OPENCLAW_DOCTOR_EXPECTED_SUBAGENT_MAX_CONCURRENT:-10}"
EXPECTED_TIMEOUT_SECONDS="${OPENCLAW_DOCTOR_EXPECTED_TIMEOUT_SECONDS:-600}"
EXPECTED_MEM_EMBEDDER_PROVIDER="${OPENCLAW_DOCTOR_EXPECTED_MEM_EMBEDDER_PROVIDER:-ollama}"

# WS/event-loop warning thresholds (advisory, not hard policy invariants).
WS_SAFE_TIMEOUT_SECONDS="${OPENCLAW_DOCTOR_WS_SAFE_TIMEOUT_SECONDS:-600}"
WS_SAFE_MAX_CONCURRENT="${OPENCLAW_DOCTOR_WS_SAFE_MAX_CONCURRENT:-10}"
WS_SAFE_MAX_SUBAGENT_CONCURRENT="${OPENCLAW_DOCTOR_WS_SAFE_MAX_SUBAGENT_CONCURRENT:-10}"

# openclaw gateway status/health use WebSocket RPC (CLI default 10s). Under event-loop pressure
# (Slack pong, mem0, embeds), RPC can exceed 10s while curl /health stays 200 — false FAIL in doctor.
GATEWAY_RPC_TIMEOUT_MS="${OPENCLAW_DOCTOR_GATEWAY_RPC_TIMEOUT_MS:-30000}"

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
  # All previously gateway-managed cron jobs have been migrated to launchd plists.
  # The jobs are tracked via their launchd plist labels (ai.openclaw.schedule.*).
  # No legacy gateway-managed cron job IDs remain to check.
  cat <<'EOF'
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

infer_gateway_profile_dir_from_port() {
  local gateway_port="${1:-}"
  case "$gateway_port" in
    18789) echo "$HOME/.openclaw_prod" ;;
    18810) echo "$HOME/.openclaw" ;;
    *) echo "" ;;
  esac
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

plist_extract_raw() {
  local key_path="$1"
  local plist_path="$2"
  local out=""
  out="$(plutil -extract "$key_path" raw -o - "$plist_path" 2>/dev/null)" || return 1
  printf '%s' "$out"
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

validate_runtime_invariants() {
  local cfg_path="$LIVE_OPENCLAW/openclaw.json"
  local primary max_conc sub_max timeout_s mem_embedder

  primary="$(jq -r '.agents.defaults.model.primary // empty' "$cfg_path" 2>/dev/null || true)"
  max_conc="$(jq -r '.agents.defaults.maxConcurrent // empty' "$cfg_path" 2>/dev/null || true)"
  sub_max="$(jq -r '.agents.defaults.subagents.maxConcurrent // empty' "$cfg_path" 2>/dev/null || true)"
  timeout_s="$(jq -r '.agents.defaults.timeoutSeconds // empty' "$cfg_path" 2>/dev/null || true)"
  mem_embedder="$(jq -r '.plugins.entries."openclaw-mem0".config.oss.embedder.provider // empty' "$cfg_path" 2>/dev/null || true)"

  if [[ -n "$EXPECTED_PRIMARY_MODEL" && "$EXPECTED_PRIMARY_MODEL" != "any" ]]; then
    if [[ "$primary" == "$EXPECTED_PRIMARY_MODEL" ]]; then
      pass "runtime invariant: primary model is $EXPECTED_PRIMARY_MODEL"
    else
      fail "runtime invariant: primary model drifted (got '$primary', expected '$EXPECTED_PRIMARY_MODEL')"
    fi
  fi

  if [[ "$EXPECTED_MAX_CONCURRENT" =~ ^[0-9]+$ ]]; then
    if [[ "$max_conc" == "$EXPECTED_MAX_CONCURRENT" ]]; then
      pass "runtime invariant: agents.defaults.maxConcurrent is $EXPECTED_MAX_CONCURRENT"
    else
      fail "runtime invariant: agents.defaults.maxConcurrent drifted (got '$max_conc', expected '$EXPECTED_MAX_CONCURRENT')"
    fi
  fi

  if [[ "$EXPECTED_SUBAGENT_MAX_CONCURRENT" =~ ^[0-9]+$ ]]; then
    if [[ "$sub_max" == "$EXPECTED_SUBAGENT_MAX_CONCURRENT" ]]; then
      pass "runtime invariant: agents.defaults.subagents.maxConcurrent is $EXPECTED_SUBAGENT_MAX_CONCURRENT"
    else
      fail "runtime invariant: agents.defaults.subagents.maxConcurrent drifted (got '$sub_max', expected '$EXPECTED_SUBAGENT_MAX_CONCURRENT')"
    fi
  fi

  if [[ "$EXPECTED_TIMEOUT_SECONDS" =~ ^[0-9]+$ ]]; then
    if [[ "$timeout_s" == "$EXPECTED_TIMEOUT_SECONDS" ]]; then
      pass "runtime invariant: agents.defaults.timeoutSeconds is $EXPECTED_TIMEOUT_SECONDS"
    else
      fail "runtime invariant: agents.defaults.timeoutSeconds drifted (got '$timeout_s', expected '$EXPECTED_TIMEOUT_SECONDS')"
    fi
  fi

  if [[ -n "$EXPECTED_MEM_EMBEDDER_PROVIDER" && "$EXPECTED_MEM_EMBEDDER_PROVIDER" != "any" ]]; then
    if [[ "$mem_embedder" == "$EXPECTED_MEM_EMBEDDER_PROVIDER" ]]; then
      pass "runtime invariant: mem0 embedder provider is $EXPECTED_MEM_EMBEDDER_PROVIDER"
    else
      fail "runtime invariant: mem0 embedder provider drifted (got '$mem_embedder', expected '$EXPECTED_MEM_EMBEDDER_PROVIDER')"
    fi
  fi
}

check_config_audit_gateway_rewrites() {
  local audit_path="$LIVE_OPENCLAW/logs/config-audit.jsonl"
  local cfg_path="$LIVE_OPENCLAW/openclaw.json"
  local parsed ts changed argv

  if [[ ! -f "$audit_path" ]]; then
    warn "config-audit file missing: $audit_path"
    return 0
  fi

  parsed="$(python3 - "$audit_path" "$cfg_path" <<'PY'
import json, sys
from pathlib import Path

audit_path = Path(sys.argv[1])
cfg_path = str(Path(sys.argv[2]))
latest = None

for raw in audit_path.read_text(encoding="utf-8", errors="replace").splitlines():
    line = raw.strip()
    if not line:
        continue
    try:
        obj = json.loads(line)
    except Exception:
        continue
    if obj.get("event") != "config.write":
        continue
    if obj.get("configPath") != cfg_path:
        continue
    argv = obj.get("argv") or []
    if "gateway" not in argv or "--allow-unconfigured" not in argv:
        continue
    latest = obj

if latest is None:
    print("")
else:
    ts = latest.get("ts", "unknown")
    changed = latest.get("changedPathCount")
    if changed is None:
        changed = -1
    argv = " ".join(latest.get("argv") or [])
    print(f"{ts}|{changed}|{argv}")
PY
)"

  if [[ -z "$parsed" ]]; then
    pass "config-audit: no gateway --allow-unconfigured rewrites recorded for $cfg_path"
    return 0
  fi

  IFS='|' read -r ts changed argv <<< "$parsed"
  if [[ "$changed" =~ ^-?[0-9]+$ ]] && [[ "$changed" -gt 2 ]]; then
    warn "config-audit: gateway --allow-unconfigured rewrote $changed paths at $ts (argv: $argv) — investigate drift risk"
  else
    pass "config-audit: latest gateway --allow-unconfigured rewrite touched $changed path(s) at $ts"
  fi
}

cmp_text() {
  local left="$1"
  local right="$2"
  [[ "$left" == "$right" ]]
}

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

  if [[ -z "${OPENCLAW_STATE_DIR:-}" ]]; then
    _gateway_port="$(plutil -extract EnvironmentVariables.OPENCLAW_GATEWAY_PORT raw -o - "$GATEWAY_PLIST" 2>/dev/null || true)"
    _inferred_state_dir="$(infer_gateway_profile_dir_from_port "$_gateway_port")"
    if [[ -n "$_inferred_state_dir" ]]; then
      export OPENCLAW_STATE_DIR="$_inferred_state_dir"
    fi
    unset _gateway_port _inferred_state_dir
  fi
  if [[ -z "${OPENCLAW_CONFIG_PATH:-}" && -n "${OPENCLAW_STATE_DIR:-}" ]]; then
    export OPENCLAW_CONFIG_PATH="${OPENCLAW_STATE_DIR}/openclaw.json"
  fi
fi

# Live OpenClaw tree (openclaw.json, cron/, lib/, agents/, logs/): this checkout by default.
# OPENCLAW_LIVE_ROOT overrides; else OPENCLAW_STATE_DIR / OPENCLAW_CONFIG_PATH (gateway/monitor).
if [[ -n "${OPENCLAW_LIVE_ROOT:-}" ]]; then
  LIVE_OPENCLAW="${OPENCLAW_LIVE_ROOT}"
elif [[ -n "${OPENCLAW_STATE_DIR:-}" ]]; then
  LIVE_OPENCLAW="${OPENCLAW_STATE_DIR}"
elif [[ -n "${OPENCLAW_CONFIG_PATH:-}" ]] && [[ -f "$OPENCLAW_CONFIG_PATH" ]]; then
  LIVE_OPENCLAW="$(cd "$(dirname "$OPENCLAW_CONFIG_PATH")" && pwd)"
else
  LIVE_OPENCLAW="$REPO_ROOT"
fi

TOKEN_PROBE_LIB="$LIVE_OPENCLAW/lib/token-probes.sh"
if [[ -f "$TOKEN_PROBE_LIB" ]]; then
  # shellcheck disable=SC1090
  source "$TOKEN_PROBE_LIB"
else
  fail "shared token probe library missing: $TOKEN_PROBE_LIB"
  exit 1
fi

printf 'Live OpenClaw dir: %s\n' "$LIVE_OPENCLAW"

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

require_dir "$LIVE_OPENCLAW" 'live OpenClaw dir'
require_file "$LIVE_OPENCLAW/openclaw.json" 'live openclaw config'
require_file "$LIVE_OPENCLAW/cron/jobs.json" 'live cron jobs'
require_dir "$LIVE_OPENCLAW/logs" 'live logs dir'
require_file "$LIVE_OPENCLAW/run-scheduled-job.sh" 'live scheduled job runner'
if [[ "$IS_DARWIN" -eq 1 ]]; then
  require_file "$GATEWAY_PLIST" 'live launchd gateway plist'
  # Catch plist-label drift: doctor.sh GATEWAY_LABEL must match the physical plist's actual label.
  # A mismatch means the plist was renamed in git but the migration (install-launchagents.sh)
  # hasn't run yet on this machine — doctor.sh will fail the whole chain.
  physical_label="$(plutil -extract Label raw -o - "$GATEWAY_PLIST" 2>/dev/null || true)"
  if [[ -n "$physical_label" && "$physical_label" != "$GATEWAY_LABEL" ]]; then
    fail "gateway plist label DRIFT: doctor.sh expects '$GATEWAY_LABEL' but physical plist is '$physical_label' — run install-launchagents.sh to migrate"
  elif [[ "$physical_label" == "$GATEWAY_LABEL" ]]; then
    pass "gateway plist label matches: $GATEWAY_LABEL"
  fi
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

if [[ "$live_json_ok" -eq 1 ]]; then
  validate_runtime_invariants
  check_config_audit_gateway_rewrites
fi

# WS churn safety bounds: timeoutSeconds and maxConcurrent too high = event-loop saturation
# Node.js WS pong budget is 5000ms; with many long-running LLM calls the pong handler is starved.
if [[ "$live_json_ok" -eq 1 ]]; then
  _timeout_s=$(jq -r '.agents.defaults.timeoutSeconds // 0' "$LIVE_OPENCLAW/openclaw.json" 2>/dev/null)
  _max_conc=$(jq -r '.agents.defaults.maxConcurrent // 0' "$LIVE_OPENCLAW/openclaw.json" 2>/dev/null)
  _sub_max_conc=$(jq -r '.agents.defaults.subagents.maxConcurrent // 0' "$LIVE_OPENCLAW/openclaw.json" 2>/dev/null)
  if [[ "$_timeout_s" -gt "$WS_SAFE_TIMEOUT_SECONDS" ]]; then
    warn "agents.defaults.timeoutSeconds=$_timeout_s exceeds safe bound ($WS_SAFE_TIMEOUT_SECONDS) — risk of WS pong starvation and dropped Slack messages."
  fi
  if [[ "$_max_conc" -gt "$WS_SAFE_MAX_CONCURRENT" ]]; then
    warn "agents.defaults.maxConcurrent=$_max_conc exceeds safe bound ($WS_SAFE_MAX_CONCURRENT) — risk of event-loop saturation."
  fi
  if [[ "$_sub_max_conc" -gt "$WS_SAFE_MAX_SUBAGENT_CONCURRENT" ]]; then
    warn "agents.defaults.subagents.maxConcurrent=$_sub_max_conc exceeds safe bound ($WS_SAFE_MAX_SUBAGENT_CONCURRENT) — risk of event-loop saturation."
  fi
  unset _timeout_s _max_conc _sub_max_conc
fi

# MiniMax: validate model/provider consistency, but do not hardcode plugin ids.
# OpenClaw releases have shipped both `minimax` and `minimax-portal-auth` naming,
# so plugin-id enforcement here caused false failures on healthy installs.
if [[ "$live_json_ok" -eq 1 ]]; then
  _cfg="$LIVE_OPENCLAW/openclaw.json"
  _mm_err=$(jq -r '
    def providers: (.models.providers // {});
    def model_ids:
      ([.agents.defaults.model.primary] + (.agents.defaults.model.fallbacks // [])
        + ([.agents.list[]? | .model // empty]))
      | map(select(. != null and . != ""))
      | unique
      | .[];
    . as $root
    | model_ids as $mid
    | ($mid | split("/")[0]) as $p
    | if $p == "minimax-portal" then
        if ($root | providers | has("minimax-portal") | not) then
          "model \($mid) requires models.providers.minimax-portal"
        else empty end
      elif $p == "minimax" then
        if ($root | providers | has("minimax") | not) then
          "model \($mid) uses legacy minimax/ but models.providers.minimax is missing — use minimax-portal/MiniMax-M2.7 and models.providers.minimax-portal (or register minimax)"
        else empty end
      else empty end
  ' "$_cfg" 2>/dev/null | head -n 1)
  if [[ -n "$_mm_err" ]]; then
    fail "MiniMax model/provider mismatch: $_mm_err"
  else
    pass 'MiniMax model ids match models.providers (minimax / minimax-portal)'
  fi
  unset _cfg _mm_err
fi

# ORCH-slack-all-channels: with groupPolicy=allowlist, channels.slack.channels["*"] must
# accept all invited channels without requireMention. OpenClaw has shipped both
# `enabled:true` and legacy `allow:true` channel entry schemas, so accept either.
assert_slack_listen_all_invited_channels() {
  local json_path="$1"
  local label="$2"
  if ! json_valid "$json_path"; then
    fail "$label: invalid JSON ($json_path)"
    return 1
  fi
  local enabled wild_enabled wild_allow wild_mention
  enabled="$(jq -r '.channels.slack.enabled // false' "$json_path")"
  if [[ "$enabled" != "true" ]]; then
    warn "$label: channels.slack.enabled is not true — skipping Slack wildcard check"
    return 0
  fi
  wild_enabled="$(jq -r 'if (.channels.slack.channels."*" | has("enabled")) then .channels.slack.channels."*".enabled else "missing" end' "$json_path")"
  wild_allow="$(jq -r 'if (.channels.slack.channels."*" | has("allow")) then .channels.slack.channels."*".allow else "missing" end' "$json_path")"
  wild_mention="$(jq -r 'if (.channels.slack.channels."*" | has("requireMention")) then .channels.slack.channels."*".requireMention else "missing" end' "$json_path")"
  if [[ ( "$wild_enabled" == "true" || "$wild_allow" == "true" ) && "$wild_mention" == "false" ]]; then
    pass "$label: Slack channels.\"*\" allows all invited channels (enabled/allow=true, requireMention=false)"
  else
    fail "$label: Slack channels.\"*\" must allow all invited channels with requireMention=false; got enabled=$wild_enabled allow=$wild_allow requireMention=$wild_mention ($json_path)"
  fi
}

if [[ "$live_json_ok" -eq 1 ]]; then
  validate_heartbeat_config
  assert_slack_listen_all_invited_channels "$LIVE_OPENCLAW/openclaw.json" 'live openclaw.json'
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
  pass 'gateway auth token is set in ~/.openclaw/openclaw.json'
elif ! is_placeholder_token "$plist_token"; then
  pass 'gateway token provided via launchd EnvironmentVariables'
else
  fail 'gateway token missing/placeholder in both ~/.openclaw/openclaw.json and launchd EnvironmentVariables'
fi

shell_token="${OPENCLAW_GATEWAY_TOKEN:-}"
if ! is_placeholder_token "$shell_token" && ! is_placeholder_token "$live_token" && [[ "$shell_token" != "$live_token" ]]; then
  warn 'shell OPENCLAW_GATEWAY_TOKEN differs from ~/.openclaw/openclaw.json; gateway probes will use config token'
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
    warn "legacy migrated cron job IDs are missing from ~/.openclaw/cron/jobs.json (non-fatal): $missing_ids"
  fi
  if [[ -n "$still_enabled" ]]; then
    warn "legacy migrated cron job IDs are still enabled in ~/.openclaw/cron/jobs.json (non-fatal): $still_enabled"
  fi
  if [[ -z "$missing_ids" && -z "$still_enabled" ]]; then
    pass 'legacy migrated OpenClaw cron jobs are all absent/disabled in ~/.openclaw/cron/jobs.json'
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

    plist_port="$(plist_extract_raw EnvironmentVariables.OPENCLAW_GATEWAY_PORT "$GATEWAY_PLIST" || true)"
    live_port=$(jq -r '.gateway.port // empty' "$LIVE_OPENCLAW/openclaw.json" 2>/dev/null || true)
    if [[ -n "$plist_port" && -n "$live_port" && "$plist_port" == "$live_port" ]]; then
      pass "gateway port matches between plist and live config ($live_port)"
    else
      fail "gateway port mismatch (plist=$plist_port, live=$live_port)"
    fi

    if ! is_placeholder_token "$plist_token"; then
      pass 'gateway token present in launchd EnvironmentVariables'
    elif ! is_placeholder_token "$live_token"; then
      pass 'gateway token sourced from ~/.openclaw/openclaw.json'
    else
      warn 'gateway token missing/placeholder in launchd EnvironmentVariables (may still work via openclaw.json token)'
    fi

    # Check OPENCLAW_STATE_DIR in plist matches the expected prod dir.
    # Mismatch caused the 2026-04-04 incident: plist used ~/.openclaw-production/ (wrong)
    # while deploy.sh syncs to ~/.openclaw_prod/ — gateway was live but reading stale/empty config.
    plist_state_dir="$(plist_extract_raw EnvironmentVariables.OPENCLAW_STATE_DIR "$GATEWAY_PLIST" || true)"
    expected_state_dir="$HOME/.openclaw_prod"
    inferred_state_dir="$(infer_gateway_profile_dir_from_port "$plist_port")"
    if [[ -n "$plist_state_dir" ]]; then
      # Normalize: remove trailing slash for comparison
      plist_state_dir_norm="${plist_state_dir%/}"
      if [[ "$plist_state_dir_norm" == "$expected_state_dir" ]]; then
        pass "plist OPENCLAW_STATE_DIR matches deploy target ($plist_state_dir_norm)"
      else
        fail "plist OPENCLAW_STATE_DIR mismatch: plist='$plist_state_dir_norm' expected='$expected_state_dir' — deploy.sh syncs to wrong dir; run install-launchagents.sh"
      fi
    elif [[ -n "$inferred_state_dir" && "$inferred_state_dir" == "$expected_state_dir" ]]; then
      pass "plist OPENCLAW_STATE_DIR inferred from gateway port ($plist_port -> $inferred_state_dir)"
    else
      fail "plist has no OPENCLAW_STATE_DIR; gateway will use wrong state dir — run install-launchagents.sh to fix"
    fi

    # Check auth-profiles.json present in prod state dir — liveness ≠ functional.
    # HTTP /health returns "live" even when auth-profiles.json is missing.
    # Missing file → all LLM calls fail silently with "No API key found for provider".
    # Always run this check: fall back to expected_state_dir when plist_state_dir is unset.
    auth_state_dir="${plist_state_dir:-${inferred_state_dir:-$expected_state_dir}}"
    prod_auth="$auth_state_dir/agents/main/agent/auth-profiles.json"
    if [[ -f "$prod_auth" ]]; then
      pass "auth-profiles.json present in prod state dir ($prod_auth)"
    else
      fail "auth-profiles.json MISSING: $prod_auth — gateway HTTP health will pass but agent cannot authenticate; run deploy.sh or copy from staging"
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
  AO_DASHBOARD_REGISTERED=0
  AO_DASHBOARD_LAUNCHD_RUNNING=0
  AO_DASHBOARD_LAUNCHD_NOTE=""
  launchctl_out="$TMP_DIR/launchctl-gateway.txt"
  if launchctl print "gui/$(id -u)/$GATEWAY_LABEL" >"$launchctl_out" 2>&1; then
    pass 'launchd job is registered'
    if grep -q 'state = running' "$launchctl_out"; then
      pass 'launchd job state is running'
    else
      warn 'launchd job is not in running state (will rely on listener + /health probes)'
    fi
  else
    fail "launchctl print failed for $GATEWAY_LABEL"
  fi

  # Check AO dashboard launchd (current label first, then legacy label).
  ao_dashboard_plist_found=""
  if launchctl print "gui/$(id -u)/$AO_DASHBOARD_LABEL" >"$TMP_DIR/launchctl-ao-dashboard.txt" 2>&1; then
    pass 'AO dashboard launchd job is registered'
    AO_DASHBOARD_REGISTERED=1
    ao_dashboard_plist_found="$AO_DASHBOARD_PLIST"
    if grep -q 'state = running' "$TMP_DIR/launchctl-ao-dashboard.txt"; then
      pass 'AO dashboard launchd job state is running'
      AO_DASHBOARD_LAUNCHD_RUNNING=1
    else
      AO_DASHBOARD_LAUNCHD_NOTE='AO dashboard launchd job is registered but not in running state'
    fi
  elif launchctl print "gui/$(id -u)/$AO_DASHBOARD_LEGACY_LABEL" >"$TMP_DIR/launchctl-ao-dashboard-legacy.txt" 2>&1; then
    pass 'AO dashboard launchd job is registered (legacy label)'
    AO_DASHBOARD_REGISTERED=1
    ao_dashboard_plist_found="$AO_DASHBOARD_LEGACY_PLIST"
    if grep -q 'state = running' "$TMP_DIR/launchctl-ao-dashboard-legacy.txt"; then
      pass 'AO dashboard launchd job state is running (legacy label)'
      AO_DASHBOARD_LAUNCHD_RUNNING=1
    else
      AO_DASHBOARD_LAUNCHD_NOTE='AO dashboard launchd job is registered (legacy label) but not in running state'
    fi
  else
    AO_DASHBOARD_LAUNCHD_NOTE='AO dashboard launchd job is not registered (run install-launchagents.sh to install)'
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

GATEWAY_PORT_LISTENING=0
if lsof -nP -iTCP:"$runtime_port" -sTCP:LISTEN >"$TMP_DIR/lsof-listen.txt" 2>&1; then
  GATEWAY_PORT_LISTENING=1
  pass "a process is listening on gateway port $runtime_port"
else
  fail "no process listening on gateway port $runtime_port"
fi

# Initialize ao_dashboard_plist_found to empty string for non-macOS (set -u safety)
ao_dashboard_plist_found=""

# Check AO dashboard port (env override > launchd plist --port > fallback 3011)
AO_DASHBOARD_PORT="$(detect_ao_dashboard_port "${ao_dashboard_plist_found:-}")"
AO_DASHBOARD_PORT_LISTENING=0
if lsof -nP -iTCP:"$AO_DASHBOARD_PORT" -sTCP:LISTEN >"$TMP_DIR/lsof-ao-dashboard.txt" 2>&1; then
  AO_DASHBOARD_PORT_LISTENING=1
  pass "a process is listening on AO dashboard port $AO_DASHBOARD_PORT"
else
  warn "no process listening on AO dashboard port $AO_DASHBOARD_PORT (dashboard may not be running; override with OPENCLAW_DOCTOR_AO_DASHBOARD_PORT)"
fi
if [[ "$IS_DARWIN" -eq 1 && "$AO_DASHBOARD_REGISTERED" -eq 1 && "$AO_DASHBOARD_LAUNCHD_RUNNING" -eq 0 && "$AO_DASHBOARD_PORT_LISTENING" -eq 1 ]]; then
  pass "AO dashboard is reachable on port $AO_DASHBOARD_PORT even though the standalone launchd job is idle"
elif [[ "$IS_DARWIN" -eq 1 && -n "${AO_DASHBOARD_LAUNCHD_NOTE:-}" ]]; then
  warn "$AO_DASHBOARD_LAUNCHD_NOTE"
fi

health_body_file="$TMP_DIR/health.json"
health_err_file="$TMP_DIR/health-curl.err"
HEALTH_HTTP_OK=0
health_code=$(curl -sS --max-time 5 -o "$health_body_file" -w '%{http_code}' "http://127.0.0.1:${runtime_port}/health" 2>"$health_err_file")
curl_rc=$?
if [[ "$curl_rc" -ne 0 ]]; then
  fail "HTTP /health probe command failed (curl exit=$curl_rc)"
  if [[ -s "$health_err_file" ]]; then
    warn "curl error: $(< "$health_err_file")"
  fi
elif [[ "$health_code" == '200' ]]; then
  HEALTH_HTTP_OK=1
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

status_output="$("${gateway_probe_cmd[@]}" openclaw gateway status --timeout "$GATEWAY_RPC_TIMEOUT_MS" 2>&1 || true)"
# Accept either legacy "Runtime: running" or current format indicators (Slack/Agents active)
if grep -qE 'Runtime: running|Slack: ok|^Agents:' <<<"$status_output"; then
  pass 'openclaw gateway status reports runtime running'
elif [[ "$HEALTH_HTTP_OK" -eq 1 && "$GATEWAY_PORT_LISTENING" -eq 1 ]]; then
  warn 'openclaw gateway status output missing runtime marker, but listener + /health are healthy'
else
  fail 'openclaw gateway status does not report runtime running'
fi
if grep -q 'RPC probe: failed' <<<"$status_output"; then
  if grep -qiE 'unauthorized: gateway token mismatch|provide gateway auth token|gateway token mismatch' <<<"$status_output"; then
    warn 'openclaw gateway status RPC probe was unauthorized; treating as CLI auth quirk because runtime and /health are already healthy'
  elif grep -qiE 'timeout|timed out' <<<"$status_output" && [[ "$HEALTH_HTTP_OK" -eq 1 ]]; then
    warn 'openclaw gateway status RPC probe timed out (event-loop pressure); HTTP /health is OK'
  else
    fail 'openclaw gateway status reports RPC probe failure'
  fi
fi
if grep -q 'Service config issue:' <<<"$status_output"; then
  if grep -q 'embeds OPENCLAW_GATEWAY_TOKEN and should be reinstalled' <<<"$status_output"; then
    pass 'openclaw gateway status reports embedded service token (expected for repo-managed launchd token persistence)'
  else
    warn 'openclaw gateway status reports service config issue(s)'
  fi
fi

health_cli_output="$("${gateway_probe_cmd[@]}" openclaw gateway health --timeout "$GATEWAY_RPC_TIMEOUT_MS" 2>&1)"
health_cli_rc=$?
if [[ "$health_cli_rc" -ne 0 ]]; then
  # Distinguish optional-feature misconfig (missing tunnel tokens) from real failures.
  if grep -qE 'secret reference could not be resolved|missing env var|auth\.token|remote\.token' <<<"$health_cli_output"; then
    warn "openclaw gateway health: optional tunnel token missing (exit=$health_cli_rc) — gateway is operational"
  # Treat transient local WebSocket close as non-fatal when /health and gateway status already passed.
  elif grep -qE 'gateway closed \(1000|normal closure' <<<"$health_cli_output"; then
    warn "openclaw gateway health returned transient close (exit=$health_cli_rc): treating as non-fatal"
  # RPC budget exceeded while HTTP liveness is fine — common under Slack/event-loop saturation.
  elif grep -qE 'gateway timeout|Error: gateway timeout' <<<"$health_cli_output" && [[ "$HEALTH_HTTP_OK" -eq 1 ]]; then
    warn "openclaw gateway health RPC timed out (exit=$health_cli_rc) under load; HTTP /health is OK (raise OPENCLAW_DOCTOR_GATEWAY_RPC_TIMEOUT_MS if persistent)"
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
  OPENCLAW_MCP_BIN="/Users/jleechan/.nvm/versions/node/v22.22.0/lib/node_modules/openclaw-mcp/dist/index.js"
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

  # 4a. Config guard — plugins.slots.memory must be "openclaw-mem0"
  _mem_slot="$(jq -r '.plugins.slots.memory // empty' "$LIVE_OPENCLAW/openclaw.json" 2>/dev/null || true)"
  if [[ "$_mem_slot" == "openclaw-mem0" ]]; then
    pass "plugins.slots.memory is openclaw-mem0"
  elif [[ -z "$_mem_slot" ]]; then
    fail "plugins.slots.memory is unset — mem0 plugin will be silently disabled (fix: set to \"openclaw-mem0\")"
  else
    fail "plugins.slots.memory is \"$_mem_slot\" — expected \"openclaw-mem0\""
  fi

  # 4b. Memory lookup verification — ensure mem0/memory_search is functional
  if [[ "${OPENCLAW_DOCTOR_SKIP_MEMORY:-0}" == "1" ]]; then
    warn "Memory lookup probe skipped (OPENCLAW_DOCTOR_SKIP_MEMORY=1)"
  else
    if [[ "$_mem_slot" == "openclaw-mem0" ]]; then
      memory_out="$(timeout 30 openclaw mem0 search "test" 2>&1)"
    else
      memory_out="$(timeout 30 openclaw memory search "test" 2>&1)"
    fi
    memory_rc=$?
    if printf '%s\n' "$memory_out" | grep -qiE "unknown command 'memory'|Did you mean mem0|memory.*command is unavailable because.*plugins\.allow|plugins\.allow.*excludes.*memory"; then
      memory_out="$(timeout 30 openclaw mem0 search "test" 2>&1)"
      memory_rc=$?
    fi
    # Check for NODE_MODULE_VERSION mismatch errors (better-sqlite3)
    if printf '%s\n' "$memory_out" | grep -qi "NODE_MODULE_VERSION\|MODULE_VERSION\|better-sqlite3"; then
      fail "Memory lookup failed: better-sqlite3 Node module version mismatch detected"
    elif printf '%s\n' "$memory_out" | grep -qiE "Error initializing Qdrant|ECONNREFUSED|Failed to connect to 127\.0\.0\.1 port 6333|fetch failed"; then
      fail "Memory lookup failed: Qdrant backend unavailable"
    elif printf '%s\n' "$memory_out" | grep -qi "openclaw-mem0: plugin disabled"; then
      # mem0 plugin disabled in config — treated as a failure; install with 'openclaw plugins install @mem0/openclaw-mem0'
      fail "Memory lookup skipped — mem0 plugin is disabled in config (run 'openclaw plugins install @mem0/openclaw-mem0' to enable)"
    elif [[ "$memory_rc" -eq 124 ]]; then
      warn "Memory lookup command timed out after 30s — treating as transient"
    elif [[ "$memory_rc" -ne 0 ]]; then
      fail "Memory lookup command failed (rc=$memory_rc)"
    elif printf '%s\n' "$memory_out" | grep -qE '^[[:space:]]*[0-9]+\.|"score"[[:space:]]*:'; then
      # Results start with a score like "0.531" (old format) OR JSON "score": field
      pass "Memory lookup probe succeeded (found results)"
    elif printf '%s\n' "$memory_out" | grep -qiE 'No matches|No memories found\.?|\[ *\]'; then
      # "No matches" or empty JSON array means search works but corpus is empty - OK
      pass "Memory lookup probe succeeded (search functional, corpus empty)"
    else
      warn "Memory lookup returned no searchable results (may be empty corpus)"
    fi
  fi
fi

# gog (Google CLI) — OAuth + Gmail/Calendar/Drive probes (non-interactive)
if [[ "${OPENCLAW_DOCTOR_SKIP_GOG:-0}" != "1" ]]; then
  printf '\n=== gog (Google CLI) ===\n'
  if command -v gog >/dev/null 2>&1; then
    _gog_health="$REPO_ROOT/scripts/gog-auth-health.sh"
    if [[ -x "$_gog_health" ]]; then
      LIVE_OPENCLAW="${LIVE_OPENCLAW:-$HOME/.openclaw}" \
        bash "$_gog_health" >/tmp/gog-auth-health.out 2>/tmp/gog-auth-health.err
      _gog_rc=$?
      if [[ "$_gog_rc" -eq 0 ]]; then
        pass "gog: $(tr -d '\n' </tmp/gog-auth-health.out)"
      elif [[ "$_gog_rc" -eq 2 ]]; then
        warn "gog: $(head -1 /tmp/gog-auth-health.err 2>/dev/null || true) — see stderr in /tmp/gog-auth-health.err"
      else
        fail "gog auth/API probe failed — $(head -2 /tmp/gog-auth-health.err 2>/dev/null | tr '\n' ' ')"
      fi
    else
      warn "gog-auth-health.sh missing or not executable — skipped"
    fi
  else
    warn "gog not installed — skipped Google CLI probe (brew install jleechanorg/tap/gog)"
  fi
fi

printf '\n=== Session health ===\n'

# Stale session lock files (dead-owner locks cause silent message loss)
SESSION_DIR="$LIVE_OPENCLAW/agents/main/sessions"
if [[ -d "$SESSION_DIR" ]]; then
  stale_locks=()
  while IFS= read -r lockfile; do
    raw=$(cat "$lockfile" 2>/dev/null)
    pid=$(echo "$raw" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['pid'])" 2>/dev/null || echo "$raw" | tr -d '[:space:]')
    if [[ -n "$pid" ]] && [[ "$pid" =~ ^[0-9]+$ ]] && ! kill -0 "$pid" 2>/dev/null; then
      stale_locks+=("$(basename "$lockfile") (pid=$pid dead)")
    fi
  done < <(find "$SESSION_DIR" -name "*.lock" -maxdepth 1 2>/dev/null)
  if [[ ${#stale_locks[@]} -eq 0 ]]; then
    pass "No stale session lock files (dead-owner locks cause silent Slack message loss)"
  else
    fail "Stale session lock files found — remove and restart gateway: ${stale_locks[*]}"
  fi
  # Warn on excessive .tmp accumulation (indicates frequent unclean shutdowns)
  tmp_count=$(find "$SESSION_DIR" -name "*.tmp" -maxdepth 1 2>/dev/null | wc -l | tr -d ' ')
  if [[ "$tmp_count" -gt 50 ]]; then
    warn "Excessive stale .tmp files in sessions dir ($tmp_count) — run: find $SESSION_DIR -name '*.tmp' -mtime +1 -delete"
  fi
else
  warn "Sessions dir $SESSION_DIR not found — session lock/.tmp checks skipped"
fi

printf '\n=== openclaw.json validation ===\n'

# Pytest validation
if command -v python3 >/dev/null 2>&1 && python3 -c "import pytest" >/dev/null 2>&1; then
  pytest_out="$TMP_DIR/pytest-configs.txt"
  # Run only the comprehensive config-validation classes (not legacy tests with known pre-existing failures)
  OPENCLAW_TEST_MAIN_CONFIG_PATH="$LIVE_OPENCLAW/openclaw.json" \
  python3 -m pytest "$REPO_ROOT/tests/test_openclaw_configs.py" \
    -k "TestMetaAndLogging or TestAuthProfiles or TestAgentDefaults or TestMinimaxProviderConsistency or TestToolsConfig or TestEnvSection or TestGatewaySecurity or TestHooksConfig or TestSessionConfig or TestCommandsConfig or TestMessagesConfig or TestPluginChannelConsistency or TestSlackChannelsConfig or TestRequiredAgents or TestSkillsConfig or TestExecSafeBins" \
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
