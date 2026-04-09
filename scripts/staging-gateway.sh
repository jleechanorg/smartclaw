#!/usr/bin/env bash
# staging-gateway.sh — Start/stop/status for SmartClaw staging gateway (port 18810)
# Production runs on 18789; staging is isolated for pre-deploy validation.
set -euo pipefail

STAGING_PORT="${OPENCLAW_STAGING_PORT:-18810}"
STAGING_DIR="${HOME}/.smartclaw/staging"
STAGING_CONFIG="${STAGING_DIR}/openclaw.json"
STAGING_PID_FILE="${STAGING_DIR}/.gateway.pid"
STAGING_LOG="${HOME}/.smartclaw/logs/staging-gateway.log"
NODE_BIN="${OPENCLAW_NODE_BIN:-$(launchctl print gui/$(id -u)/com.smartclaw.gateway 2>/dev/null | grep -oE '/[^ ]*bin/node' | head -1 || command -v node 2>/dev/null || true)}"
if [[ -z "$NODE_BIN" || ! -x "$NODE_BIN" ]]; then
    echo "ERROR: Node.js not found. Set OPENCLAW_NODE_BIN or ensure node is in PATH."
    exit 1
fi
OPENCLAW_BIN="${OPENCLAW_BIN:-$(command -v openclaw 2>/dev/null || true)}"
if [[ -z "$OPENCLAW_BIN" || ! -x "$OPENCLAW_BIN" ]]; then
    echo "ERROR: openclaw binary not found. Install openclaw globally or set OPENCLAW_BIN."
    exit 1
fi

usage() {
    echo "Usage: $0 {start|stop|status}"
    echo ""
    echo "Manages the OpenClaw staging gateway on port ${STAGING_PORT}."
    echo "Production runs on 18789; staging is isolated for pre-deploy testing."
    exit 1
}

ensure_staging_config() {
    if [[ ! -f "$STAGING_CONFIG" ]]; then
        echo "ERROR: Staging config not found at $STAGING_CONFIG"
        echo "Create it by copying production config:"
        echo "  cp ~/.smartclaw/openclaw.json ${STAGING_CONFIG}"
        echo "  Then modify port and Slack channel settings."
        exit 1
    fi
}

start_gateway() {
    ensure_staging_config

    # Check if already running (validate PID owns the staging port)
    if [[ -f "$STAGING_PID_FILE" ]]; then
        local pid
        pid=$(cat "$STAGING_PID_FILE" 2>/dev/null || echo "")
        if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
            # Verify this PID actually owns the staging port (not a recycled PID)
            if lsof -i ":${STAGING_PORT}" -p "$pid" 2>/dev/null | grep -q LISTEN; then
                echo "Staging gateway already running (PID: $pid, port: $STAGING_PORT)"
                return 0
            else
                echo "WARNING: PID $pid alive but not listening on port $STAGING_PORT — stale PID file"
                rm -f "$STAGING_PID_FILE"
            fi
        else
            rm -f "$STAGING_PID_FILE"
        fi
    fi

    # Check if production or another process is on the staging port
    if lsof -i ":${STAGING_PORT}" 2>/dev/null | grep -q LISTEN; then
        echo "ERROR: Port ${STAGING_PORT} is already in use"
        lsof -i ":${STAGING_PORT}" | grep LISTEN
        exit 1
    fi

    # Ensure log directory exists
    mkdir -p "$(dirname "$STAGING_LOG")"

    echo "Starting staging gateway..."
    echo "  Node: $NODE_BIN"
    echo "  Config: $STAGING_CONFIG"
    echo "  Port: $STAGING_PORT"
    echo "  Log: $STAGING_LOG"

    # Start gateway with staging config (inherit production env vars for parity)
    OPENCLAW_CONFIG_DIR="$STAGING_DIR" \
    OPENCLAW_PORT="$STAGING_PORT" \
    NODE_ENV="staging" \
    GOOGLE_CLOUD_PROJECT="${GOOGLE_CLOUD_PROJECT:-}" \
    NODE_EXTRA_CA_CERTS="${NODE_EXTRA_CA_CERTS:-}" \
    NODE_USE_SYSTEM_CA="${NODE_USE_SYSTEM_CA:-}" \
    nohup "$NODE_BIN" "$OPENCLAW_BIN" gateway run \
        --bind loopback \
        --port "$STAGING_PORT" \
        --force \
        >> "$STAGING_LOG" 2>&1 &

    local gateway_pid=$!
    echo "$gateway_pid" > "$STAGING_PID_FILE"

    # Wait for startup (max 15 seconds)
    local attempts=0
    while [[ $attempts -lt 15 ]]; do
        sleep 1
        attempts=$((attempts + 1))
        if curl -sf "http://127.0.0.1:${STAGING_PORT}/health" >/dev/null 2>&1; then
            echo "Staging gateway started (PID: $gateway_pid, port: $STAGING_PORT)"
            return 0
        fi
        # Check if process died
        if ! kill -0 "$gateway_pid" 2>/dev/null; then
            echo "ERROR: Staging gateway process died during startup"
            echo "Check logs: tail -20 $STAGING_LOG"
            rm -f "$STAGING_PID_FILE"
            exit 1
        fi
    done

    echo "WARNING: Gateway started (PID: $gateway_pid) but health endpoint not responding after 15s"
    echo "Check logs: tail -20 $STAGING_LOG"
}

