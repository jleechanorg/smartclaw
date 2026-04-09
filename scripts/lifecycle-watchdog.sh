#!/usr/bin/env bash
# Lifecycle-worker watchdog — auto-bootstraps launchd if LW is not running.
# Runs every 2 minutes via cron. Supplements launchd KeepAlive for cases where
# launchd loses track of the service (e.g., after repeated rapid exits).
#
# Usage: add to crontab:
#   */2 * * * * /Users/jleechan/.openclaw/scripts/lifecycle-watchdog.sh

set -euo pipefail

PLIST="$HOME/Library/LaunchAgents/com.agentorchestrator.lifecycle-agent-orchestrator.plist"
SERVICE="com.agentorchestrator.lifecycle-agent-orchestrator"
LOG="$HOME/.agent-orchestrator/bb5e6b7f8db3-agent-orchestrator/lifecycle-watchdog.log"
GUI_DOMAIN="gui/$(id -u)"

ts() { date '+%Y-%m-%dT%H:%M:%S%z'; }

mkdir -p "$(dirname "$LOG")"

# 1. Check if process is alive
if ps aux | grep -q "[l]ifecycle-worker agent-orchestrator"; then
  exit 0
fi

# 2. Check launchd state
state=$(launchctl print "$GUI_DOMAIN/$SERVICE" 2>/dev/null | grep "state =" | awk '{print $NF}' || echo "unknown")

if [[ "$state" == "running" ]]; then
  # launchd thinks it's running but process is gone — stale state
  echo "[$(ts)] WARN: launchd state=running but no process found — skipping bootstrap" >> "$LOG"
  exit 0
fi

# 3. Process dead and launchd not managing — bootstrap
echo "[$(ts)] ALERT: lifecycle-worker not running (launchd state=$state) — bootstrapping" >> "$LOG"

if launchctl bootstrap "$GUI_DOMAIN" "$PLIST" 2>&1 | tee -a "$LOG"; then
  sleep 3
  if ps aux | grep -q "[l]ifecycle-worker agent-orchestrator"; then
    echo "[$(ts)] OK: lifecycle-worker bootstrapped successfully" >> "$LOG"
  else
    echo "[$(ts)] WARN: bootstrap ran but process still not found" >> "$LOG"
  fi
else
  echo "[$(ts)] ERROR: bootstrap failed" >> "$LOG"
fi
