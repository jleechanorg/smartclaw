#!/usr/bin/env bash
# Install agento launchd jobs:
#   - ai.agento.backfill    — auto-spawn sessions for [agento]-tagged PRs (every 15min)
#
# NOTE: The AO orchestrators (GitHub polling + reactions) are managed separately
# by install-agento-orchestrators.sh → ai.agento.orchestrators launchd job.
# NOTE: The ai.agento.dashboard job (ao start --no-dashboard) is also created here.
#
# Usage: ./scripts/install-agento.sh [--uninstall]
set -euo pipefail

LAUNCHD_DIR="$HOME/Library/LaunchAgents"
AO_BIN="$HOME/bin/ao"
AO_CONFIG="$HOME/agent-orchestrator.yaml"
AO_WORKDIR="$HOME/.openclaw"
BACKFILL_SCRIPT="$HOME/.openclaw/scripts/ao-backfill.sh"

UNINSTALL=false
[[ "${1:-}" == "--uninstall" ]] && UNINSTALL=true

install_job() {
  local label="$1"
  local plist="$LAUNCHD_DIR/$label.plist"
  launchctl bootout "gui/$UID" "$plist" 2>/dev/null || true
  launchctl bootstrap "gui/$UID" "$plist"
  echo "  ✓ $label loaded"
}

uninstall_job() {
  local label="$1"
  local plist="$LAUNCHD_DIR/$label.plist"
  launchctl bootout "gui/$UID" "$plist" 2>/dev/null || true
  rm -f "$plist"
  echo "  ✓ $label uninstalled"
}

if $UNINSTALL; then
  echo "Uninstalling agento launchd jobs..."
  uninstall_job "ai.agento.dashboard"
  uninstall_job "ai.agento.backfill"
  exit 0
fi

mkdir -p "$LAUNCHD_DIR"

# Validate prerequisites
if [[ ! -x "$AO_BIN" ]]; then
  echo "ERROR: ao binary not found at $AO_BIN" >&2
  exit 1
fi
if [[ ! -f "$AO_CONFIG" ]]; then
  echo "ERROR: agent-orchestrator.yaml not found at $AO_CONFIG" >&2
  exit 1
fi
if [[ ! -x "$BACKFILL_SCRIPT" ]]; then
  echo "ERROR: ao-backfill.sh not found/executable at $BACKFILL_SCRIPT" >&2
  echo "  Run: chmod +x $BACKFILL_SCRIPT" >&2
  exit 1
fi

echo "Installing agento launchd jobs..."

# --- ai.agento.dashboard ---
cat > "$LAUNCHD_DIR/ai.agento.dashboard.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>ai.agento.dashboard</string>
    <key>ProgramArguments</key>
    <array>
        <string>$AO_BIN</string>
        <string>start</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/bin:/opt/homebrew/bin</string>
        <key>HOME</key>
        <string>$HOME</string>
        <key>AO_CONFIG_PATH</key>
        <string>$AO_CONFIG</string>
    </dict>
    <key>WorkingDirectory</key>
    <string>$AO_WORKDIR</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/ao-dashboard.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/ao-dashboard.err.log</string>
</dict>
</plist>
PLIST
install_job "ai.agento.dashboard"

# --- ai.agento.backfill ---
cat > "$LAUNCHD_DIR/ai.agento.backfill.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>ai.agento.backfill</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$BACKFILL_SCRIPT</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/bin:/opt/homebrew/bin</string>
        <key>HOME</key>
        <string>$HOME</string>
        <key>AO_CONFIG_PATH</key>
        <string>$AO_CONFIG</string>
    </dict>
    <key>StartInterval</key>
    <integer>900</integer>
    <key>RunAtLoad</key>
    <false/>
    <key>StandardOutPath</key>
    <string>/tmp/ao-backfill.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/ao-backfill.err.log</string>
</dict>
</plist>
PLIST
install_job "ai.agento.backfill"

echo ""
echo "Verifying..."
sleep 2
for label in ai.agento.dashboard ai.agento.backfill; do
  if launchctl print "gui/$UID/$label" >/dev/null 2>&1; then
    echo "  ✓ $label registered"
  else
    echo "  ✗ $label NOT registered"
  fi
done

if lsof -i :3011 2>/dev/null | grep -q LISTEN; then
  echo "  ✓ dashboard listening on port 3011"
else
  echo "  • dashboard not yet on port 3011 (may still be starting)"
fi

echo ""
echo "Logs:"
echo "  dashboard: tail -f /tmp/ao-dashboard.log"
echo "  backfill:  tail -f /tmp/ao-backfill.log"
