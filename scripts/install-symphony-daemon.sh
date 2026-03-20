#!/usr/bin/env bash
# install-symphony-daemon.sh - Install or repair the Symphony launchd daemon
#
# Installs the Symphony daemon plist and starts it via launchctl (macOS only).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DAEMON_DIR="$HOME/.openclaw/symphony"
PLIST_LABEL="ai.openclaw.symphony"
PLIST_PATH="$HOME/Library/LaunchAgents/$PLIST_LABEL.plist"

if [ "$(uname)" != "Darwin" ]; then
  echo "ERROR: Symphony daemon install requires macOS (launchctl)." >&2
  exit 1
fi

mkdir -p "$DAEMON_DIR"

# Write plist
cat > "$PLIST_PATH" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$PLIST_LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$REPO_ROOT/src/orchestration/symphony_daemon.py</string>
    <string>--socket</string>
    <string>$DAEMON_DIR/daemon.sock</string>
  </array>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$DAEMON_DIR/daemon.log</string>
  <key>StandardErrorPath</key>
  <string>$DAEMON_DIR/daemon.err</string>
</dict>
</plist>
PLIST

launchctl unload "$PLIST_PATH" 2>/dev/null || true
launchctl load -w "$PLIST_PATH"
echo "Symphony daemon installed and started."
echo "Socket: $DAEMON_DIR/daemon.sock"
echo "Logs:   $DAEMON_DIR/daemon.log"
