#!/usr/bin/env bash
# install.sh — Install all launchd jobs for this machine.
#
# Usage: ./scripts/install.sh [--uninstall]
#
# Installs:
#   openclaw:  ai.openclaw.gateway, ai.openclaw.startup-check, scheduled jobs, MC (if present)
#   mctrl:     ai.mctrl.supervisor
#   ao-orchestrators: per-project GitHub pollers that fire reactions (ci-failed, bugbot-comments, etc.)
#   ao-pr-poller: idle session handler
#   github-intake: GitHub intake daemon
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

UNINSTALL_FLAG="${1:-}"

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
run_installer "ao-pr-poller (idle session handler)" "install-ao-pr-poller.sh"
run_installer "GitHub intake daemon"               "install-github-intake.sh"

# Legacy agento cleanup (only on uninstall)
if [[ "$UNINSTALL_FLAG" == "--uninstall" ]]; then
    echo "--- Legacy agento cleanup ---"
    for label in ai.agento.dashboard ai.agento.backfill; do
        if launchctl list | grep -q "$label"; then
            echo "  • bootout $label"
            launchctl bootout "system/$label" 2>/dev/null || true
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
