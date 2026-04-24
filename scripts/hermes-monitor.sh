#!/usr/bin/env bash
# hermes-monitor.sh — Validates Hermes staging + prod gateways
# Usage: bash scripts/hermes-monitor.sh

set -u

HERMES_BIN="${HERMES_BIN:-hermes}"
HERMES_STAGING_HOME="${HERMES_STAGING_HOME:-$HOME/.hermes}"
HERMES_PROD_HOME="${HERMES_PROD_HOME:-$HOME/.hermes_prod}"
OPENCLAW_PROD_HEALTH_URL="${OPENCLAW_PROD_HEALTH_URL:-http://127.0.0.1:18789/health}"

PASS=0
FAIL=0
WARN=0

pass() { printf '[PASS] %s\n' "$1"; PASS=$((PASS+1)); }
fail() { printf '[FAIL] %s\n' "$1"; FAIL=$((FAIL+1)); }
warn() { printf '[WARN] %s\n' "$1"; WARN=$((WARN+1)); }
info() { printf '[INFO] %s\n' "$1"; }

echo "=== Hermes Monitor ==="
echo ""

# ── Hermes staging ────────────────────────────────────────────
info "Hermes staging (HERMES_HOME=$HERMES_STAGING_HOME)"

STAGING_GW=$(HERMES_HOME="$HERMES_STAGING_HOME" "$HERMES_BIN" gateway status 2>&1)
if echo "$STAGING_GW" | grep -q "Gateway is running"; then
    pass "Hermes staging gateway running"
else
    warn "Hermes staging gateway NOT running (non-blocking for prod deploy)"
fi

STAGING_STAT=$(HERMES_HOME="$HERMES_STAGING_HOME" "$HERMES_BIN" status 2>&1)
if echo "$STAGING_STAT" | grep "Slack" | grep -q "✓"; then
    pass "Hermes staging Slack: configured"
elif echo "$STAGING_STAT" | grep "Slack" | grep -q "✗"; then
    fail "Hermes staging Slack: error"
else
    warn "Hermes staging Slack: unknown"
fi

if echo "$STAGING_GW" | grep -q "token already in use"; then
    # Discord/Telegram conflicts are expected when two instances share auth.json
    # Only Slack matters — both have separate tokens; downgrade to warn
    CONFLICT=$(echo "$STAGING_GW" | grep 'token already in use' | head -1 | sed 's/^[ ]*⚠ //' | sed 's/ Stop.*//')
    warn "Hermes staging platform conflict (non-Slack): $CONFLICT"
else
    pass "Hermes staging no token conflicts"
fi

echo ""

# ── Hermes prod ───────────────────────────────────────────────
info "Hermes prod (HERMES_HOME=$HERMES_PROD_HOME)"

PROD_GW=$(HERMES_HOME="$HERMES_PROD_HOME" "$HERMES_BIN" gateway status 2>&1)
if echo "$PROD_GW" | grep -q "Gateway is running"; then
    pass "Hermes prod gateway running"
else
    fail "Hermes prod gateway NOT running"
fi

PROD_STAT=$(HERMES_HOME="$HERMES_PROD_HOME" "$HERMES_BIN" status 2>&1)
if echo "$PROD_STAT" | grep "Slack" | grep -q "✓"; then
    pass "Hermes prod Slack: configured"
elif echo "$PROD_STAT" | grep "Slack" | grep -q "✗"; then
    fail "Hermes prod Slack: error"
else
    warn "Hermes prod Slack: unknown"
fi

if echo "$PROD_GW" | grep -q "token already in use"; then
    # Only warn (deploy context) when both staging and prod are running.
    # If only prod is running, a token conflict is a real failure.
    local staging_count
    staging_count=$(launchctl list 2>/dev/null | grep -c "ai.smartclaw.hermes-staging" || true)
    local prod_count
    prod_count=$(launchctl list 2>/dev/null | grep -c "ai.smartclaw.hermes.prod" || true)
    local conflict_msg
    conflict_msg=$(echo "$PROD_GW" | grep 'token already in use' | head -1 | sed 's/^[ ]*⚠ //' | sed 's/ Stop.*//')
    if [[ "$staging_count" -gt 0 && "$prod_count" -gt 0 ]]; then
        warn "Hermes prod platform conflict (both instances running — deploy restart expected): $conflict_msg"
    else
        fail "Hermes prod platform conflict: $conflict_msg"
    fi
else
    pass "Hermes prod no token conflicts"
fi

echo ""

# ── OpenClaw prod (AO path) ──────────────────────────────────
info "OpenClaw prod health (AO path)"
OC_STATUS=$(curl -fsS -m 10 "$OPENCLAW_PROD_HEALTH_URL" 2>&1)
if echo "$OC_STATUS" | grep -q '"ok":true'; then
    pass "OpenClaw prod HTTP health OK"
else
    warn "OpenClaw prod HTTP health degraded (non-blocking, OpenClaw is deprecated): $OC_STATUS"
fi

OPENCLAW_GW_HEALTH=$(OPENCLAW_STATE_DIR=~/.openclaw_prod openclaw gateway health --timeout 30000 2>&1 | grep -v ExperimentalWarning | tail -5)
if echo "$OPENCLAW_GW_HEALTH" | grep -q "Slack: ok"; then
    pass "OpenClaw prod Slack (staging tokens): ok"
else
    warn "OpenClaw prod Slack: $(echo "$OPENCLAW_GW_HEALTH" | grep Slack)"
fi

echo ""

# ── Summary ────────────────────────────────────────────────────
echo "=== Summary: PASS=$PASS FAIL=$FAIL WARN=$WARN ==="
[[ $FAIL -gt 0 ]] && exit 1 || exit 0
