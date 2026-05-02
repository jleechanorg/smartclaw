#!/bin/bash
# Regression test for doctor.sh profile inference when the plist omits OPENCLAW_STATE_DIR.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$REPO_ROOT/scripts/doctor.sh"

PASSED=0
FAILED=0

pass() { echo "PASS: $1"; PASSED=$((PASSED + 1)); }
fail() { echo "FAIL: $1"; FAILED=$((FAILED + 1)); }

TMP_ROOT="$(mktemp -d /tmp/test-doctor-profile-inference.XXXXXX)"
cleanup() { rm -rf "$TMP_ROOT"; }
trap cleanup EXIT

HOME_DIR="$TMP_ROOT/home"
mkdir -p "$HOME_DIR"

extract_function() {
  local fn_name="$1"
  python3 - "$SCRIPT" "$fn_name" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
fn_name = sys.argv[2]
text = path.read_text(encoding="utf-8").splitlines()
out = []
capture = False
depth = 0
for line in text:
    if not capture and line.startswith(f"{fn_name}()"):
        capture = True
    if capture:
        out.append(line)
        depth += line.count("{")
        depth -= line.count("}")
        if depth == 0:
            break
print("\n".join(out))
PY
}

eval "$(extract_function infer_gateway_profile_dir_from_port)"

PLIST="$TMP_ROOT/ai.smartclaw.gateway.plist"
python3 - "$PLIST" <<'PY'
import plistlib
import sys

plist = {
    "Label": "ai.smartclaw.gateway",
    "EnvironmentVariables": {
        "OPENCLAW_GATEWAY_PORT": "18789"
    },
}
with open(sys.argv[1], "wb") as f:
    plistlib.dump(plist, f)
PY

if /usr/bin/plutil -extract EnvironmentVariables.OPENCLAW_STATE_DIR raw -o - "$PLIST" >/dev/null 2>&1; then
  fail "plist unexpectedly contains OPENCLAW_STATE_DIR"
else
  pass "plist omits OPENCLAW_STATE_DIR"
fi

HOME="$HOME_DIR"
PORT="$(/usr/bin/plutil -extract EnvironmentVariables.OPENCLAW_GATEWAY_PORT raw -o - "$PLIST")"
INFERRED="$(HOME="$HOME_DIR" infer_gateway_profile_dir_from_port "$PORT")"

if [[ "$INFERRED" == "$HOME_DIR/.smartclaw_prod" ]]; then
  pass "doctor infers prod profile from gateway port 18789"
else
  fail "doctor inferred '$INFERRED' instead of '$HOME_DIR/.smartclaw_prod'"
fi

INFERRED_STAGING="$(HOME="$HOME_DIR" infer_gateway_profile_dir_from_port "18810")"
if [[ "$INFERRED_STAGING" == "$HOME_DIR/.smartclaw" ]]; then
  pass "doctor infers staging profile from gateway port 18810"
else
  fail "doctor inferred '$INFERRED_STAGING' instead of '$HOME_DIR/.smartclaw' for staging port"
fi

if [[ "$FAILED" -gt 0 ]]; then
  echo ""
  echo "FAILED: $FAILED test(s), PASSED: $PASSED"
  exit 1
fi

echo ""
echo "PASSED: $PASSED test(s)"
