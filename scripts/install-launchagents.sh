#!/usr/bin/env bash
# Install OpenClaw services from this repo (macOS: LaunchAgents, Linux: systemd user units)
# Usage: ./scripts/install-launchagents.sh [--mc-token <token>]
#
# Installs:
#   - ai.openclaw.qdrant              (qdrant Docker container, port 6333, for mem0)
#   - ai.openclaw.gateway             (openclaw gateway, port 18789, via openclaw CLI)
#   - ai.openclaw.webhook             (webhook daemon, port 19888, GitHub webhook ingress)
#   - ai.openclaw.startup-check       (startup verification on login)
#   - ai.openclaw.monitor-agent       (periodic health monitoring, hourly)
#   - ai.openclaw.mission-control     (MC backend, port 9010)
#   - ai.openclaw.mission-control-frontend (MC frontend, port 3000)
#
# The MC token is read from ~/.openclaw/openclaw.json if not passed explicitly.
# Gateway token is hardcoded in ~/.openclaw/openclaw.json — the gateway reads it
# directly. Do NOT inject tokens into plists (single source of truth: openclaw.json).

set -euo pipefail

# --- OS detection ---
case "$(uname -s)" in
  Darwin) OS="macos" ;;
  Linux)  OS="linux" ;;
  *) echo "Unsupported OS: $(uname -s)" >&2; exit 1 ;;
esac
echo "Detected OS: $OS"

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG_DIR="$REPO_DIR/openclaw-config"
LAUNCHD_DIR="$HOME/Library/LaunchAgents"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
OPENCLAW_HOME="$HOME/.openclaw"
ENV_FILE="$REPO_DIR/.env"
# Linux PATH used in generated systemd units
LINUX_PATH="$HOME/.npm-global/bin:$HOME/.local/bin:$HOME/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

detect_local_timezone() {
  local target
  target="$(readlink /etc/localtime 2>/dev/null || true)"
  if [[ "$target" == *"/zoneinfo/"* ]]; then
    echo "${target##*/zoneinfo/}"
    return 0
  fi
  echo "${TZ:-unknown}"
}

