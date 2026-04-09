#!/usr/bin/env bash
# staging-canary.sh — 6-point canary test for SmartClaw gateway
# Run against staging (port 18810) before applying changes to production (18789).
# Exit 0 only if ALL 6 checks pass. Exit 1 on any failure.
set -uo pipefail

PORT="${1:-18810}"
# Accept --port flag
if [[ "${1:-}" == "--port" ]]; then
    PORT="${2:-18810}"
    shift 2 2>/dev/null || true
fi

PASS_COUNT=0
FAIL_COUNT=0
RESULTS=()

check() {
    local name="$1"
    local result="$2"  # 0=pass, non-zero=fail
    local detail="${3:-}"

    if [[ "$result" -eq 0 ]]; then
        PASS_COUNT=$((PASS_COUNT + 1))
        RESULTS+=("PASS  $name${detail:+ — $detail}")
        echo "  PASS  $name${detail:+ — $detail}"
    else
        FAIL_COUNT=$((FAIL_COUNT + 1))
        RESULTS+=("FAIL  $name${detail:+ — $detail}")
        echo "  FAIL  $name${detail:+ — $detail}"
    fi
}

echo "=== OpenClaw Staging Canary (port $PORT) ==="
echo ""

# ── Check 1: Gateway listening on port ──
echo "[1/6] Gateway health endpoint..."
HEALTH_OUTPUT=$(curl -sf --max-time 8 "http://127.0.0.1:${PORT}/health" 2>&1)
HEALTH_RC=$?
if [[ $HEALTH_RC -eq 0 ]]; then
    check "Gateway health endpoint" 0 "HTTP 200 on port $PORT"
else
    # Distinguish between "not listening" and "listening but unhealthy"
    if lsof -i ":${PORT}" 2>/dev/null | grep -q LISTEN; then
        check "Gateway health endpoint" 1 "Port $PORT listening but /health returned error (curl exit=$HEALTH_RC)"
    else
        check "Gateway health endpoint" 1 "No process listening on port $PORT (curl exit=$HEALTH_RC)"
    fi
fi

# ── Check 2: Config schema validation (no unrecognized keys) ──
echo "[2/6] Config schema validation..."
CONFIG_DIR="${OPENCLAW_STAGING_DIR:-$HOME/.smartclaw/staging}"
if [[ "$PORT" == "18789" ]]; then
    CONFIG_DIR="$HOME/.smartclaw"
fi
CONFIG_FILE="$CONFIG_DIR/openclaw.json"
if [[ ! -f "$CONFIG_FILE" ]]; then
    check "Config schema validation" 1 "Config file not found: $CONFIG_FILE"
else
    # Check for known crash-causing keys
    SCHEMA_ERRORS=""
    # cmuxBotToken crashes gateway (incident 2026-03-28)
    if python3 -c "import json; d=json.load(open('$CONFIG_FILE')); exit(0 if 'cmuxBotToken' in str(d.get('channels',{}).get('slack',{})) else 1)" 2>/dev/null; then
        SCHEMA_ERRORS="${SCHEMA_ERRORS}cmuxBotToken present in channels.slack (known crash key); "
    fi
    # checkCompatibility at wrong level crashes gateway
    if python3 -c "import json; d=json.load(open('$CONFIG_FILE')); exit(0 if 'checkCompatibility' in d.get('gateway',{}) or 'checkCompatibility' in d.get('agents',{}).get('defaults',{}) else 1)" 2>/dev/null; then
        SCHEMA_ERRORS="${SCHEMA_ERRORS}checkCompatibility at gateway/agents.defaults level (crashes gateway); "
    fi
    # Validate JSON is parseable
    if ! python3 -c "import json; json.load(open('$CONFIG_FILE'))" 2>/dev/null; then
        SCHEMA_ERRORS="${SCHEMA_ERRORS}Invalid JSON; "
    fi
    # Check critical keys survived
    MISSING_KEYS=""
    for key_check in \
        "d.get('agents',{}).get('defaults',{}).get('heartbeat')" \
        "d.get('gateway',{}).get('auth')" \
        "d.get('channels',{}).get('slack',{}).get('botToken')"; do
        if ! python3 -c "import json; d=json.load(open('$CONFIG_FILE')); v=$key_check; exit(0 if v else 1)" 2>/dev/null; then
            MISSING_KEYS="${MISSING_KEYS}${key_check}; "
        fi
    done
    if [[ -n "$MISSING_KEYS" ]]; then
        SCHEMA_ERRORS="${SCHEMA_ERRORS}Missing critical keys: $MISSING_KEYS"
    fi

    if [[ -z "$SCHEMA_ERRORS" ]]; then
        check "Config schema validation" 0 "No crash-causing keys, JSON valid, critical keys present"
    else
        check "Config schema validation" 1 "$SCHEMA_ERRORS"
    fi
fi

# ── Check 3: Native modules load (mem0 better-sqlite3) ──
echo "[3/6] Native module ABI check..."
NODE_BIN="${OPENCLAW_NODE_BIN:-$(launchctl print gui/$(id -u)/com.smartclaw.gateway 2>/dev/null | grep -oE '/[^ ]*bin/node' | head -1 || echo '${HOME}/.nvm/versions/node/v22.22.0/bin/node')}"
BETTER_SQLITE_PATH="$HOME/.smartclaw/extensions/openclaw-mem0/node_modules/better-sqlite3"
if [[ ! -d "$BETTER_SQLITE_PATH" ]]; then
    check "Native module ABI" 1 "better-sqlite3 not found at $BETTER_SQLITE_PATH"
