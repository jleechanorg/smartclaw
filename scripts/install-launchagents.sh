#!/usr/bin/env bash
# Install OpenClaw services from this repo (macOS: LaunchAgents, Linux: systemd user units)
# Usage: ./scripts/install-launchagents.sh [--mc-token <token>]
#
# Installs:
#   - ai.smartclaw.qdrant              (qdrant Docker container, port 6333, for mem0)
#   - com.smartclaw.gateway            (openclaw gateway, port 18789, via openclaw CLI)
#   - ai.smartclaw.webhook             (webhook daemon, port 19888, GitHub webhook ingress)
#   - ai.smartclaw.startup-check       (startup verification on login)
#   - ai.smartclaw.monitor-agent       (periodic health monitoring, hourly)
#   - ai.smartclaw.mission-control     (MC backend, port 9010)
#   - ai.smartclaw.mission-control-frontend (MC frontend, port 3000)
#
# The MC token is read from ~/.smartclaw/openclaw.json if not passed explicitly.
# Gateway token is hardcoded in ~/.smartclaw/openclaw.json — the gateway reads it
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
# All plists and scripts tracked in the repo live at the repo root
# (openclaw-config/ was removed in 4e57fa88 — repo IS ~/.smartclaw now).
CONFIG_DIR="$REPO_DIR"
LAUNCHD_DIR="$HOME/Library/LaunchAgents"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
OPENCLAW_HOME="$HOME/.smartclaw"
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

if [[ "$OS" == "macos" ]] && ! is_valid_mc_token "$MC_TOKEN" && [[ -f "$LAUNCHD_DIR/ai.smartclaw.mission-control.plist" ]]; then
  MC_TOKEN=$(plutil -extract EnvironmentVariables.LOCAL_AUTH_TOKEN raw \
    -o - "$LAUNCHD_DIR/ai.smartclaw.mission-control.plist" 2>/dev/null || true)
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
p = pathlib.Path.home()/'.smartclaw'/'openclaw.json'
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
  # ORCH-k78 fix: Find a Python interpreter with mem0/qdrant-client.
  # Never prefer macOS system Python (/usr/bin/python3) for launchd services.
  local python_path candidate

  # Prefer Homebrew Python when it has required packages.
  if /opt/homebrew/bin/python3 -c "import mem0; import qdrant_client" 2>/dev/null; then
    python_path="/opt/homebrew/bin/python3"
    echo "$python_path"
    return 0
  fi

  # Fall back to whichever python3 is on PATH if it has required packages.
  candidate="$(command -v python3 2>/dev/null || true)"
  # ORCH-k78: explicitly skip /usr/bin/python3 — never use macOS system Python for mem0.
  if [[ -n "$candidate" && "$candidate" != "/usr/bin/python3" ]] && \
     "$candidate" -c "import mem0; import qdrant_client" 2>/dev/null; then
    python_path="$candidate"
    echo "$python_path"
    return 0
  fi

  # No suitable interpreter found — abort so the caller can prompt for installation.
  echo "ERROR: detect_python_with_mem0: no Python with mem0+qdrant-client found." >&2
  echo "Install mem0: pip install mem0 qdrant-client" >&2
  return 1
}

# Detect python path once at script start; abort if no suitable interpreter found.
# Only needed on macOS — Linux systemd units use PATH resolution.
if [[ "$OS" == "macos" ]]; then
  PYTHON_PATH_FOR_LAUNCHD="$(detect_python_with_mem0)" || {
    echo "ERROR: install-launchagents.sh requires a Python with mem0 and qdrant-client." >&2
    exit 1
  }
  echo "Using Python with mem0: $PYTHON_PATH_FOR_LAUNCHD"
fi

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

