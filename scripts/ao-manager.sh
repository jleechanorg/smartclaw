#!/usr/bin/env bash
# ao-manager.sh — Unified AO lifecycle manager.
#
# Reads all projects from the rendered local AO config (~/.agent-orchestrator.yaml
# by default, or AO_CONFIG_PATH when overridden) and manages:
#   1. lifecycle-workers (one per project, via ao lifecycle-worker)
#   2. orchestrator tmux sessions (one per project, via ao start --no-dashboard)
#   3. dashboard (first project only, via ao start)
#   4. notifier (via ao-manager-notifier.sh)
#
# launchd plist: ~/Library/LaunchAgents/ai.agento.manager.plist
#   KeepAlive: true — launchd restarts if this script exits
#
# Usage:
#   ./ao-manager.sh                  # run interactively (foreground loop)
#   ./ao-manager.sh --once           # start everything once and exit (for testing)
#   ./ao-manager.sh --status        # print running component status
#
# Log: /tmp/ao-manager.log
set -uo pipefail

LOG="/tmp/ao-manager.log"
CONFIG_PATH="${AO_CONFIG_PATH:-$HOME/.agent-orchestrator.yaml}"
LOG_DIR="${AO_LOG_DIR:-$HOME/.smartclaw/logs}"
AO_BIN="${AO_BIN:-$HOME/bin/ao}"
MONITOR_INTERVAL="${AO_MONITOR_INTERVAL:-60}"   # seconds between health checks

# ── process check helpers ───────────────────────────────────────────────────

# Check if a process is running by command-line pattern.
# Filters out the grep process itself to avoid false positives.
is_running() {
  local pattern="$1"
  ps aux 2>/dev/null | grep -v "grep" | grep -qE "$pattern"
}

# ── helpers ──────────────────────────────────────────────────────────────────

log()  { echo "[$(date '+%H:%M:%S')] ao-manager: $*" | tee -a "$LOG"; }
logn() { printf "[%s] ao-manager: %s " "$(date '+%H:%M:%S')" "$*" | tee -a "$LOG"; }
ok()   { echo "✓ $*" | tee -a "$LOG"; }
err()  { echo "✗ ERROR: $*" | tee -a "$LOG" >&2; }

require_file() {
  [[ -f "$1" ]] || { err "Required file not found: $1"; exit 1; }
}

# ── config ───────────────────────────────────────────────────────────────────

# associative array: projectId -> sessionPrefix
declare -A PROJECT_PREFIXES

