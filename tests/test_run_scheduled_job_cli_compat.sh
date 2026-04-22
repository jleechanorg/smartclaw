#!/bin/bash
# Regression: run-scheduled-job must use current openclaw agent flags and
# explicit prod state/config wiring.
set -euo pipefail

SCRIPT="${HOME}/.smartclaw/run-scheduled-job.sh"

PASSED=0
FAILED=0

pass() { echo "PASS: $1"; PASSED=$((PASSED + 1)); }
fail() { echo "FAIL: $1"; FAILED=$((FAILED + 1)); }

if [[ ! -f "$SCRIPT" ]]; then
  echo "FAIL: missing $SCRIPT"
  exit 1
fi

if grep -q -- '--timeout-seconds' "$SCRIPT"; then
  fail "legacy --timeout-seconds flag still present"
else
  pass "legacy --timeout-seconds flag removed"
fi

if grep -q -- '--timeout "\$timeout_seconds"' "$SCRIPT"; then
  pass "runner uses supported --timeout flag"
else
  fail "runner missing supported --timeout flag"
fi

if grep -q 'OPENCLAW_STATE_DIR="\$openclaw_state_dir"' "$SCRIPT"; then
  pass "runner exports OPENCLAW_STATE_DIR to openclaw invocation"
else
  fail "runner missing OPENCLAW_STATE_DIR export"
fi

if grep -q 'OPENCLAW_CONFIG_PATH="\$openclaw_config_path"' "$SCRIPT"; then
  pass "runner exports OPENCLAW_CONFIG_PATH to openclaw invocation"
else
  fail "runner missing OPENCLAW_CONFIG_PATH export"
fi

if [[ "$FAILED" -gt 0 ]]; then
  echo ""
  echo "FAILED: $FAILED test(s), PASSED: $PASSED"
  exit 1
fi

echo ""
echo "PASSED: $PASSED test(s)"
