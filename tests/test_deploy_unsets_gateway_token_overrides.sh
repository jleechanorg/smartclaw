#!/bin/bash
# Regression: deploy wrapper must unset shell gateway token overrides when
# calling openclaw/monitor, so stale env values cannot break local loopback auth.
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

gateway_unset_count="$(grep -c -- '-u OPENCLAW_GATEWAY_TOKEN' "$SCRIPT" || true)"
remote_unset_count="$(grep -c -- '-u OPENCLAW_GATEWAY_REMOTE_TOKEN' "$SCRIPT" || true)"

if [[ "$gateway_unset_count" -ge 4 ]]; then
  pass "deploy unsets OPENCLAW_GATEWAY_TOKEN for openclaw invocations (count=$gateway_unset_count)"
else
  fail "deploy missing OPENCLAW_GATEWAY_TOKEN unset guards (count=$gateway_unset_count)"
fi

if [[ "$remote_unset_count" -ge 4 ]]; then
  pass "deploy unsets OPENCLAW_GATEWAY_REMOTE_TOKEN for openclaw invocations (count=$remote_unset_count)"
else
  fail "deploy missing OPENCLAW_GATEWAY_REMOTE_TOKEN unset guards (count=$remote_unset_count)"
fi

if [[ "$FAILED" -gt 0 ]]; then
  echo ""
  echo "FAILED: $FAILED test(s), PASSED: $PASSED"
  exit 1
fi

echo ""
echo "PASSED: $PASSED test(s)"
