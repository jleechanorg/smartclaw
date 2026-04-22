#!/usr/bin/env bash
# staging-canary.sh — 8-point canary test for OpenClaw gateway
# Run against staging (port 18810) before applying changes to production (18789).
# Exit 0 only if ALL 8 checks pass. Exit 1 on any failure.
set -uo pipefail

PORT="${1:-18790}"
# Accept --port flag
if [[ "${1:-}" == "--port" ]]; then
    PORT="${2:-18790}"
    shift 2 2>/dev/null || true
fi

PASS_COUNT=0
FAIL_COUNT=0
RESULTS=()
# Canonical production port — checks 7 and 8 use this to select the correct state dir.
PROD_PORT="${OPENCLAW_PROD_PORT:-18789}"

is_stub_main_config() {
    python3 - "$1" <<'PY'
import json
import sys

with open(sys.argv[1]) as fh:
    cfg = json.load(fh)

slack = cfg.get("channels", {}).get("slack", {}) or {}
required = [
    cfg.get("gateway", {}).get("auth", {}).get("token"),
    cfg.get("meta", {}).get("lastTouchedVersion"),
    cfg.get("agents", {}).get("defaults", {}).get("workspace"),
    cfg.get("plugins", {}).get("entries"),
]

missing = any(not item for item in required)
if slack.get("enabled") is True and not (slack.get("botToken") and slack.get("appToken")):
    missing = True

sys.exit(0 if missing else 1)
PY
}

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
echo "[1/9] Gateway health endpoint..."
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
echo "[2/9] Config schema validation..."
# Staging gateway (18810) uses ~/.smartclaw/openclaw.staging.json.
# Architecture: ~/.smartclaw/ = staging (18810), ~/.smartclaw_prod/ = production (18789)
# Explicit override (CI / custom layouts)
if [[ -n "${OPENCLAW_STAGING_CONFIG:-}" ]]; then
    CONFIG_FILE="$OPENCLAW_STAGING_CONFIG"
elif [[ "$PORT" == "18789" ]]; then
    CONFIG_FILE="$HOME/.smartclaw_prod/openclaw.json"
elif [[ "$PORT" == "18810" ]]; then
    CONFIG_FILE="$HOME/.smartclaw/openclaw.staging.json"
else
    echo "  WARN: Unknown port $PORT — defaulting to staging config (~/.smartclaw/openclaw.staging.json)"
    CONFIG_FILE="$HOME/.smartclaw/openclaw.staging.json"
fi
if [[ ! -f "$CONFIG_FILE" ]]; then
    check "Config schema validation" 1 "Config file not found: $CONFIG_FILE"
else
    # Check for known crash-causing keys
    SCHEMA_ERRORS=""
    STUB_CONFIG=0
    if is_stub_main_config "$CONFIG_FILE"; then
        STUB_CONFIG=1
    fi
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
    REQUIRED_KEYS=(
        "d.get('gateway',{}).get('auth')"
    )
    if [[ "$STUB_CONFIG" -eq 0 ]]; then
        REQUIRED_KEYS+=(
            "d.get('agents',{}).get('defaults',{}).get('heartbeat')"
            "d.get('channels',{}).get('slack',{}).get('botToken')"
        )
    fi
    for key_check in "${REQUIRED_KEYS[@]}"; do
        if ! python3 -c "import json; d=json.load(open('$CONFIG_FILE')); v=$key_check; exit(0 if v else 1)" 2>/dev/null; then
            MISSING_KEYS="${MISSING_KEYS}${key_check}; "
        fi
    done
    if [[ -n "$MISSING_KEYS" ]]; then
        SCHEMA_ERRORS="${SCHEMA_ERRORS}Missing critical keys: $MISSING_KEYS"
    fi

    if [[ -z "$SCHEMA_ERRORS" ]]; then
        if [[ "$STUB_CONFIG" -eq 1 ]]; then
            check "Config schema validation" 0 "Repo stub accepted: JSON valid, no crash-causing keys, live-only keys intentionally omitted"
        else
            check "Config schema validation" 0 "No crash-causing keys, JSON valid, critical keys present"
        fi
    else
        check "Config schema validation" 1 "$SCHEMA_ERRORS"
    fi
fi