# Detect whether openclaw binary is in launchd's PATH (launchd doesn't inherit shell env).
# Returns the dir + ":" if non-standard, else empty string.
# NOTE: Duplicated from install-openclaw-scheduled-jobs.sh (same logic, independent scripts).
# Both may run standalone and must each compute OPENCLAW_EXTRA_PATH independently.
_detect_openclaw_extra_path() {
  local bin_path bin_dir
  bin_path="$(command -v openclaw 2>/dev/null || true)"
  [[ -z "$bin_path" ]] && echo "" && return
  bin_dir="$(dirname "$bin_path")"
  case ":$HOME/.bun/bin:$HOME/.local/bin:$HOME/bin:$HOME/Library/pnpm:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:" in
    *":${bin_dir}:"*) echo "" ;;
    *)                echo "${bin_dir}:" ;;
  esac
}

OPENCLAW_EXTRA_PATH="$(_detect_openclaw_extra_path)"

# Resolve openclaw binary path. Uses command -v (which checks PATH) as primary;
# explicit paths as fallbacks for machines where openclaw lives outside PATH.
_openclaw_resolve() {
  local candidate
  # 1. PATH lookup
  candidate="$(command -v openclaw 2>/dev/null || true)"
  if [[ -n "$candidate" && -x "$candidate" ]]; then
    printf '%s\n' "$candidate"
    return 0
  fi
  # 2. Explicitly check nvm/pnpm/bun/Homebrew locations (startup-check.sh compatible)
  for candidate in \
    "$HOME/.nvm/versions/node/current/bin/openclaw" \
    "$HOME/.nvm/versions/node/v22.22.0/bin/openclaw" \
    "$HOME/Library/pnpm/openclaw" \
    "$HOME/.bun/bin/openclaw" \
    "/opt/homebrew/bin/openclaw" \
    "/usr/local/bin/openclaw"; do
    if [[ -x "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  # 3. Not found
  return 1
}

OPENCLAW_BIN="$(_openclaw_resolve || true)"

# Validate early — OPENCLAW_BIN is substituted into plist templates via sed;
# an empty value produces a broken plist (empty ProgramArguments).
if [[ -z "$OPENCLAW_BIN" ]] || [[ ! -x "$OPENCLAW_BIN" ]]; then
  echo "ERROR: openclaw binary not found or not executable." >&2
  echo "  Resolved: '${OPENCLAW_BIN:-<empty>}'" >&2
  echo "  Check PATH, or install openclaw before running this script." >&2
  exit 1
fi

# Detect bash binary for launchd plists that use bash -l -c.
# Prefer Homebrew bash (newer, consistent with @HOMEBREW_BASH@ used in ao-manager plist).
if [[ -x /opt/homebrew/bin/bash ]]; then
  HOMEBREW_BASH="/opt/homebrew/bin/bash"
elif [[ -x /usr/local/bin/bash ]]; then
  HOMEBREW_BASH="/usr/local/bin/bash"
else
  HOMEBREW_BASH="/bin/bash"
fi

install_plist() {
  local src="$1"
  local base label
  base="$(basename "$src")"
  case "$base" in
    *.plist.template) label="${base%.plist.template}" ;;
    *.plist)          label="${base%.plist}" ;;
    *)                label="$base" ;;
  esac
  local dst="$LAUNCHD_DIR/$label.plist"

  # AO dashboard lives in agent-orchestrator repo
  AO_DASHBOARD_DIR="${AO_DASHBOARD_DIR:-${HOME}/projects_reference/agent-orchestrator/packages/web}"

  mkdir -p "$LAUNCHD_DIR" "$HOME/.smartclaw/logs" "$HOME/.smartclaw/logs/scheduled-jobs"
  sed \
    -e "s|PLACEHOLDER_MC_TOKEN|$(_esc_sed "$MC_TOKEN")|g" \
    -e "s|@HOME@|$(_esc_sed "$HOME")|g" \
    -e "s|@REPO_ROOT@|$(_esc_sed "$REPO_DIR")|g" \
    -e "s|@PYTHON_PATH@|$(_esc_sed "$PYTHON_PATH_FOR_LAUNCHD")|g" \
    -e "s|@PYTHON3_PATH@|$(_esc_sed "$PYTHON_PATH_FOR_LAUNCHD")|g" \
    -e "s|@NODE_BIN_DIR@|$(_esc_sed "$NODE_BIN_DIR_FOR_LAUNCHD")|g" \
    -e "s|@NODE_PATH@|$(_esc_sed "$NODE_PATH_FOR_LAUNCHD")|g" \
    -e "s|@AO_DASHBOARD_DIR@|$(_esc_sed "$AO_DASHBOARD_DIR")|g" \
    -e "s|@OPENCLAW_EXTRA_PATH@|$(_esc_sed "$OPENCLAW_EXTRA_PATH")|g" \
    -e "s|@OPENCLAW_BIN@|$(_esc_sed "$OPENCLAW_BIN")|g" \
    -e "s|@HOMEBREW_BASH@|$(_esc_sed "$HOMEBREW_BASH")|g" \
    "$src" > "$dst"

  # Try bootstrap; if it fails, bootout then re-bootstrap.
  # Propagate final exit status so callers can verify load succeeded.
  local result=0
  if ! launchctl bootstrap "gui/$(id -u)" "$dst" 2>/dev/null; then
    launchctl bootout "gui/$(id -u)" "$dst" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$dst" || result=$?
  fi
  if [[ $result -eq 0 ]]; then
    echo "  ✓ $label loaded"
  else
    echo "  ✗ $label FAILED to load (exit $result)" >&2
    return $result
  fi
}

