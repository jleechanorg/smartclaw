#!/bin/bash
# Test matrix for resolve_gh_token() in scripts/harness-analyzer.sh
#
# Covers all 4 auth-input modes:
#   1. GH_TOKEN env var (literal token)
#   2. GITHUB_TOKEN env var (literal, not a file path)
#   3. GITHUB_TOKEN env var as file path
#   4. Default file (~/.github_token fallback via GITHUB_TOKEN_SOURCE)
#
# Usage: bash tests/test_harness_analyzer_auth.sh

set -euo pipefail

PASS=0
FAIL=0
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# Extract resolve_gh_token function from harness-analyzer.sh for isolated testing
FUNC_SOURCE=$(sed -n '/^resolve_gh_token()/,/^}/p' "$SCRIPT_DIR/scripts/harness-analyzer.sh")

run_test() {
    local name="$1"
    local expected="$2"
    local actual="$3"

    if [ "$actual" = "$expected" ]; then
        echo "  PASS: $name"
        PASS=$((PASS + 1))
    else
        echo "  FAIL: $name (expected='$expected', got='$actual')"
        FAIL=$((FAIL + 1))
    fi
}

echo "=== resolve_gh_token() auth mode tests ==="

# --- Mode 1: GH_TOKEN literal ---
echo ""
echo "Mode 1: GH_TOKEN env var (literal)"
RESULT=$(
    unset GITHUB_TOKEN 2>/dev/null || true
    export GH_TOKEN="ghp_mode1_literal"
    eval "$FUNC_SOURCE"
    resolve_gh_token
)
run_test "GH_TOKEN literal is returned" "ghp_mode1_literal" "$RESULT"

# GH_TOKEN takes priority over GITHUB_TOKEN
RESULT=$(
    export GH_TOKEN="ghp_gh_token_wins"
    export GITHUB_TOKEN="ghp_github_token_loses"
    eval "$FUNC_SOURCE"
    resolve_gh_token
)
run_test "GH_TOKEN takes priority over GITHUB_TOKEN" "ghp_gh_token_wins" "$RESULT"

# --- Mode 2: GITHUB_TOKEN literal (not a file path) ---
echo ""
echo "Mode 2: GITHUB_TOKEN env var (literal, not a file)"
RESULT=$(
    unset GH_TOKEN 2>/dev/null || true
    export GITHUB_TOKEN="ghp_mode2_literal"
    eval "$FUNC_SOURCE"
    resolve_gh_token
)
run_test "GITHUB_TOKEN literal is returned when not a file" "ghp_mode2_literal" "$RESULT"

# --- Mode 3: GITHUB_TOKEN as file path ---
echo ""
echo "Mode 3: GITHUB_TOKEN env var as file path"
TOKEN_FILE=$(mktemp)
printf 'ghp_mode3_from_file' > "$TOKEN_FILE"
RESULT=$(
    unset GH_TOKEN 2>/dev/null || true
    export GITHUB_TOKEN="$TOKEN_FILE"
    # GITHUB_TOKEN_SOURCE picks up from GITHUB_TOKEN
    export GITHUB_TOKEN_SOURCE="$TOKEN_FILE"
    eval "$FUNC_SOURCE"
    resolve_gh_token
)
run_test "GITHUB_TOKEN file path is read" "ghp_mode3_from_file" "$RESULT"

# File with trailing newlines/carriage returns
printf 'ghp_mode3_trimmed\r\n' > "$TOKEN_FILE"
RESULT=$(
    unset GH_TOKEN 2>/dev/null || true
    export GITHUB_TOKEN="$TOKEN_FILE"
    export GITHUB_TOKEN_SOURCE="$TOKEN_FILE"
    eval "$FUNC_SOURCE"
    resolve_gh_token
)
run_test "File token has \\r\\n stripped" "ghp_mode3_trimmed" "$RESULT"
rm -f "$TOKEN_FILE"

# --- Mode 4: Default file path fallback ---
echo ""
echo "Mode 4: Default file path fallback (GITHUB_TOKEN_SOURCE)"
DEFAULT_FILE=$(mktemp)
printf 'ghp_mode4_default' > "$DEFAULT_FILE"
RESULT=$(
    unset GH_TOKEN 2>/dev/null || true
    unset GITHUB_TOKEN 2>/dev/null || true
    export GITHUB_TOKEN_SOURCE="$DEFAULT_FILE"
    eval "$FUNC_SOURCE"
    resolve_gh_token
)
run_test "Default file fallback is read" "ghp_mode4_default" "$RESULT"
rm -f "$DEFAULT_FILE"

# --- Mode 5: All missing → failure ---
echo ""
echo "Mode 5: No token available (should fail)"
EXIT_CODE=0
(
    unset GH_TOKEN 2>/dev/null || true
    unset GITHUB_TOKEN 2>/dev/null || true
    export GITHUB_TOKEN_SOURCE="/nonexistent/path/token"
    eval "$FUNC_SOURCE"
    resolve_gh_token
) >/dev/null 2>&1 || EXIT_CODE=$?
run_test "Returns non-zero when no token found" "1" "$EXIT_CODE"

# --- Summary ---
echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