else
    NATIVE_OUTPUT=$("$NODE_BIN" -e "try { require('$BETTER_SQLITE_PATH')(':memory:').exec('SELECT 1'); console.log('OK'); } catch(e) { console.log('FAIL: ' + e.message); process.exit(1); }" 2>&1)
    NATIVE_RC=$?
    if [[ $NATIVE_RC -eq 0 && "$NATIVE_OUTPUT" == "OK" ]]; then
        NODE_VERSION=$("$NODE_BIN" --version 2>/dev/null || echo "unknown")
        check "Native module ABI" 0 "better-sqlite3 loads with $NODE_BIN ($NODE_VERSION)"
    else
        check "Native module ABI" 1 "$NATIVE_OUTPUT"
    fi
fi

# ── Check 4: Slack Socket Mode connectivity ──
echo "[4/6] Slack app token validity..."
SLACK_APP_TOKEN=""
if [[ -f "$CONFIG_FILE" ]]; then
    SLACK_APP_TOKEN=$(python3 -c "
import json, os
d = json.load(open('$CONFIG_FILE'))
# Try channels.slack.appToken first, then env
t = d.get('channels',{}).get('slack',{}).get('appToken','')
# Resolve \${VAR} placeholders (openclaw.json stores tokens as env references)
if isinstance(t, str) and t.startswith('\${') and t.endswith('}'):
    t = os.environ.get(t[2:-1], '')
if not t:
    t = d.get('env',{}).get('OPENCLAW_SLACK_APP_TOKEN','')
if isinstance(t, str) and t.startswith('\${') and t.endswith('}'):
    t = os.environ.get(t[2:-1], '')
print(t)
" 2>/dev/null)
fi
if [[ -z "$SLACK_APP_TOKEN" ]]; then
    # Fallback to env var
    SLACK_APP_TOKEN="${OPENCLAW_SLACK_APP_TOKEN:-}"
fi
if [[ -z "$SLACK_APP_TOKEN" ]]; then
    check "Slack app token" 1 "No app token found in config or env"
else
    SLACK_RESPONSE=$(curl -sf --max-time 10 -X POST \
        "https://slack.com/api/apps.connections.open" \
        -H "Authorization: Bearer $SLACK_APP_TOKEN" \
        -H "Content-Type: application/x-www-form-urlencoded" 2>&1)
    SLACK_RC=$?
    if [[ $SLACK_RC -eq 0 ]] && echo "$SLACK_RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); exit(0 if d.get('ok') else 1)" 2>/dev/null; then
        check "Slack app token" 0 "apps.connections.open succeeded"
    else
        SLACK_ERROR=$(echo "$SLACK_RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('error','unknown'))" 2>/dev/null || echo "curl failed ($SLACK_RC)")
        check "Slack app token" 1 "$SLACK_ERROR"
    fi
fi

# ── Check 5: SDK protocol version compatibility ──
echo "[5/6] SDK protocol version check..."
OPENCLAW_VERSION=$(openclaw --version 2>/dev/null || echo "unknown")
SDK_VERSION=$(npm ls @agentclientprotocol/sdk 2>/dev/null | grep agentclientprotocol | head -1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' || echo "")
if [[ -z "$SDK_VERSION" ]]; then
    # Try reading from installed openclaw package
    SDK_VERSION=$(node -e "try { const p = require(require.resolve('@agentclientprotocol/sdk/package.json', {paths:['/opt/homebrew/lib/node_modules/openclaw']})); console.log(p.version); } catch(e) { console.log(''); }" 2>/dev/null || echo "")
fi
if [[ -z "$SDK_VERSION" ]]; then
    check "SDK protocol version" 1 "Could not detect SDK version — fail-closed (install @agentclientprotocol/sdk or check PATH)"
else
    SDK_MAJOR=$(echo "$SDK_VERSION" | cut -d. -f1)
    SDK_MINOR=$(echo "$SDK_VERSION" | cut -d. -f2)
    if [[ "$SDK_MAJOR" -ne 0 ]]; then
        check "SDK protocol version" 1 "SDK $SDK_VERSION — major version $SDK_MAJOR != 0 (incompatible)"
    elif [[ "$SDK_MINOR" -le 16 ]]; then
        check "SDK protocol version" 0 "SDK $SDK_VERSION (protocol compat — major=0, minor <= 16)"
    else
        check "SDK protocol version" 1 "SDK $SDK_VERSION (protocol 0.${SDK_MINOR} — minor > 16, breaks ws-stream)"
    fi
fi

# ── Check 6: Heartbeat response time ──
echo "[6/6] Heartbeat response time..."
if [[ $HEALTH_RC -ne 0 ]]; then
    check "Heartbeat response" 1 "Skipped — gateway not reachable"
else
    START_MS=$(python3 -c "import time; print(int(time.time()*1000))")
    curl -sf --max-time 5 "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1
    HB_RC=$?
    END_MS=$(python3 -c "import time; print(int(time.time()*1000))")
    ELAPSED_MS=$((END_MS - START_MS))
    if [[ $HB_RC -eq 0 && $ELAPSED_MS -lt 5000 ]]; then
        check "Heartbeat response" 0 "${ELAPSED_MS}ms (< 5000ms threshold)"
    elif [[ $HB_RC -eq 0 ]]; then
        check "Heartbeat response" 1 "${ELAPSED_MS}ms (>= 5000ms threshold — too slow)"
    else
        check "Heartbeat response" 1 "Health endpoint failed (curl exit=$HB_RC)"
    fi
fi

# ── Summary ──
echo ""
echo "=== Results: $PASS_COUNT PASS / $FAIL_COUNT FAIL ==="
for r in "${RESULTS[@]}"; do
    echo "  $r"
done
echo ""

if [[ $FAIL_COUNT -gt 0 ]]; then
    echo "CANARY FAILED — do NOT apply changes to production"
    exit 1
else
    echo "CANARY PASSED — safe to apply to production"
    exit 0
fi