stop_gateway() {
    if [[ ! -f "$STAGING_PID_FILE" ]]; then
        echo "Staging gateway not running (no PID file)"
        return 0
    fi

    local pid
    pid=$(cat "$STAGING_PID_FILE" 2>/dev/null || echo "")

    if [[ -z "$pid" ]]; then
        echo "Staging gateway not running (empty PID file)"
        rm -f "$STAGING_PID_FILE"
        return 0
    fi

    if kill -0 "$pid" 2>/dev/null; then
        echo "Stopping staging gateway (PID: $pid)..."
        kill "$pid" 2>/dev/null || true
        # Wait for graceful shutdown (max 5 seconds)
        local attempts=0
        while [[ $attempts -lt 5 ]]; do
            sleep 1
            attempts=$((attempts + 1))
            if ! kill -0 "$pid" 2>/dev/null; then
                echo "Staging gateway stopped"
                rm -f "$STAGING_PID_FILE"
                return 0
            fi
        done
        # Force kill
        kill -9 "$pid" 2>/dev/null || true
        echo "Staging gateway force-killed"
    else
        echo "Staging gateway not running (stale PID: $pid)"
    fi

    rm -f "$STAGING_PID_FILE"
}

status_gateway() {
    echo "=== Staging Gateway Status ==="
    echo "Port: $STAGING_PORT"

    # PID file check
    if [[ -f "$STAGING_PID_FILE" ]]; then
        local pid
        pid=$(cat "$STAGING_PID_FILE" 2>/dev/null || echo "")
        if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
            echo "Process: RUNNING (PID: $pid)"
        else
            echo "Process: DEAD (stale PID: $pid)"
        fi
    else
        echo "Process: NOT RUNNING"
    fi

    # Port check
    if lsof -i ":${STAGING_PORT}" 2>/dev/null | grep -q LISTEN; then
        echo "Port: LISTENING"
    else
        echo "Port: NOT LISTENING"
    fi

    # Health check
    if curl -sf "http://127.0.0.1:${STAGING_PORT}/health" >/dev/null 2>&1; then
        echo "Health: OK"
    else
        echo "Health: UNREACHABLE"
    fi

    # Config check
    if [[ -f "$STAGING_CONFIG" ]]; then
        echo "Config: $STAGING_CONFIG (exists)"
    else
        echo "Config: $STAGING_CONFIG (MISSING)"
    fi
}

case "${1:-}" in
    start)  start_gateway ;;
    stop)   stop_gateway ;;
    status) status_gateway ;;
    *)      usage ;;
esac