echo "Installing services ($OS)..."

# --- qdrant (mem0 vector store) ---
QDRANT_PLIST_TEMPLATE="$REPO_DIR/launchd/ai.smartclaw.qdrant.plist.template"
QDRANT_INSTALLED=0
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
    dst="$LAUNCHD_DIR/ai.smartclaw.qdrant.plist"
    sed -e "s|@HOME@|$(_esc_sed "$HOME")|g" "$QDRANT_PLIST_TEMPLATE" > "$dst"
    launchctl bootout "gui/$(id -u)" "$dst" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$dst"
    echo "  ✓ ai.smartclaw.qdrant installed (qdrant on port 6333)"
    QDRANT_INSTALLED=1
  else
    install_systemd_service "openclaw-qdrant" \
      "/bin/bash $OPENCLAW_HOME/scripts/start-qdrant-container.sh" \
      "oneshot" "" "qdrant"
    QDRANT_INSTALLED=1
  fi
else
  echo "  • skipping ai.smartclaw.qdrant (template not found: $QDRANT_PLIST_TEMPLATE)"
fi

# --- webhook daemon (GitHub webhook ingress + remediation worker) ---
WEBHOOK_PLIST_TEMPLATE="$REPO_DIR/launchd/ai.smartclaw.webhook.plist.template"
WEBHOOK_INSTALLED=0
if [[ "$OS" == "macos" ]]; then
    if [[ -f "$WEBHOOK_PLIST_TEMPLATE" ]]; then
        dst="$LAUNCHD_DIR/ai.smartclaw.webhook.plist"
        # Read webhookSecret from webhook.json (gitignored — not in openclaw.json which has strict schema)
        WEBHOOK_SECRET="$(python3 -c "import json,os; p=os.path.expanduser('~/.smartclaw/webhook.json'); d=json.load(open(p)) if os.path.exists(p) else {}; print(d.get('webhookSecret',''))" 2>/dev/null || true)"
        if [[ -z "$WEBHOOK_SECRET" ]]; then
          WEBHOOK_SECRET="$(openssl rand -hex 32)"
          python3 -c "
import json, os
secret = '$WEBHOOK_SECRET'
p = os.path.expanduser('~/.smartclaw/webhook.json')
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
        echo "  ✓ ai.smartclaw.webhook installed (webhook daemon on port 19888)"
        WEBHOOK_INSTALLED=1
    else
        echo "  • skipping ai.smartclaw.webhook (template not found: $WEBHOOK_PLIST_TEMPLATE)"
    fi
else
    # webhook_daemon.py is retired — use agent-orchestrator's GitHub poller plugin instead.
    # See agent-orchestrator.yaml for configuration.
    echo "  • skipping openclaw-webhook on Linux (webhook_daemon.py is retired — use AO poller-github-pr plugin)"
    WEBHOOK_INSTALLED=0
fi

