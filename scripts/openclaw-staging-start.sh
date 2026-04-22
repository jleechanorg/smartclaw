#!/usr/bin/env bash
# openclaw-staging-start.sh — Bring up OpenClaw staging gateway temporarily
# Usage: bash openclaw-staging-start.sh [--wait]
#   --wait  : block until gateway is healthy (for CI/deployment pipelines)
set -uo pipefail

STAGING_LABEL="ai.smartclaw.staging"
STAGING_PORT="${OPENCLAW_STAGING_PORT:-18810}"
MAX_WAIT="${MAX_WAIT:-30}"
GATEWAY_BIN="${HOME}/.nvm/versions/node/v22.22.0/bin/openclaw"

echo "=== OpenClaw Staging Start ==="
echo "  Label : $STAGING_LABEL"
echo "  Port  : $STAGING_PORT"

# Check if already running
if lsof -i ":${STAGING_PORT}" -sTCP:LISTEN 2>/dev/null | grep -qv "^COMMAND"; then
    echo "  INFO: Staging gateway already running on port $STAGING_PORT — not reloading"
else
    # Try launchd first; if I/O error, unload old registration then re-load
    # or fall back to direct process start.
    echo "  Loading launchd plist..."
    if ! launchctl load -w "$HOME/Library/LaunchAgents/${STAGING_LABEL}.plist" 2>/dev/null; then
        # I/O error usually means service already registered — try unload then load
        launchctl unload "$HOME/Library/LaunchAgents/${STAGING_LABEL}.plist" 2>/dev/null || true
        sleep 1
        if ! launchctl load -w "$HOME/Library/LaunchAgents/${STAGING_LABEL}.plist" 2>/dev/null; then
            echo "  launchd load failed — starting gateway process directly..."
            pkill -f "openclaw.*18810" 2>/dev/null || true
            sleep 1
            OPENCLAW_STATE_DIR="$HOME/.smartclaw" \
            OPENCLAW_CONFIG_PATH="$HOME/.smartclaw/openclaw.staging.json" \
            OPENCLAW_GATEWAY_PORT="18810" \
            HOME="$HOME" \
            PATH="$GATEWAY_BIN:$PATH" \
            nohup "$GATEWAY_BIN" gateway --port 18810 --allow-unconfigured \
                >> "$HOME/.smartclaw/logs/staging-gateway.log" 2>> "$HOME/.smartclaw/logs/staging-gateway.err.log" &
        fi
    fi
    echo "  Launchd plist loaded (or direct start initiated)"
fi

# Optional wait block
if [[ "${1:-}" == "--wait" ]]; then
    echo "  Waiting up to ${MAX_WAIT}s for gateway health..."
    for i in $(seq 1 "$MAX_WAIT"); do
        if curl -sf --max-time 3 "http://127.0.0.1:${STAGING_PORT}/health" >/dev/null 2>&1; then
            echo "  Gateway healthy after ${i}s"
            exit 0
        fi
        sleep 1
    done
    echo "  WARN: Gateway not healthy after ${MAX_WAIT}s — check logs:"
    echo "        tail -f $HOME/.smartclaw/logs/staging-gateway.log"
    exit 1
fi

echo "  Staging gateway started (background, port $STAGING_PORT)"
echo "  Health check: curl http://127.0.0.1:${STAGING_PORT}/health"
echo "  Logs: tail -f $HOME/.smartclaw/logs/staging-gateway.log"
echo "  Stop:   bash openclaw-staging-stop.sh"