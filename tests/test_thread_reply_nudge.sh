#!/usr/bin/env bash
# test_thread_reply_nudge.sh — TDD tests for thread-reply-nudge channel resolution

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NUDGE_SCRIPT="$SCRIPT_DIR/../scripts/thread-reply-nudge.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

PASSED=0
FAILED=0

log_pass() { echo -e "${GREEN}✓ PASS${NC}: $1"; ((PASSED++)); }
log_fail() { echo -e "${RED}✗ FAIL${NC}: $1"; ((FAILED++)); }
log_info() { echo -e "${YELLOW}ℹ INFO${NC}: $1"; }

# Source the nudge script in function-only mode (IS_SOURCED=1 skips main body).
# Restore shell options after sourcing because the script sets -euo pipefail
# which would otherwise bleed into the test body and cause early exit on any
# non-zero return code.
IS_SOURCED=1 source "$NUDGE_SCRIPT"
set +e  # undo -e added by the sourced script; tests use explicit log_pass/fail

# Create a minimal openclaw.json mock with known channels (includes the previously-missed one)
make_mock_config() {
  local dir="$1"
  cat >"$dir/openclaw.json" <<'EOF'
{
  "channels": {
    "slack": {
      "channels": {
        "${SLACK_CHANNEL_ID}": {"allow": true},
        "C0AJQ5M0A0Y": {"allow": true},
        "C0AH3RY3DK6": {"allow": true},
        "C0AJ3SD5C79": {"allow": true},
        "*": {"allow": true}
      }
    }
  }
}
EOF
}

# Helper: reset env state between tests
reset_env() {
  unset THREAD_REPLY_CHANNEL 2>/dev/null || true
  unset OPENCLAW_CONFIG_FILE 2>/dev/null || true
}

# ── Test 1: env var override (comma-separated) ────────────────────────────────
test_env_var_override() {
  log_info "Test: THREAD_REPLY_CHANNEL env var returns exactly those channels"
  reset_env

  local tmpdir; tmpdir="$(mktemp -d)"
  make_mock_config "$tmpdir"
  export THREAD_REPLY_CHANNEL="CA123,CB456"
  export OPENCLAW_CONFIG_FILE="$tmpdir/openclaw.json"
  local result; result="$(resolve_nudge_channels 2>/dev/null)"
  reset_env; rm -rf "$tmpdir"

  if echo "$result" | grep -q "CA123" && echo "$result" | grep -q "CB456"; then
    log_pass "Env var channels CA123 CB456 returned"
  else
    log_fail "Env var override failed; got: $result"
  fi
}

# ── Test 2: json channels included (all explicit, no wildcard) ────────────────
test_json_channels_included() {
  log_info "Test: All explicit channels from openclaw.json are included"
  reset_env

  local tmpdir; tmpdir="$(mktemp -d)"
  make_mock_config "$tmpdir"
  export OPENCLAW_CONFIG_FILE="$tmpdir/openclaw.json"
  local result; result="$(resolve_nudge_channels 2>/dev/null)"
  reset_env; rm -rf "$tmpdir"

  local ok=true
  for ch in ${SLACK_CHANNEL_ID} C0AJQ5M0A0Y C0AH3RY3DK6 C0AJ3SD5C79; do
    if ! echo "$result" | grep -q "$ch"; then
      log_fail "Channel $ch missing from result: $result"
      ok=false
    fi
  done
  [[ "$ok" == "true" ]] && log_pass "All 4 explicit channels included"
}

# ── Test 3: wildcard excluded ─────────────────────────────────────────────────
test_wildcard_excluded() {
  log_info "Test: Wildcard '*' is NOT included in channel list"
  reset_env

  local tmpdir; tmpdir="$(mktemp -d)"
  make_mock_config "$tmpdir"
  export OPENCLAW_CONFIG_FILE="$tmpdir/openclaw.json"
  local result; result="$(resolve_nudge_channels 2>/dev/null)"
  reset_env; rm -rf "$tmpdir"

  if echo "$result" | grep -qE '(^| )\*( |$)'; then
    log_fail "Wildcard '*' found in channel list: $result"
  else
    log_pass "Wildcard '*' excluded from channel list"
  fi
}

# ── Test 4: fallback when no json and no env var ──────────────────────────────
test_fallback_hardcoded() {
  log_info "Test: Fallback channel used when no json and no env var"
  reset_env

  export OPENCLAW_CONFIG_FILE="/nonexistent/openclaw.json"
  local result; result="$(resolve_nudge_channels 2>/dev/null)"
  reset_env

  if [[ -n "$result" ]]; then
    log_pass "Fallback returned non-empty channel list: $result"
  else
    log_fail "Fallback returned empty channel list"
  fi
}

# ── Test 5: DRY_RUN=1 prompt contains all channels ────────────────────────────
test_prompt_includes_channels() {
  log_info "Test: DRY_RUN=1 prompt contains all resolved channels"
  reset_env

  local tmpdir; tmpdir="$(mktemp -d)"
  make_mock_config "$tmpdir"

  # Use isolated lock/log dirs to bypass the 90s debounce and avoid lock conflicts
  local lockdir="$tmpdir/nudge.lock"
  local logdir="$tmpdir/logs"
  local prompt
  prompt="$(DRY_RUN=1 OPENCLAW_CONFIG_FILE="$tmpdir/openclaw.json" \
    NUDGE_LOCK_DIR="$lockdir" NUDGE_LOG_DIR="$logdir" \
    bash "$NUDGE_SCRIPT" 2>&1 || true)"
  rm -rf "$tmpdir"

  local ok=true
  for ch in ${SLACK_CHANNEL_ID} C0AJQ5M0A0Y C0AH3RY3DK6 C0AJ3SD5C79; do
    if ! echo "$prompt" | grep -q "$ch"; then
      log_fail "Channel $ch missing from prompt: $prompt"
      ok=false
    fi
  done
  [[ "$ok" == "true" ]] && log_pass "All channels present in DRY_RUN prompt"
}

# ── Test 6: the previously-missed channel C0AJ3SD5C79 is now covered ──────────
test_previously_missed_channel() {
  log_info "Test: C0AJ3SD5C79 (previously missed) is now covered"
  reset_env

  local tmpdir; tmpdir="$(mktemp -d)"
  make_mock_config "$tmpdir"
  export OPENCLAW_CONFIG_FILE="$tmpdir/openclaw.json"
  local result; result="$(resolve_nudge_channels 2>/dev/null)"
  reset_env; rm -rf "$tmpdir"

  if echo "$result" | grep -q "C0AJ3SD5C79"; then
    log_pass "C0AJ3SD5C79 is now covered by nudge"
  else
    log_fail "C0AJ3SD5C79 still missing from nudge channels: $result"
  fi
}

# ── Main ──────────────────────────────────────────────────────────────────────
main() {
  echo "========================================"
  echo "thread-reply-nudge Channel Resolution Tests"
  echo "========================================"
  echo ""

  test_env_var_override
  test_json_channels_included
  test_wildcard_excluded
  test_fallback_hardcoded
  test_prompt_includes_channels
  test_previously_missed_channel

  echo ""
  echo "========================================"
  echo "Results: $PASSED passed, $FAILED failed"
  echo "========================================"

  [[ $FAILED -gt 0 ]] && exit 1
  exit 0
}

main "$@"