is_valid_mc_token() {
  local token="${1:-}"
  [[ -n "$token" ]] && [[ ${#token} -ge 50 ]] && [[ "$token" != "your-local-auth-token-here" ]]
}

read_env_value() {
  local key="$1"
  if [[ -f "$ENV_FILE" ]]; then
    python3 - "$ENV_FILE" "$key" <<'PY'
import pathlib, sys
env_path = pathlib.Path(sys.argv[1])
key = sys.argv[2]
for line in env_path.read_text().splitlines():
    if line.startswith(f"{key}="):
        val = line.split("=", 1)[1]
        # Strip surrounding single or double quotes
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
            val = val[1:-1]
        print(val)
        break
PY
  fi
}

# --- resolve MC token ---
MC_TOKEN="${MC_TOKEN:-${LOCAL_AUTH_TOKEN:-}}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --mc-token)
      if [[ $# -lt 2 || -z "$2" || "$2" == --* ]]; then
        echo "Error: --mc-token requires a non-empty value" >&2
        exit 1
      fi
      MC_TOKEN="$2"
      shift 2
      ;;
    --gateway-token)
      if [[ $# -lt 2 || -z "$2" || "$2" == --* ]]; then
        echo "Error: --gateway-token requires a non-empty value" >&2
        exit 1
      fi
      # Gateway token is read directly from openclaw.json — this flag is a no-op
      shift 2
      ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

if [[ "$OS" == "macos" ]] && ! is_valid_mc_token "$MC_TOKEN" && [[ -f "$LAUNCHD_DIR/ai.openclaw.mission-control.plist" ]]; then
  MC_TOKEN=$(plutil -extract EnvironmentVariables.LOCAL_AUTH_TOKEN raw \
    -o - "$LAUNCHD_DIR/ai.openclaw.mission-control.plist" 2>/dev/null || true)
fi

if ! is_valid_mc_token "$MC_TOKEN"; then
  MC_TOKEN="$(read_env_value LOCAL_AUTH_TOKEN)"
fi

if ! is_valid_mc_token "$MC_TOKEN"; then
  MC_TOKEN="$(read_env_value MISSION_CONTROL_TOKEN)"
fi

if ! is_valid_mc_token "$MC_TOKEN"; then
  MC_TOKEN=$(python3 - <<'PY'
import json, pathlib
p = pathlib.Path.home()/'.openclaw'/'openclaw.json'
try:
    data = json.loads(p.read_text())
except Exception:
    print("")
else:
    print(data.get('env', {}).get('MISSION_CONTROL_TOKEN', ''))
PY
)
fi

if ! is_valid_mc_token "$MC_TOKEN"; then
  MC_TOKEN=$(python3 - <<'PY'
import secrets
print(secrets.token_hex(32))
PY
)
  echo "Generated new local Mission Control token for launchd services."
fi

echo "MC token: ${MC_TOKEN:0:8}... (${#MC_TOKEN} chars)"

# --- install startup helper + plists ---
_safe_install() {
  local src="$1" dst="$2" mode="$3"
  if [[ ! -f "$src" ]]; then
    echo "  • skipping install: $src not found"
    return 0
  fi
  if [[ "$(realpath "$src" 2>/dev/null)" == "$(realpath "$dst" 2>/dev/null)" ]]; then
    chmod "$mode" "$dst"
  else
    install -m "$mode" "$src" "$dst"
  fi
}

install_startup_check_script() {
  install -d "$OPENCLAW_HOME"
  mkdir -p "$OPENCLAW_HOME/logs" "$OPENCLAW_HOME/logs/scheduled-jobs"
  if [[ -f "$CONFIG_DIR/startup-check.sh" ]] && [[ "$(realpath "$CONFIG_DIR/startup-check.sh" 2>/dev/null)" != "$(realpath "$OPENCLAW_HOME/startup-check.sh" 2>/dev/null)" ]]; then
    install -m 755 "$CONFIG_DIR/startup-check.sh" "$OPENCLAW_HOME/startup-check.sh"
  else
    _safe_install "$CONFIG_DIR/startup-check.sh" "$OPENCLAW_HOME/startup-check.sh" 755
  fi
  _safe_install "$CONFIG_DIR/run-scheduled-job.sh" "$OPENCLAW_HOME/run-scheduled-job.sh" 755
  echo "  ✓ startup-check.sh installed"
  echo "  ✓ run-scheduled-job.sh installed"
}

detect_python_with_mem0() {
  # ORCH-k78 fix: Find python with mem0/qdrant-client installed
  # Prefer homebrew python, fallback to any python3 with required packages
  local python_path

  # Try homebrew python first
  if /opt/homebrew/bin/python3 -c "import mem0; import qdrant_client" 2>/dev/null; then
    python_path="/opt/homebrew/bin/python3"
  # Try system python3 if homebrew doesn't have the packages
  elif /usr/bin/python3 -c "import mem0; import qdrant_client" 2>/dev/null; then
    python_path="/usr/bin/python3"
  else
    # Fallback to whatever python3 is available - let it fail at runtime
    python_path="$(which python3 2>/dev/null || echo "/opt/homebrew/bin/python3")"
  fi

  echo "$python_path"
}

# Detect python path once at script start
PYTHON_PATH_FOR_LAUNCHD="$(detect_python_with_mem0)"
echo "Using Python with mem0: $PYTHON_PATH_FOR_LAUNCHD"

detect_node_bin_dir() {
  # Prefer nvm current symlink; fallback to resolved path of node binary
  local nvm_current="$HOME/.nvm/versions/node/current/bin"
  if [[ -x "$nvm_current/node" ]]; then
    echo "$nvm_current"
    return 0
  fi
  local node_path
  node_path="$(command -v node 2>/dev/null || true)"
  if [[ -n "$node_path" && -x "$node_path" ]]; then
    dirname "$node_path"
    return 0
  fi
  return 1
}

NODE_BIN_DIR_FOR_LAUNCHD="$(detect_node_bin_dir)" || {
  echo "ERROR: Node.js not found. Install Node or set PATH before running." >&2
  exit 1
}
NODE_PATH_FOR_LAUNCHD="$NODE_BIN_DIR_FOR_LAUNCHD/node"
echo "Using Node bin dir: $NODE_BIN_DIR_FOR_LAUNCHD"

_esc_sed() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/&/\\&/g; s/|/\\|/g'; }

# --- Linux: systemctl --user wrapper with SIGHUP+symlink fallback ---
_systemd_user_available() {
  systemctl --user status >/dev/null 2>&1
}

_reload_systemd_user() {
  if systemctl --user daemon-reload 2>/dev/null; then
    return 0
  fi
  # Fallback: SIGHUP the running systemd user instance
  local pid
  pid=$(pgrep -u "$(id -u)" -x systemd 2>/dev/null | head -n1 || true)
  if [[ -n "$pid" ]]; then
    kill -HUP "$pid" 2>/dev/null && sleep 1
  fi
}

_enable_unit() {
  local unit="$1"
  local target="${2:-default.target}"
  if systemctl --user enable "$unit" 2>/dev/null; then
    return 0
  fi
  # Fallback: create wants symlink manually
  local wants_dir="$SYSTEMD_USER_DIR/${target}.wants"
  mkdir -p "$wants_dir"
  ln -sf "../$unit" "$wants_dir/$unit" 2>/dev/null || true
}

_start_or_defer() {
  local unit="$1"
  local type="${2:-simple}"  # simple | oneshot | timer
  if systemctl --user start "$unit" 2>/dev/null; then
    return 0
  fi
  if [[ "$type" == "oneshot" ]]; then
    echo "  (note: $unit is oneshot — will run on next login)"
  else
    echo "  (note: $unit will start on next login — systemd --user D-Bus not reachable in this shell)"
  fi
}

# --- Linux: write a systemd user service unit and enable it ---
install_systemd_service() {
  local unit_name="$1"   # e.g. "openclaw-gateway"
  local exec_start="$2"
  local svc_type="${3:-simple}"  # simple | oneshot
  local extra_env="${4:-}"
  local log_base="${5:-$unit_name}"
  local restart_policy="no"
  [[ "$svc_type" == "simple" ]] && restart_policy="always"
  local unit_file="$SYSTEMD_USER_DIR/${unit_name}.service"

  mkdir -p "$SYSTEMD_USER_DIR" "$OPENCLAW_HOME/logs"
  cat > "$unit_file" <<EOF
[Unit]
Description=OpenClaw ${unit_name}
After=network.target

[Service]
Type=${svc_type}
ExecStart=${exec_start}
Restart=${restart_policy}
RestartSec=30
StandardOutput=append:${OPENCLAW_HOME}/logs/${log_base}.log
StandardError=append:${OPENCLAW_HOME}/logs/${log_base}.err.log
Environment=HOME=${HOME}
Environment=PATH=${LINUX_PATH}
${extra_env}
[Install]
WantedBy=default.target
EOF
  _reload_systemd_user
  _enable_unit "${unit_name}.service"
  _start_or_defer "${unit_name}.service" "$svc_type"
  echo "  ✓ ${unit_name}.service installed"
}

# --- Linux: write a systemd user timer+service pair and enable it ---
install_systemd_timer() {
  local unit_name="$1"   # e.g. "openclaw-schedule-backup-4h20"
  local exec_start="$2"
  local on_calendar="$3"  # may be multi-line (each line = one OnCalendar=)
  local extra_env="${4:-}"
  local log_name="${unit_name#openclaw-schedule-}"

  mkdir -p "$SYSTEMD_USER_DIR" "$OPENCLAW_HOME/logs/scheduled-jobs"

  # Service unit
  cat > "$SYSTEMD_USER_DIR/${unit_name}.service" <<EOF
[Unit]
Description=OpenClaw ${unit_name}

[Service]
Type=oneshot
ExecStart=${exec_start}
StandardOutput=append:${OPENCLAW_HOME}/logs/scheduled-jobs/${log_name}.out.log
StandardError=append:${OPENCLAW_HOME}/logs/scheduled-jobs/${log_name}.err.log
Environment=HOME=${HOME}
Environment=TZ=America/Los_Angeles
Environment=PATH=${LINUX_PATH}
${extra_env}
EOF

  # Timer unit — each line of on_calendar becomes one OnCalendar= entry
  {
    printf '[Unit]\nDescription=OpenClaw timer: %s\n\n[Timer]\n' "$unit_name"
    while IFS= read -r cal_entry; do
      [[ -n "$cal_entry" ]] && printf 'OnCalendar=%s\n' "$cal_entry"
    done <<< "$on_calendar"
    printf 'Persistent=true\n\n[Install]\nWantedBy=timers.target\n'
  } > "$SYSTEMD_USER_DIR/${unit_name}.timer"

  _reload_systemd_user
  _enable_unit "${unit_name}.timer" "timers.target"
  _start_or_defer "${unit_name}.timer" "timer"
  echo "  ✓ ${unit_name}.timer installed"
}

install_plist() {
  local src="$1"
  local label
  label=$(basename "$src" .plist)
  local dst="$LAUNCHD_DIR/$label.plist"

  mkdir -p "$LAUNCHD_DIR"
  sed \
    -e "s|PLACEHOLDER_MC_TOKEN|$(_esc_sed "$MC_TOKEN")|g" \
    -e "s|@HOME@|$(_esc_sed "$HOME")|g" \
    -e "s|@PYTHON_PATH@|$(_esc_sed "$PYTHON_PATH_FOR_LAUNCHD")|g" \
    -e "s|@PYTHON3_PATH@|$(_esc_sed "$PYTHON_PATH_FOR_LAUNCHD")|g" \
    -e "s|@NODE_BIN_DIR@|$(_esc_sed "$NODE_BIN_DIR_FOR_LAUNCHD")|g" \
    -e "s|@NODE_PATH@|$(_esc_sed "$NODE_PATH_FOR_LAUNCHD")|g" \
    "$src" > "$dst"

  if ! launchctl bootstrap "gui/$(id -u)" "$dst" 2>/dev/null; then
    launchctl bootout "gui/$(id -u)" "$dst" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$dst"
  fi
  echo "  ✓ $label loaded"
}

echo "Installing services ($OS)..."

# --- qdrant (mem0 vector store) ---
QDRANT_PLIST_TEMPLATE="$REPO_DIR/launchd/ai.openclaw.qdrant.plist.template"
# Install the qdrant start script regardless of OS (both paths use it)
if [[ -f "$QDRANT_PLIST_TEMPLATE" ]]; then
  if docker info >/dev/null 2>&1; then
    bash "$REPO_DIR/scripts/install-qdrant-container.sh"
  else
    echo "  • Docker not running — skipping qdrant container setup (will start on next login)"
  fi
  mkdir -p "$OPENCLAW_HOME/scripts"
  _src="$REPO_DIR/scripts/start-qdrant-container.sh"
  _dst="$OPENCLAW_HOME/scripts/start-qdrant-container.sh"
  if [[ "$(realpath "$_src" 2>/dev/null)" != "$(realpath "$_dst" 2>/dev/null)" ]]; then
    install -m 755 "$_src" "$_dst"
  else
    chmod 755 "$_dst"
  fi
  if [[ "$OS" == "macos" ]]; then
    dst="$LAUNCHD_DIR/ai.openclaw.qdrant.plist"
    sed -e "s|@HOME@|$(_esc_sed "$HOME")|g" "$QDRANT_PLIST_TEMPLATE" > "$dst"
    launchctl bootout "gui/$(id -u)" "$dst" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$dst"
    echo "  ✓ ai.openclaw.qdrant installed (qdrant on port 6333)"
  else
    install_systemd_service "openclaw-qdrant" \
      "/bin/bash $OPENCLAW_HOME/scripts/start-qdrant-container.sh" \
      "oneshot" "" "qdrant"
  fi
else
  echo "  • skipping ai.openclaw.qdrant (template not found: $QDRANT_PLIST_TEMPLATE)"
fi

# --- webhook daemon (GitHub webhook ingress + remediation worker) ---
WEBHOOK_PLIST_TEMPLATE="$REPO_DIR/launchd/ai.openclaw.webhook.plist.template"
WEBHOOK_INSTALLED=0
if [[ "$OS" == "macos" ]]; then
    if [[ -f "$WEBHOOK_PLIST_TEMPLATE" ]]; then
        dst="$LAUNCHD_DIR/ai.openclaw.webhook.plist"
        # Read webhookSecret from webhook.json (gitignored — not in openclaw.json which has strict schema)
        WEBHOOK_SECRET="$(python3 -c "import json,os; p=os.path.expanduser('~/.openclaw/webhook.json'); d=json.load(open(p)) if os.path.exists(p) else {}; print(d.get('webhookSecret',''))" 2>/dev/null || true)"
        if [[ -z "$WEBHOOK_SECRET" ]]; then
          WEBHOOK_SECRET="$(openssl rand -hex 32)"
          python3 -c "
import json, os
secret = '$WEBHOOK_SECRET'
p = os.path.expanduser('~/.openclaw/webhook.json')
d = json.load(open(p)) if os.path.exists(p) else {}
d['webhookSecret'] = secret
open(p, 'w').write(json.dumps(d, indent=2))
"
          echo "  ↳ Generated new GITHUB_WEBHOOK_SECRET and saved to webhook.json"
        fi
        sed \
          -e "s|@HOME@|$(_esc_sed "$HOME")|g" \
          -e "s|@PYTHON_PATH@|$(_esc_sed "$PYTHON_PATH_FOR_LAUNCHD")|g" \
          -e "s|@REPO_DIR@|$(_esc_sed "$REPO_DIR")|g" \
          -e "s|@WEBHOOK_SECRET@|$(_esc_sed "$WEBHOOK_SECRET")|g" \
          "$WEBHOOK_PLIST_TEMPLATE" > "$dst"
        launchctl bootout "gui/$(id -u)" "$dst" 2>/dev/null || true
        launchctl bootstrap "gui/$(id -u)" "$dst"
        echo "  ✓ ai.openclaw.webhook installed (webhook daemon on port 19888)"
        WEBHOOK_INSTALLED=1
    else
        echo "  • skipping ai.openclaw.webhook (template not found: $WEBHOOK_PLIST_TEMPLATE)"
    fi
else
    # webhook_daemon.py is retired — use agent-orchestrator's GitHub poller plugin instead.
    # See agent-orchestrator.yaml for configuration.
    echo "  • skipping openclaw-webhook on Linux (webhook_daemon.py is retired — use AO poller-github-pr plugin)"
    WEBHOOK_INSTALLED=0
fi

# --- gateway ---
# Token is hardcoded in ~/.openclaw/openclaw.json — the gateway reads it directly.
# Do NOT inject tokens into plists; openclaw.json is the single source of truth.
if [[ "$OS" == "macos" ]]; then
  openclaw gateway install --force --port 18789 >/dev/null
  PLIST="$LAUNCHD_DIR/ai.openclaw.gateway.plist"
  if [[ -f "$PLIST" ]]; then
    launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
  fi
  echo "  ✓ ai.openclaw.gateway installed via openclaw gateway install"
else
  # Linux: write systemd service for gateway
  GATEWAY_ENV="Environment=OPENCLAW_GATEWAY_PORT=18789
Environment=OPENCLAW_SERVICE_KIND=gateway
Environment=OPENCLAW_SERVICE_MARKER=openclaw
Environment=OPENCLAW_SYSTEMD_UNIT=openclaw-gateway.service
Environment=OPENCLAW_RAW_STREAM=1
Environment=OPENCLAW_RAW_STREAM_PATH=/tmp/openclaw/raw-stream.jsonl
Environment=GOOGLE_CLOUD_PROJECT=infinite-zephyr-487405-d0
Environment=MISSION_CONTROL_BASE_URL=http://localhost:9010
Environment=MISSION_CONTROL_BOARD_ID=aa68f729-d5e0-4d44-8c99-51bcebc0b8bc"
  install_systemd_service "openclaw-gateway" \
    "$(command -v openclaw) gateway run --port 18789 --bind loopback" \
    "simple" "$GATEWAY_ENV" "gateway"
fi

# --- startup check ---
install_startup_check_script
if [[ "$OS" == "macos" ]]; then
  install_plist "$CONFIG_DIR/ai.openclaw.startup-check.plist"
else
  install_systemd_service "openclaw-startup-check" \
    "/bin/bash $OPENCLAW_HOME/startup-check.sh" \
    "oneshot" "" "startup-check"
fi

# --- monitor-agent (periodic health monitoring) ---
MONITOR_AGENT_INSTALLED=0
if [[ "$OS" == "macos" ]]; then
  MONITOR_AGENT_PLIST="$REPO_DIR/launchd/ai.openclaw.monitor-agent.plist"
  if [[ -f "$MONITOR_AGENT_PLIST" ]]; then
    install_plist "$MONITOR_AGENT_PLIST"
    MONITOR_AGENT_INSTALLED=1
  else
    echo "  • skipping ai.openclaw.monitor-agent (plist not found: $MONITOR_AGENT_PLIST)"
  fi
else
  # Linux: install systemd timer for hourly health monitoring
  install_systemd_timer "openclaw-monitor-agent" \
    "/bin/bash $OPENCLAW_HOME/monitor-agent.sh" \
    "hourly"
  MONITOR_AGENT_INSTALLED=1
fi

# --- scheduled jobs ---
if [[ "$OS" == "macos" ]]; then
  SCHEDULE_INSTALLER="$REPO_DIR/scripts/install-openclaw-scheduled-jobs.sh"
  if [[ -x "$SCHEDULE_INSTALLER" ]]; then
    LOCAL_TZ="$(detect_local_timezone)"
    if [[ "$LOCAL_TZ" != "America/Los_Angeles" && "${OPENCLAW_ALLOW_NON_PT_SCHEDULE:-0}" != "1" ]]; then
      echo "  • skipping scheduled job migration: local timezone '$LOCAL_TZ' differs from America/Los_Angeles"
      echo "    set OPENCLAW_ALLOW_NON_PT_SCHEDULE=1 to override"
    else
      "$SCHEDULE_INSTALLER"
    fi
  else
    echo "  • skipping scheduled job migration (installer not found: $SCHEDULE_INSTALLER)"
  fi
else
  # Linux: install systemd timers for all scheduled jobs
  # Each timer maps to a run-scheduled-job.sh invocation matching the launchd plists
  RUNNER="$OPENCLAW_HOME/run-scheduled-job.sh"
  install_systemd_timer "openclaw-schedule-backup-4h20" \
    "/bin/bash $RUNNER 882c6964-1deb-4b4b-936d-9edcab83fbda" \
    "*-*-* 00:20:00 America/Los_Angeles
*-*-* 04:20:00 America/Los_Angeles
*-*-* 08:20:00 America/Los_Angeles
*-*-* 12:20:00 America/Los_Angeles
*-*-* 16:20:00 America/Los_Angeles
*-*-* 20:20:00 America/Los_Angeles"

  install_systemd_timer "openclaw-schedule-daily-checkin-9am" \
    "/bin/bash $RUNNER 522e23a7-c7c1-41f2-b117-a3af05661578" \
    "*-*-* 09:00:00 America/Los_Angeles"

  install_systemd_timer "openclaw-schedule-daily-checkin-12pm" \
    "/bin/bash $RUNNER 7424ea0d-2c8a-4a59-b58e-09b242c6c58e" \
    "*-*-* 12:00:00 America/Los_Angeles"

  install_systemd_timer "openclaw-schedule-daily-checkin-6pm" \
    "/bin/bash $RUNNER 5192e214-2754-49d5-b567-07c7b24cb116" \
    "*-*-* 18:00:00 America/Los_Angeles"

  install_systemd_timer "openclaw-schedule-genesis-memory-curation-weekly" \
    "/bin/bash $RUNNER genesis-memory-curation-weekly" \
    "Sun *-*-* 22:00:00 America/Los_Angeles"

  install_systemd_timer "openclaw-schedule-genesis-pattern-extraction-weekly" \
    "/bin/bash $RUNNER genesis-pattern-extraction-weekly" \
    "Sun *-*-* 07:30:00 America/Los_Angeles"
fi

# --- Mission Control (macOS only for now) ---
MC_BACKEND_PLIST="$CONFIG_DIR/ai.openclaw.mission-control.plist"
MC_FRONTEND_PLIST="$CONFIG_DIR/ai.openclaw.mission-control-frontend.plist"
if [[ "$OS" == "macos" ]]; then
  if [[ -f "$MC_BACKEND_PLIST" ]]; then
    install_plist "$MC_BACKEND_PLIST"
  else
    echo "  • skipping ai.openclaw.mission-control (plist not found in openclaw-config/)"
  fi
  if [[ -f "$MC_FRONTEND_PLIST" ]]; then
    install_plist "$MC_FRONTEND_PLIST"
  else
    echo "  • skipping ai.openclaw.mission-control-frontend (plist not found in openclaw-config/)"
  fi
else
  echo "  • skipping Mission Control services (Linux: not yet implemented)"
fi
echo ""
echo "Verifying..."
sleep 3
PORTS=(18789 6333)
[[ "$WEBHOOK_INSTALLED" -eq 1 ]] && PORTS+=(19888)
if [[ "$OS" == "macos" ]]; then
  [[ -f "$MC_BACKEND_PLIST" ]] && PORTS+=(9010)
  [[ -f "$MC_FRONTEND_PLIST" ]] && PORTS+=(3000)
fi
for port in "${PORTS[@]}"; do
  if ss -ltnp 2>/dev/null | grep -q ":${port}" || lsof -i ":$port" 2>/dev/null | grep -q LISTEN; then
    echo "  ✓ port $port listening"
  else
    echo "  ✗ port $port NOT listening — check logs"
  fi
done

echo ""
if [[ "$OS" == "macos" ]]; then
  echo "Verifying launchd labels..."
  EXPECTED_LABELS=("ai.openclaw.qdrant" "ai.openclaw.gateway" "ai.openclaw.startup-check")
  [[ "$WEBHOOK_INSTALLED" -eq 1 ]] && EXPECTED_LABELS+=("ai.openclaw.webhook")
  [[ "$MONITOR_AGENT_INSTALLED" -eq 1 ]] && EXPECTED_LABELS+=("ai.openclaw.monitor-agent")
  for plist in "$LAUNCHD_DIR"/ai.openclaw.schedule.*.plist; do
    [[ -f "$plist" ]] || continue
    EXPECTED_LABELS+=("$(basename "$plist" .plist)")
  done
  [[ -f "$MC_BACKEND_PLIST" ]] && EXPECTED_LABELS+=("ai.openclaw.mission-control")
  [[ -f "$MC_FRONTEND_PLIST" ]] && EXPECTED_LABELS+=("ai.openclaw.mission-control-frontend")

  missing=0
  for label in "${EXPECTED_LABELS[@]}"; do
    if launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
      echo "  ✓ $label registered"
    else
      echo "  ✗ $label NOT registered"
      missing=1
    fi
  done

  if [[ "$missing" -ne 0 ]]; then
    echo "ERROR: one or more expected launchd labels were not registered."
    exit 1
  fi
else
  echo "Verifying systemd user units..."
  EXPECTED_UNITS=(
    "openclaw-gateway.service"
    "openclaw-webhook.service"
    "openclaw-startup-check.service"
    "openclaw-schedule-backup-4h20.timer"
    "openclaw-schedule-daily-checkin-9am.timer"
    "openclaw-schedule-daily-checkin-12pm.timer"
    "openclaw-schedule-daily-checkin-6pm.timer"
    "openclaw-schedule-genesis-memory-curation-weekly.timer"
    "openclaw-schedule-genesis-pattern-extraction-weekly.timer"
  )
  missing=0
  for unit in "${EXPECTED_UNITS[@]}"; do
    unit_file="$SYSTEMD_USER_DIR/$unit"
    if systemctl --user is-enabled "$unit" >/dev/null 2>&1; then
      echo "  ✓ $unit enabled (systemctl)"
    elif [[ -f "$unit_file" ]] || [[ -L "$SYSTEMD_USER_DIR/default.target.wants/$unit" ]] || [[ -L "$SYSTEMD_USER_DIR/timers.target.wants/$unit" ]]; then
      echo "  ✓ $unit installed (unit file present)"
    else
      echo "  ✗ $unit NOT found"
      missing=1
    fi
  done

  if [[ "$missing" -ne 0 ]]; then
    echo "WARNING: one or more expected systemd units were not installed."
  fi
fi

echo ""
echo "Log locations:"
echo "  qdrant:        ~/.openclaw/logs/qdrant.log"
echo "  gateway:       ~/.openclaw/logs/gateway.log"
echo "  startup check: ~/.openclaw/logs/startup-check.log"
if [[ "$OS" == "macos" ]]; then
  echo "  MC backend:    /tmp/mc-backend.log"
  echo "  MC frontend:   /tmp/mc-frontend.log"
fi
echo ""
if [[ "$OS" == "linux" ]]; then
  echo "Useful Linux commands:"
  echo "  systemctl --user status openclaw-gateway"
  echo "  systemctl --user list-timers 'openclaw-schedule-*'"
  echo "  journalctl --user -u openclaw-gateway -f"
fi