# --- production directory setup ---
# ~/.smartclaw/ = staging (repo checkout), ~/.smartclaw_prod/ = production (separate dir)
PROD_DIR="$HOME/.smartclaw_prod"
if [[ ! -d "$PROD_DIR" ]]; then
  echo "  Creating production directory: $PROD_DIR"
  mkdir -p "$PROD_DIR/logs"
  # Seed prod config from staging if it doesn't exist
  if [[ -f "$HOME/.smartclaw/openclaw.json" ]] && [[ ! -f "$PROD_DIR/openclaw.json" ]]; then
    cp "$HOME/.smartclaw/openclaw.json" "$PROD_DIR/openclaw.json"
    echo "  Seeded $PROD_DIR/openclaw.json from staging"
  fi
  # Symlink shared resources
  for target in SOUL.md TOOLS.md HEARTBEAT.md extensions agents credentials lcm.db; do
    src="$HOME/.smartclaw/$target"
    dst="$PROD_DIR/$target"
    if [[ -e "$src" ]] && [[ ! -e "$dst" ]]; then
      ln -sf "$src" "$dst"
    fi
  done
  echo "  ✓ Production directory initialized"
else
  mkdir -p "$PROD_DIR/logs"
fi

# --- gateway (production) ---
# Production gateway reads from ~/.smartclaw_prod/openclaw.json.
# Do NOT inject tokens into plists; openclaw.json is the single source of truth.
if [[ "$OS" == "macos" ]]; then
  # com.smartclaw.gateway: KeepAlive plist, logs to ~/.smartclaw_prod/logs/gateway.log
  # OPENCLAW_BIN is already validated at lines 389-394 (exit 1 if missing) — no redundant check needed.
  mkdir -p "$HOME/.smartclaw/logs" "$PROD_DIR/logs"
  # Verify the new plist installs successfully BEFORE tearing down the old gateway.
  # install_plist now propagates failure — if it fails we leave the old gateway running.
  if install_plist "$REPO_DIR/launchd/com.smartclaw.gateway.plist"; then
    # Migration succeeded — now safely tear down the legacy gateway.
    launchctl bootout "gui/$(id -u)/ai.smartclaw.gateway" 2>/dev/null || true
    # Remove the old plist so launchd doesn't re-load it on reboot.
    rm -f "$LAUNCHD_DIR/ai.smartclaw.gateway.plist"
  else
    echo "ERROR: new gateway plist failed to load; leaving legacy gateway running." >&2
  fi
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

# --- staging gateway ---
# ~/.smartclaw/ IS the staging environment. Staging gateway runs on port 18810
# using openclaw.json from ~/.smartclaw/. Production uses ~/.smartclaw_prod/.
if [[ "$OS" == "macos" ]]; then
  # Remove the auto-generated plist from staging-gateway.sh (now managed by installer)
  if [[ -f "$HOME/.smartclaw/ai.smartclaw.staging.plist" ]]; then
    launchctl bootout "gui/$(id -u)/ai.smartclaw.staging" 2>/dev/null || true
    rm -f "$LAUNCHD_DIR/ai.smartclaw.staging.plist"
  fi
  install_plist "$REPO_DIR/launchd/ai.smartclaw.staging.plist"
else
  echo "  • skipping staging gateway on Linux (not yet implemented)"
fi

# --- startup check ---
install_startup_check_script
if [[ "$OS" == "macos" ]]; then
  install_plist "$CONFIG_DIR/ai.smartclaw.startup-check.plist"
else
  install_systemd_service "openclaw-startup-check" \
    "/bin/bash $OPENCLAW_HOME/startup-check.sh" \
    "oneshot" "" "startup-check"
fi

# --- monitor-agent (periodic health monitoring) ---
MONITOR_AGENT_INSTALLED=0
if [[ "$OS" == "macos" ]]; then
  MONITOR_AGENT_PLIST="$REPO_DIR/launchd/ai.smartclaw.monitor-agent.plist"
  if [[ -f "$MONITOR_AGENT_PLIST" ]]; then
    install_plist "$MONITOR_AGENT_PLIST"
    MONITOR_AGENT_INSTALLED=1
  else
    echo "  • skipping ai.smartclaw.monitor-agent (plist not found: $MONITOR_AGENT_PLIST)"
  fi