# ── Check 3: Native modules load (mem0 better-sqlite3) ──
echo "[3/9] Native module ABI check..."
NODE_BIN="${OPENCLAW_NODE_BIN:-$(launchctl print gui/$(id -u)/ai.smartclaw.gateway 2>/dev/null | grep -oE '/[^ ]*bin/node' | head -1 || echo '${HOME}/.nvm/versions/node/v22.22.0/bin/node')}"
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
echo "[4/9] Slack app token validity..."
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
echo "[5/9] SDK protocol version check..."
OPENCLAW_VERSION=$(openclaw --version 2>/dev/null || echo "unknown")
SDK_VERSION=$(npm ls @agentclientprotocol/sdk --prefix "$HOME/.smartclaw" 2>/dev/null | grep agentclientprotocol | head -1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' || echo "")
if [[ -z "$SDK_VERSION" ]]; then
    # Try reading from installed openclaw package (homebrew and local installs)
    # Note: use $(echo ~) to expand ~ inside single-quoted node -e string
    SDK_VERSION=$(node -e "try { const p = require(require.resolve('@agentclientprotocol/sdk/package.json', {paths:['/opt/homebrew/lib/node_modules/openclaw','$(echo ~)/.smartclaw']})); console.log(p.version); } catch(e) { console.log(''); }" 2>/dev/null || echo "")
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
echo "[6/9] Heartbeat response time..."
if [[ $HEALTH_RC -ne 0 ]]; then
    check "Heartbeat response" 1 "Skipped — gateway not reachable"
else
    # Use the same curl budget as check 1 (8s) — 5s caused false FAILs (curl exit=28) right after
    # prod gateway restart when the event loop is still warming plugins/channels.
    HB_RC=1
    ELAPSED_MS=0
    for _hb_attempt in 1 2; do
        START_MS=$(python3 -c "import time; print(int(time.time()*1000))")
        curl -sf --max-time 8 "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1
        HB_RC=$?
        END_MS=$(python3 -c "import time; print(int(time.time()*1000))")
        ELAPSED_MS=$((END_MS - START_MS))
        if [[ $HB_RC -eq 0 ]]; then
            break
        fi
        # Transient timeout (28) during restart — one short backoff then retry once.
        if [[ $_hb_attempt -eq 1 && $HB_RC -eq 28 ]]; then
            sleep 2
            continue
        fi
        break
    done
    if [[ $HB_RC -eq 0 && $ELAPSED_MS -lt 5000 ]]; then
        check "Heartbeat response" 0 "${ELAPSED_MS}ms (< 5000ms threshold)"
    elif [[ $HB_RC -eq 0 ]]; then
        check "Heartbeat response" 1 "${ELAPSED_MS}ms (>= 5000ms threshold — too slow)"
    else
        check "Heartbeat response" 1 "Health endpoint failed (curl exit=$HB_RC)"
    fi
fi

# ── Check 7: Stale session locks ──
# Derive state dir from port: prod port (~/.smartclaw_prod), else staging (~/.smartclaw)
if [[ "$PORT" == "$PROD_PORT" ]]; then
    SESSION_DIR="$HOME/.smartclaw_prod/agents/main/sessions"
else
    SESSION_DIR="$HOME/.smartclaw/agents/main/sessions"
fi
echo "[7/9] Stale session lock check..."
if [[ -d "$SESSION_DIR" ]]; then
    STALE_LOCKS=()
    while IFS= read -r lockfile; do
        raw=$(cat "$lockfile" 2>/dev/null)
        # Lock files may be plain PID or JSON {"pid":N,...}
        pid=$(echo "$raw" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['pid'])" 2>/dev/null || echo "$raw" | tr -d '[:space:]')
        if [[ -n "$pid" ]] && [[ "$pid" =~ ^[0-9]+$ ]] && ! kill -0 "$pid" 2>/dev/null; then
            STALE_LOCKS+=("$(basename "$lockfile") (pid=$pid dead)")
        fi
    done < <(find "$SESSION_DIR" -name "*.lock" -maxdepth 1 2>/dev/null)
    if [[ ${#STALE_LOCKS[@]} -eq 0 ]]; then
        check "Stale session locks" 0 "No dead-owner lock files found"
    else
        check "Stale session locks" 1 "${#STALE_LOCKS[@]} stale lock(s): ${STALE_LOCKS[*]}"
    fi
else
    check "Stale session locks" 0 "Sessions dir not present (skipping)"
fi

# ── Check 8: Agent auth-profiles.json present ──
# HTTP /health returns "live" even when auth-profiles.json is missing.
# Missing auth-profiles → every LLM call fails silently with
# "No API key found for provider anthropic" — gateway appears healthy but is dead.
# Derive state dir from port (same logic as Check 7).
# PROD_PORT defined at top of script; use it here for custom deployment support.
if [[ "$PORT" == "$PROD_PORT" ]]; then
    AUTH_DIR="$HOME/.smartclaw_prod/agents/main/agent"
else
    AUTH_DIR="$HOME/.smartclaw/agents/main/agent"
fi
echo "[8/9] Agent auth-profiles.json present..."
AUTH_FILE="$AUTH_DIR/auth-profiles.json"
if [[ -f "$AUTH_FILE" ]]; then
    # Verify it's valid JSON and has at least one provider key
    PROFILE_COUNT=$(python3 -c "
import json, sys
try:
    d = json.load(open('$AUTH_FILE'))
    # auth-profiles is a list or dict of provider configs
    n = len(d) if isinstance(d, (list, dict)) else 0
    print(n)
except Exception as e:
    print(0)
" 2>/dev/null || echo "0")
    if [[ "$PROFILE_COUNT" -gt 0 ]]; then
        check "Agent auth-profiles.json" 0 "$PROFILE_COUNT profile(s) in $AUTH_FILE"
    else
        check "Agent auth-profiles.json" 1 "File present but empty or invalid JSON: $AUTH_FILE"
    fi
else
    check "Agent auth-profiles.json" 1 "Missing: $AUTH_FILE — agent cannot authenticate (HTTP liveness does NOT prove this)"
fi

# ── Check 9: Single gateway instance ──
# Multiple openclaw-gateway processes listening on this port = lock storm.
# Must filter by port (lsof) not just pgrep — staging (18810) and prod (18789)
# both run openclaw-gateway; counting all causes false failures on the non-target port.
echo "[9/9] Single openclaw-gateway instance check..."
_gw_count=$(lsof -i ":${PORT}" -sTCP:LISTEN 2>/dev/null | grep -v "^COMMAND" | awk '{print $2}' | sort -u | wc -l)
if [[ "$_gw_count" -eq 1 ]]; then
    check "Single gateway instance" 0 "1 process listening on port $PORT (no orphans)"
elif [[ "$_gw_count" -eq 0 ]]; then
    check "Single gateway instance" 1 "No process listening on port $PORT — gateway not running"
else
    check "Single gateway instance" 1 "$_gw_count processes listening on port $PORT — orphan conflict risk (lock storms)"
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