load_projects() {
  require_file "$CONFIG_PATH"

  # Read project IDs from YAML — capture to variable first so error
  # detection works reliably (process substitution errors aren't caught
  # by || the same way command substitution errors are).
  local raw_ids
  raw_ids=$(python3 - "$CONFIG_PATH" <<'PY'
import sys
import yaml

with open(sys.argv[1], encoding="utf-8") as f:
    cfg = yaml.safe_load(f)

for pid in cfg.get("projects", {}):
    print(pid)
PY
 2>&1) || {
    err "Failed to parse $CONFIG_PATH: $raw_ids"
    exit 1
  }
  mapfile -t PROJECT_IDS <<< "$raw_ids"

  if [[ ${#PROJECT_IDS[@]} -eq 0 ]]; then
    err "No projects found in $CONFIG_PATH"
    exit 1
  fi

  # Load session prefixes — capture and check for errors
  local raw_prefixes
  raw_prefixes=$(python3 - "$CONFIG_PATH" <<'PY'
import sys
import yaml

with open(sys.argv[1], encoding="utf-8") as f:
    cfg = yaml.safe_load(f)

for pid, proj in cfg.get("projects", {}).items():
    print(f"{pid}\t{proj.get('sessionPrefix', '')}")
PY
 2>&1) || {
    err "Failed to read session prefixes from $CONFIG_PATH"
    exit 1
  }
  while IFS=$'\t' read -r pid prefix; do
    PROJECT_PREFIXES["$pid"]="$prefix"
  done <<< "$raw_prefixes"

  log "Projects loaded: ${PROJECT_IDS[*]}"
}

get_prefix() {
  local project="$1"
  echo "${PROJECT_PREFIXES[$project]:-}"
}

# ── component starters ───────────────────────────────────────────────────────

start_lifecycle_worker() {
  local project="$1"
  local pidfile="$LOG_DIR/ao-lifecycle-$project.pid"

  # Skip if already running
  if is_running "ao lifecycle-worker ${project}$"; then
    log "lifecycle-worker $project — already running, skipping"
    return 0
  fi

  mkdir -p "$LOG_DIR"
  # Use bash -lc so ~/.bashrc is sourced before ao runs.
  # This ensures GITHUB_TOKEN, OPENCLAW_AO_HOOK_TOKEN, etc. are set.
  nohup bash -lc "exec $AO_BIN lifecycle-worker '$project'" \
    >> "$LOG_DIR/ao-lifecycle-$project.log" 2>&1 &
  local pid=$!
  echo $pid > "$pidfile"
  disown $pid 2>/dev/null || true
  log "lifecycle-worker $project started (pid $pid)"
}

start_orchestrator() {
  local project="$1"
  local prefix
  prefix=$(get_prefix "$project")
  if [[ -z "$prefix" ]]; then
    err "No sessionPrefix for project $project — cannot start orchestrator"
    return 1
  fi

  # Check if an orchestrator session for this project already exists.
  # Session names follow the pattern: {configHash}-{prefix}-orchestrator
  local existing
  existing=$(tmux list-sessions -F '#{session_name}' 2>/dev/null | \
    grep -E "^[a-f0-9]+-${prefix}-orchestrator$" | head -1 || true)

  if [[ -n "$existing" ]]; then
    log "orchestrator session $project ($existing) — already running, skipping"
    return 0
  fi

  # Start orchestrator via ao start (ao handles config-hash + naming).
  # Use bash -lc so ~/.bashrc is sourced before ao starts — this ensures
  # GITHUB_TOKEN, OPENCLAW_AO_HOOK_TOKEN, and other shell-defined env vars
  # are inherited by the orchestrator subprocesses (reactions require these).
  if [[ "$project" == "$FIRST_PROJECT" ]]; then
    logn "starting orchestrator + dashboard for $project..."
    tmux new-session -d -s "startup-$prefix" -c "$HOME/.smartclaw" \
      "bash -lc '$AO_BIN start '\''$project'\'''" 2>/dev/null
  else
    logn "starting orchestrator for $project..."
    tmux new-session -d -s "startup-$prefix" -c "$HOME/.smartclaw" \
      "bash -lc '$AO_BIN start '\''$project'\'' --no-dashboard'" 2>/dev/null
  fi
  # Give ao a moment to create the real session then check
  sleep 3
  local real_session
  real_session=$(tmux list-sessions -F '#{session_name}' 2>/dev/null | \
    grep -E "^[a-f0-9]+-${prefix}-orchestrator$" | head -1 || true)
  if [[ -n "$real_session" ]]; then
    ok "session $real_session"
  else
    err "failed to start orchestrator for $project — check ao start output"
  fi
}

start_notifier() {
  local notifier_py
  local pidfile="$LOG_DIR/ao-notifier.pid"
  notifier_py="$HOME/.smartclaw/scripts/agento-notifier.py"
  if [[ ! -f "$notifier_py" ]]; then
    log "Notifier Python script not found at $notifier_py — skipping notifier"
    return 0
  fi

  # Check if either notifier variant is running
  if is_running "ao-manager-notifier" || is_running "agento-notifier"; then
    log "notifier — already running, skipping"
    return 0
  fi

  # Use bash -lc so ~/.bashrc is sourced before python3 runs.
  # This ensures SLACK_BOT_TOKEN etc. are available.
  logn "starting notifier..."
  nohup bash -lc "python3 '$notifier_py'" >> "$LOG_DIR/ao-notifier.log" 2>&1 &
  local pid=$!
  echo $pid > "$pidfile"
  disown $pid 2>/dev/null || true
  ok "notifier started (pid $pid)"
}

# ── health check ─────────────────────────────────────────────────────────────

check_component() {
  local label="$1"
  local pidfile="$2"
  local tmux_session="${3:-}"

  local running=false
  if [[ -n "$tmux_session" ]] && tmux has-session -t "$tmux_session" 2>/dev/null; then
    running=true
  elif [[ -n "$pidfile" && -f "$pidfile" ]]; then
    local pid
    pid=$(cat "$pidfile" 2>/dev/null)
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      running=true
    fi
  fi

  if $running; then
    printf "  %-30s %s\n" "$label" "running"
  else
    printf "  %-30s %s\n" "$label" "STOPPED"
  fi
}

# ── session discovery ─────────────────────────────────────────────────────────

# Find running orchestrator session for a given project prefix
find_orchestrator_session() {
  local prefix="$1"
  tmux list-sessions -F '#{session_name}' 2>/dev/null | \
    grep -E "^[a-f0-9]+-${prefix}-orchestrator$" | head -1 || true
}

status() {
  echo "=== AO Manager Status ==="
  echo "Config: $CONFIG_PATH"
  echo "Log:    $LOG"
  echo ""

  echo "Lifecycle Workers:"
  for project in "${PROJECT_IDS[@]}"; do
    check_component "lifecycle-worker $project" \
      "$LOG_DIR/ao-lifecycle-$project.pid"
  done

  echo ""
  echo "Orchestrator Sessions:"
  for project in "${PROJECT_IDS[@]}"; do
    local prefix
    prefix=$(get_prefix "$project")
    local session
    session=$(find_orchestrator_session "$prefix")
    if [[ -n "$session" ]]; then
      printf "  %-30s %s\n" "orchestrator $project" "running ($session)"
    else
      printf "  %-30s %s\n" "orchestrator $project" "STOPPED"
    fi
  done

  echo ""
  echo "Notifier:"
  check_component "notifier" "$LOG_DIR/ao-notifier.pid"

  echo ""
  echo "launchd:"
  local mgr_state
  mgr_state=$(launchctl print "gui/$(id -u)/ai.agento.manager" 2>&1 | grep "^    state = " || echo "    state = unknown")
  echo "  ai.agento.manager $mgr_state"
}

# ── main ─────────────────────────────────────────────────────────────────────

start_all() {
  mkdir -p "$LOG_DIR"

  log "=== AO Manager starting ==="
  log "Config: $CONFIG_PATH"
  log "Projects: ${PROJECT_IDS[*]}"

  # 1. Notifier
  start_notifier

  # 2. Lifecycle workers for ALL projects
  for project in "${PROJECT_IDS[@]}"; do
    start_lifecycle_worker "$project"
    sleep 1
  done

  # 3. Orchestrator sessions (first project gets dashboard)
  FIRST_PROJECT="${PROJECT_IDS[0]}"
  for project in "${PROJECT_IDS[@]}"; do
    start_orchestrator "$project"
    sleep 2
  done

  log "=== AO Manager ready ==="
}

monitor_loop() {
  log "Entering monitor loop (interval: ${MONITOR_INTERVAL}s)"
  while true; do
    sleep "$MONITOR_INTERVAL"

    # Restart any stopped lifecycle workers
    for project in "${PROJECT_IDS[@]}"; do
      if ! is_running "ao lifecycle-worker ${project}$"; then
        log "[monitor] lifecycle-worker $project died — restarting"
        start_lifecycle_worker "$project"
      fi
    done

    # Restart any stopped orchestrator sessions
    for project in "${PROJECT_IDS[@]}"; do
      local prefix
      prefix=$(get_prefix "$project")
      local session
      session=$(find_orchestrator_session "$prefix")
      if [[ -z "$session" ]]; then
        log "[monitor] orchestrator session for $project died — restarting"
        start_orchestrator "$project"
      fi
    done

    # Restart notifier if stopped
    if ! is_running "ao-manager-notifier" && ! is_running "agento-notifier"; then
      log "[monitor] notifier died — restarting"
      start_notifier
    fi
  done
}

# ── CLI dispatch ─────────────────────────────────────────────────────────────

MODE="${1:-}"

case "$MODE" in
  --once)
    load_projects
    start_all
    log "--once mode: exiting after startup"
    ;;
  --status)
    load_projects
    status
    ;;
  --help|-h)
    echo "Usage: ao-manager.sh [--once|--status]"
    echo "  (no args)  — start all components and enter monitor loop"
    echo "  --once     — start all components once and exit"
    echo "  --status   — print health of all components"
    echo "  --help     — this message"
    ;;
  *)
    load_projects
    start_all
    monitor_loop
    ;;
esac