else
  # Linux: install systemd timer for hourly health monitoring
  install_systemd_timer "openclaw-monitor-agent" \
    "/bin/bash $OPENCLAW_HOME/monitor-agent.sh" \
    "hourly"
  MONITOR_AGENT_INSTALLED=1
fi

# --- AO dashboard (KeepAlive web UI) ---
AGENTO_DASHBOARD_INSTALLED=0
if [[ "$OS" == "macos" ]]; then
  AGENTO_DASHBOARD_PLIST="$REPO_DIR/launchd/ai.agento.dashboard.plist.template"
  if [[ -f "$AGENTO_DASHBOARD_PLIST" ]]; then
    install_plist "$AGENTO_DASHBOARD_PLIST"
    echo "  ✓ ai.agento.dashboard installed"
    AGENTO_DASHBOARD_INSTALLED=1
  else
    echo "  • skipping ai.agento.dashboard (template not found: $AGENTO_DASHBOARD_PLIST)"
  fi
else
  echo "  • skipping ai.agento.dashboard (Linux not yet implemented)"
fi

# --- Claude memory sync (background service, every-15-min sync) ---
# Does not use ai.smartclaw.schedule.* naming — it's a persistent background service.
# launchd is macOS-only; no Linux equivalent (systemd timers handle scheduled jobs).
if [[ "$OS" == "macos" ]]; then
  MEMORY_SYNC_PLIST="$REPO_DIR/launchd/ai.smartclaw.claude-memory-sync.plist.template"
  if [[ -f "$MEMORY_SYNC_PLIST" ]]; then
    install_plist "$MEMORY_SYNC_PLIST"
    echo "  ✓ ai.smartclaw.claude-memory-sync installed"
  else
    echo "  • skipping ai.smartclaw.claude-memory-sync (template not found)"
  fi
else
  echo "  • skipping ai.smartclaw.claude-memory-sync (launchd is macOS-only)"
fi

# --- scheduled jobs ---
SCHEDULED_JOBS_INSTALLED=0
# Skip if called from the central installer (install-openclaw-launchd.sh), which
# runs install-openclaw-scheduled-jobs.sh directly in Step 2 to avoid double-install.
if [[ "${CALLED_AS_PART_OF_CENTRAL:-0}" == "1" ]]; then
  echo "  • skipping nested scheduled-jobs install (central installer handles this in Step 2)"
elif [[ "$OS" == "macos" ]]; then
  SCHEDULE_INSTALLER="$REPO_DIR/scripts/install-openclaw-scheduled-jobs.sh"
  if [[ -x "$SCHEDULE_INSTALLER" ]]; then
    LOCAL_TZ="$(detect_local_timezone)"
    if [[ "$LOCAL_TZ" != "America/Los_Angeles" && "${OPENCLAW_ALLOW_NON_PT_SCHEDULE:-0}" != "1" ]]; then
      echo "  • skipping scheduled job migration: local timezone '$LOCAL_TZ' differs from America/Los_Angeles"
      echo "    set OPENCLAW_ALLOW_NON_PT_SCHEDULE=1 to override"
    else
      "$SCHEDULE_INSTALLER"
      SCHEDULED_JOBS_INSTALLED=1
    fi
  else
    echo "  • skipping scheduled job migration (installer not found: $SCHEDULE_INSTALLER)"
  fi
