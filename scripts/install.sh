#!/usr/bin/env bash
# install.sh — Install all launchd jobs for this machine.
#
# Usage: ./scripts/install.sh [--uninstall]
#
# Installs:
#   openclaw:  com.smartclaw.gateway, ai.smartclaw.startup-check, scheduled jobs, MC (if present)
#   mctrl:     ai.mctrl.supervisor
#   ao-orchestrators: per-project GitHub pollers that fire reactions (ci-failed, bugbot-comments, etc.)
#   ao-lifecycle: lifecycle-worker for agent-orchestrator (launchd KeepAlive job)
#   github-intake: GitHub intake daemon
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

UNINSTALL_FLAG="${1:-}"

# ~/agent-orchestrator.yaml → ~/.smartclaw/agent-orchestrator.yaml (orch-2u9d)
if [[ "$UNINSTALL_FLAG" != "--uninstall" ]]; then
  bash "$SCRIPT_DIR/bootstrap.sh" --symlink-only
fi

echo "=== Install All LaunchAgents ==="
echo ""

run_installer() {
  local name="$1"
  local script="$SCRIPT_DIR/$2"
  if [[ ! -x "$script" ]]; then
    echo "  • skipping $name (installer not found: $script)"
    return
  fi
  echo "--- $name ---"
  "$script" "$UNINSTALL_FLAG" || echo "  WARNING: $name installer exited with error"
  echo ""
}

run_installer "OpenClaw (gateway + startup + MC)"  "install-launchagents.sh"
run_installer "mctrl supervisor"                   "install-mctrl-supervisor.sh"
run_installer "ao orchestrators (reactions)"       "install-ao-orchestrators.sh"
run_installer "ao lifecycle-worker (agent-orchestrator)" "install-ao-lifecycle-agent-orchestrator.sh"
echo "--- ao doctor lifecycle-worker cap (local AO clone) ---"
bash "$SCRIPT_DIR/patch-ao-doctor-lifecycle-max.sh" || echo "  NOTE: run scripts/patch-ao-doctor-lifecycle-max.sh if ao doctor warns about worker count >3"
echo ""
run_installer "GitHub intake daemon"               "install-github-intake.sh"

# Legacy agento cleanup (only on uninstall)
if [[ "$UNINSTALL_FLAG" == "--uninstall" ]]; then
    echo "--- Legacy agento cleanup ---"
    for label in ai.agento.dashboard ai.agento.backfill; do
        if launchctl list | grep -q "$label"; then
            echo "  • bootout $label"
            launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
        fi
        plist="$HOME/Library/LaunchAgents/$label.plist"
        if [[ -f "$plist" ]]; then
            echo "  • remove $plist"
            rm -f "$plist"
        fi
    done
    echo ""
fi

echo "=== Done ==="
