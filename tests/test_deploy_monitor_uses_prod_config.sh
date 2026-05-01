#!/bin/bash
# Regression: deploy monitor invocations must use prod config/state so Slack
# delivery does not depend on repo-stub ~/.smartclaw/openclaw.json.
set -euo pipefail

SCRIPT="${HOME}/.smartclaw/scripts/deploy.sh"

PASSED=0
FAILED=0

pass() { echo "PASS: $1"; PASSED=$((PASSED + 1)); }
fail() { echo "FAIL: $1"; FAILED=$((FAILED + 1)); }

if [[ ! -f "$SCRIPT" ]]; then
  echo "FAIL: missing $SCRIPT"
  exit 1
fi

state_ref_count="$(grep -c 'OPENCLAW_STATE_DIR="$PROD_DIR"' "$SCRIPT" || true)"
config_ref_count="$(grep -c 'OPENCLAW_CONFIG_PATH="$PROD_DIR/openclaw.json"' "$SCRIPT" || true)"

if [[ "$state_ref_count" -ge 2 ]]; then
  pass "deploy monitor paths set OPENCLAW_STATE_DIR to prod (count=$state_ref_count)"
else
  fail "deploy monitor paths missing OPENCLAW_STATE_DIR prod wiring (count=$state_ref_count)"
fi

if [[ "$config_ref_count" -ge 2 ]]; then
  pass "deploy monitor paths set OPENCLAW_CONFIG_PATH to prod config (count=$config_ref_count)"
else
  fail "deploy monitor paths missing OPENCLAW_CONFIG_PATH prod wiring (count=$config_ref_count)"
fi

if [[ "$FAILED" -gt 0 ]]; then
  echo ""
  echo "FAILED: $FAILED test(s), PASSED: $PASSED"
  exit 1
fi

echo ""
echo "PASSED: $PASSED test(s)"
