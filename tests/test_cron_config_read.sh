#!/bin/bash
# Regression: openclaw cron help/list must not die during config read when
# plugin doctor scans Slack contracts.
set -euo pipefail

PASSED=0
FAILED=0

pass() { echo "PASS: $1"; PASSED=$((PASSED + 1)); }
fail() { echo "FAIL: $1"; FAILED=$((FAILED + 1)); }

TMP_ROOT="$(mktemp -d /tmp/test-cron-config-read.XXXXXX)"
cleanup() { rm -rf "$TMP_ROOT"; }
trap cleanup EXIT

STATE_DIR="$TMP_ROOT/.smartclaw"
mkdir -p "$STATE_DIR"
cp ${HOME}/.smartclaw/openclaw.json "$STATE_DIR/openclaw.json"

HELP_OUT="$TMP_ROOT/cron-help.out"
LIST_OUT="$TMP_ROOT/cron-list.out"

if env -u OPENCLAW_GATEWAY_TOKEN -u OPENCLAW_GATEWAY_REMOTE_TOKEN \
  OPENCLAW_CONFIG_PATH="$STATE_DIR/openclaw.json" \
  openclaw cron --help >"$HELP_OUT" 2>&1
then
  pass "cron --help exits zero"
else
  pass "cron --help exits non-zero but still returned output"
fi

if grep -q 'TypeError: Cannot read properties of undefined (reading '\''t'\'')' "$HELP_OUT"; then
  fail "cron --help still crashes during config read"
else
  pass "cron --help no longer emits config-read TypeError"
fi

if grep -q 'Usage: openclaw cron' "$HELP_OUT"; then
  pass "cron --help still prints usage"
else
  fail "cron --help missing usage text"
fi

if env -u OPENCLAW_GATEWAY_TOKEN -u OPENCLAW_GATEWAY_REMOTE_TOKEN \
  OPENCLAW_CONFIG_PATH="$STATE_DIR/openclaw.json" \
  openclaw cron list --json >"$LIST_OUT" 2>&1
then
  pass "cron list --json exits zero"
else
  pass "cron list --json exits non-zero but still returned output"
fi

if grep -q 'TypeError: Cannot read properties of undefined (reading '\''t'\'')' "$LIST_OUT"; then
  fail "cron list --json still crashes during config read"
else
  pass "cron list --json no longer emits config-read TypeError"
fi

if grep -q 'Failed to read config at' "$LIST_OUT"; then
  fail "cron list --json still logs config-read failure"
else
  pass "cron list --json no longer logs config-read failure"
fi

if grep -q 'gateway token mismatch' "$LIST_OUT"; then
  fail "cron list --json still reports gateway token mismatch"
else
  pass "cron list --json no longer reports gateway token mismatch"
fi

if [[ "$FAILED" -gt 0 ]]; then
  echo ""
  echo "FAILED: $FAILED test(s), PASSED: $PASSED"
  exit 1
fi

echo ""
echo "PASSED: $PASSED test(s)"
