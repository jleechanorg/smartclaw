#!/usr/bin/env bash
# install-all.sh — Install all launchd jobs for this machine.
#
# Usage: ./scripts/install-all.sh [--uninstall]
#
# Installs:
#   openclaw:  ai.openclaw.gateway, ai.openclaw.startup-check, scheduled jobs, MC (if present)
#   mctrl:     ai.mctrl.supervisor
#   agento:    ai.agento.dashboard, ai.agento.backfill
#   agento-orchestrators: per-project GitHub pollers that fire reactions (ci-failed, bugbot-comments, etc.)
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
run_installer "agento (dashboard + backfill)"      "install-agento.sh"
run_installer "agento orchestrators (reactions)"   "install-agento-orchestrators.sh"
run_installer "ao-pr-poller (idle session handler)" "install-ao-pr-poller.sh"
run_installer "GitHub intake daemon"               "install-github-intake.sh"

echo "=== Done ==="
