#!/usr/bin/env bash
# Install the mctrl supervisor as a launchd agent.
# Usage: ./scripts/install-mctrl-supervisor.sh [--uninstall]
set -euo pipefail

LABEL="ai.mctrl.supervisor"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LAUNCHD_DIR="$HOME/Library/LaunchAgents"
PLIST="$LAUNCHD_DIR/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs/mctrl"

UNINSTALL=false
[[ "${1:-}" == "--uninstall" ]] && UNINSTALL=true

if $UNINSTALL; then
  launchctl bootout "gui/$UID" "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Uninstalled $LABEL"
  exit 0
fi

mkdir -p "$LAUNCHD_DIR" "$LOG_DIR"
chmod +x "$REPO_DIR/scripts/run-mctrl-supervisor.sh"

sed \
  -e "s|@REPO_ROOT@|$REPO_DIR|g" \
  -e "s|@HOME@|$HOME|g" \
  "$REPO_DIR/scripts/mctrl-supervisor.plist.template" \
  > "$PLIST"

# Reload if already loaded (bootstrap/bootout is the modern API; load/unload deprecated since macOS 10.10)
launchctl bootout "gui/$UID" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$PLIST"

echo "Installed $LABEL"
echo "Logs: $LOG_DIR/supervisor.log"
echo "Status: launchctl list $LABEL"