else
  # Linux: install systemd timers for all scheduled jobs
  # Each timer directly executes the corresponding script in $OPENCLAW_HOME/scripts,
  # mirroring the macOS launchd scheduled jobs.
  # ── morning operational jobs ──────────────────────────────────────────────────
  install_systemd_timer "openclaw-schedule-morning-log-review" \
    "/bin/bash $OPENCLAW_HOME/scripts/morning-log-review.sh" \
    "Mon..Fri * 08:00:00 America/Los_Angeles"

  install_systemd_timer "openclaw-schedule-docs-drift-review" \
    "/bin/bash $OPENCLAW_HOME/scripts/docs-drift-review.sh" \
    "Mon..Fri * 08:15:00 America/Los_Angeles"

  install_systemd_timer "openclaw-schedule-cron-backup-sync" \
    "/bin/bash $OPENCLAW_HOME/scripts/cron-backup-sync.sh" \
    "Mon..Fri * 08:25:00 America/Los_Angeles"

  install_systemd_timer "openclaw-schedule-weekly-error-trends" \
    "/bin/bash $OPENCLAW_HOME/scripts/weekly-error-trends.sh" \
    "Mon * 09:00:00 America/Los_Angeles"

  install_systemd_timer "openclaw-schedule-daily-research" \
    "/bin/bash $OPENCLAW_HOME/scripts/daily-openclaw-research.sh" \
    "Mon..Fri * 18:00:00 America/Los_Angeles"
fi

# --- Mission Control (macOS only for now) ---
MC_BACKEND_PLIST="$CONFIG_DIR/ai.smartclaw.mission-control.plist"
MC_FRONTEND_PLIST="$CONFIG_DIR/ai.smartclaw.mission-control-frontend.plist"
if [[ "$OS" == "macos" ]]; then
  if [[ -f "$MC_BACKEND_PLIST" ]]; then
    install_plist "$MC_BACKEND_PLIST"
  else
    echo "  • skipping ai.smartclaw.mission-control (plist not found in openclaw-config/)"
  fi
  if [[ -f "$MC_FRONTEND_PLIST" ]]; then
    install_plist "$MC_FRONTEND_PLIST"
  else
    echo "  • skipping ai.smartclaw.mission-control-frontend (plist not found in openclaw-config/)"
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
  EXPECTED_LABELS=("com.smartclaw.gateway" "ai.smartclaw.startup-check")
  [[ "$QDRANT_INSTALLED" -eq 1 ]] && EXPECTED_LABELS+=("ai.smartclaw.qdrant")
  [[ "$WEBHOOK_INSTALLED" -eq 1 ]] && EXPECTED_LABELS+=("ai.smartclaw.webhook")
  [[ "$MONITOR_AGENT_INSTALLED" -eq 1 ]] && EXPECTED_LABELS+=("ai.smartclaw.monitor-agent")
  [[ "$AGENTO_DASHBOARD_INSTALLED" -eq 1 ]] && EXPECTED_LABELS+=("ai.agento.dashboard")

  # Scheduled job labels — only add if scheduled jobs were actually installed
  # NOTE: Keep this list in sync with launchd/ai.smartclaw.schedule.*.plist.template filenames
  if [[ "$SCHEDULED_JOBS_INSTALLED" -eq 1 ]]; then
    for tmpl in "$REPO_DIR"/launchd/ai.smartclaw.schedule.*.plist.template; do
      [[ -f "$tmpl" ]] || continue
      label="${tmpl%.plist.template}"
      label="$(basename "$label")"
      EXPECTED_LABELS+=("$label")
    done
  fi

  # Also pick up any plist already in LAUNCHD_DIR (backwards compat)
  for plist in "$LAUNCHD_DIR"/ai.smartclaw.schedule.*.plist; do
    [[ -f "$plist" ]] || continue
    EXPECTED_LABELS+=("$(basename "$plist" .plist)")
  done
  [[ -f "$MC_BACKEND_PLIST" ]] && EXPECTED_LABELS+=("ai.smartclaw.mission-control")
  [[ -f "$MC_FRONTEND_PLIST" ]] && EXPECTED_LABELS+=("ai.smartclaw.mission-control-frontend")

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
    "openclaw-schedule-morning-log-review.timer"
    "openclaw-schedule-docs-drift-review.timer"
    "openclaw-schedule-cron-backup-sync.timer"
    "openclaw-schedule-weekly-error-trends.timer"
    "openclaw-schedule-daily-research.timer"
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
echo "  qdrant:        ~/.smartclaw/logs/qdrant.log"
echo "  gateway:       ~/.smartclaw/logs/gateway.log"
echo "  startup check: ~/.smartclaw/logs/startup-check.log"
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
