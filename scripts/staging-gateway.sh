#!/usr/bin/env bash
# staging-gateway.sh — Start/stop/status for OpenClaw staging gateway (port 18790)
# Production runs on 18789; staging uses the main ~/.smartclaw/ dir + openclaw.staging.json.
# This script delegates to launchd (ai.smartclaw.staging) for reliable long-running operation.
set -euo pipefail

STAGING_PORT="${OPENCLAW_STAGING_PORT:-18790}"
OPENCLAW_DIR="${HOME}/.smartclaw"
STAGING_CONFIG="${OPENCLAW_DIR}/openclaw.staging.json"
STAGING_LABEL="ai.smartclaw.staging"

usage() {
    echo "Usage: $0 {start|stop|status|restart}"
    echo ""
    echo "Manages the OpenClaw staging gateway on port ${STAGING_PORT}."
    echo "Delegates to launchd label '${STAGING_LABEL}' for reliability."
    echo "Production gateway runs on port 18789."
    exit 1
}

# Guard: staging config must exist
ensure_staging_config() {
    if [[ ! -f "$STAGING_CONFIG" ]]; then
        echo "ERROR: Staging config not found at $STAGING_CONFIG"
        echo "Create it by copying production config:"
        echo "  cp ~/.smartclaw/openclaw.json ~/.smartclaw/openclaw.staging.json"
        echo "  Then modify port to 18790 and Slack routing."
        exit 1
    fi
}

start_gateway() {
    ensure_staging_config

    local state
    state=$(launchctl print gui/$(id -u)/${STAGING_LABEL} 2>/dev/null | grep "state = " | awk '{print $NF}' || echo "not loaded")
    if [[ "$state" == "running" ]]; then
        echo "Staging gateway already running (launchd: ${STAGING_LABEL}, port: ${STAGING_PORT})"
        return 0
    fi

    # Install + load the plist if not already
    if [[ ! -f "${OPENCLAW_DIR}/ai.smartclaw.staging.plist" ]]; then
        echo "Installing launchd plist..."
        install -m 644 /dev/null "${OPENCLAW_DIR}/ai.smartclaw.staging.plist"
        cat > "${OPENCLAW_DIR}/ai.smartclaw.staging.plist" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key>
	<string>ai.smartclaw.staging</string>
	<key>EnvironmentVariables</key>
	<dict>
		<key>HOME</key>
		<string>/Users/jleechan</string>
		<key>OPENCLAW_RAW_STREAM</key>
		<string>1</string>
		<key>OPENCLAW_RAW_STREAM_PATH</key>
		<string>/tmp/openclaw/staging-raw-stream.jsonl</string>
	</dict>
	<key>KeepAlive</key>
	<true/>
	<key>ProgramArguments</key>
	<array>
		<string>${HOME}/.nvm/versions/node/v22.22.0/bin/node</string>
		<string>/opt/homebrew/lib/node_modules/openclaw/dist/index.js</string>
		<string>gateway</string>
		<string>--port</string>
		<string>18790</string>
		<string>--config</string>
		<string>${HOME}/.smartclaw/openclaw.staging.json</string>
		<string>--allow-unconfigured</string>
	</array>
	<key>RunAtLoad</key>
	<true/>
	<key>StandardErrorPath</key>
	<string>${HOME}/.smartclaw/logs/staging-gateway.err.log</string>
	<key>StandardOutPath</key>
	<string>${HOME}/.smartclaw/logs/staging-gateway.log</string>
</dict>
</plist>
PLIST
    fi

    launchctl bootout gui/$(id -u)/${STAGING_LABEL} 2>/dev/null || true
    launchctl bootstrap gui/$(id -u) "${OPENCLAW_DIR}/ai.smartclaw.staging.plist" 2>&1

    # Wait for health (max 20s)
    echo "Starting staging gateway..."
    local attempts=0
    while [[ $attempts -lt 20 ]]; do
        sleep 1
        attempts=$((attempts + 1))
        if curl -sf "http://127.0.0.1:${STAGING_PORT}/health" >/dev/null 2>&1; then
            echo "Staging gateway started (port: ${STAGING_PORT})"
            return 0
        fi
    done
    echo "WARNING: Gateway started but health endpoint not responding after 20s."
    echo "Check logs: tail -20 ${OPENCLAW_DIR}/logs/staging-gateway.log"
}

stop_gateway() {
    launchctl bootout gui/$(id -u)/${STAGING_LABEL} 2>&1 || true
    echo "Staging gateway stopped"
}

status_gateway() {
    echo "=== Staging Gateway Status ==="
    echo "Port: $STAGING_PORT"
    echo "Config: $STAGING_CONFIG"

    local state pid
    state=$(launchctl print gui/$(id -u)/${STAGING_LABEL} 2>/dev/null | grep "state = " | awk '{print $NF}' || echo "not loaded")
    pid=$(launchctl print gui/$(id -u)/${STAGING_LABEL} 2>/dev/null | grep "pid = " | awk '{print $NF}' || echo "none")

    echo "Launchd: ${state} (pid: ${pid})"

    if lsof -i ":${STAGING_PORT}" 2>/dev/null | grep -q LISTEN; then
        echo "Port: LISTENING"
    else
        echo "Port: NOT LISTENING"
    fi

    if curl -sf --max-time 3 "http://127.0.0.1:${STAGING_PORT}/health" >/dev/null 2>&1; then
        echo "Health: OK"
    else
        echo "Health: UNREACHABLE"
    fi
}

case "${1:-}" in
    start)   start_gateway ;;
    stop)    stop_gateway ;;
    status)  status_gateway ;;
    restart) stop_gateway; sleep 2; start_gateway ;;
    *)       usage ;;
esac
