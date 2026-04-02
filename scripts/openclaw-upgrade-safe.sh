#!/usr/bin/env bash
# openclaw-upgrade-safe.sh
# Safe wrapper around `npm install -g openclaw@<version>` that validates
# SDK compatibility and runs pre-flight checks before upgrading.
#
# Usage:
#   bash openclaw-upgrade-safe.sh <new-version>               # interactive confirm
#   bash openclaw-upgrade-safe.sh <new-version> --yes         # non-interactive
#   bash openclaw-upgrade-safe.sh <new-version> --ack-minor-jump  # allow 0.x SDK jump
#   bash openclaw-upgrade-safe.sh <new-version> --yes --ack-minor-jump
#
# Example:
#   bash openclaw-upgrade-safe.sh 2026.4.1
set -uo pipefail

GATEWAY_NODE="${HOME}/.nvm/versions/node/v22.22.0/bin/node"
GATEWAY_NPM="${HOME}/.nvm/versions/node/v22.22.0/bin/npm"
PREFLIGHT="$HOME/.smartclaw/scripts/gateway-preflight.sh"
BETTER_SQLITE3_DIR="$HOME/.smartclaw/extensions/openclaw-mem0"
BASELINE_FILE="$HOME/.smartclaw/.gateway-node-version"

NEW_VERSION="${1:-}"
YES_FLAG=""
ACK_JUMP=""

for arg in "${@:2}"; do
  case "$arg" in
    --yes) YES_FLAG="--yes" ;;
    --ack-minor-jump) ACK_JUMP="--ack-minor-jump" ;;
  esac
done

if [ -z "$NEW_VERSION" ]; then
  echo "Usage: $(basename "$0") <new-openclaw-version> [--yes] [--ack-minor-jump]"
  echo "Example: $(basename "$0") 2026.4.1"
  exit 1
fi

echo "=== OpenClaw Safe Upgrade: $NEW_VERSION ==="
echo ""

# --- Step 1: Pre-flight check ---
echo "--- Step 1/5: Pre-flight check ---"
if [ -f "$PREFLIGHT" ]; then
  bash "$PREFLIGHT" || {
    echo ""
    echo "PREFLIGHT FAILED — resolve issues before upgrading."
    echo "Run: bash $PREFLIGHT --fix"
    exit 1
  }
else
  echo "FAIL: preflight script not found at $PREFLIGHT — cannot verify gateway health before upgrade"
  echo "Restore the preflight script at $PREFLIGHT before upgrading"
  exit 1
fi
echo ""

# --- Step 2: SDK compatibility check ---
echo "--- Step 2/5: SDK compatibility check ---"
# Source validate_sdk_compatibility from preflight
if [ -f "$PREFLIGHT" ]; then
  # Extract and eval only the function definition (safe subset)
  # We re-implement inline to avoid sourcing the full preflight
  _cur_sdk="unknown"
  [ -f "$HOME/.smartclaw/.current-sdk-version" ] \
    && _cur_sdk=$(cat "$HOME/.smartclaw/.current-sdk-version" | tr -d '[:space:]')

  _new_sdk=$("$GATEWAY_NPM" view "openclaw@${NEW_VERSION}" dependencies 2>/dev/null \
    | grep -i agentclientprotocol | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)
  _new_sdk="${_new_sdk:-unknown}"

  echo "  Current openclaw @agentclientprotocol/sdk : ${_cur_sdk}"
  echo "  Target  openclaw @agentclientprotocol/sdk : ${_new_sdk}"

  _sdk_ok=1
  if [ "$_new_sdk" = "unknown" ] || [ "$_cur_sdk" = "unknown" ]; then
    echo "  WARN: Could not determine SDK version(s) — proceeding (check manually)"
  else
    _cur_major=$(echo "$_cur_sdk" | cut -d. -f1); _cur_minor=$(echo "$_cur_sdk" | cut -d. -f2)
    _new_major=$(echo "$_new_sdk" | cut -d. -f1); _new_minor=$(echo "$_new_sdk" | cut -d. -f2)
    _major_diff=$(( _new_major - _cur_major ))
    _minor_diff=$(( _new_minor - _cur_minor ))

    if [ "$_major_diff" -gt 0 ]; then
      echo "  FAIL: Major SDK version jump ($_cur_sdk → $_new_sdk) — protocol mismatch WILL cause errors"
      echo "  Incident ref: openclaw 2026.3.24→2026.3.28 (SDK 0.16→0.17) → 367 ws-stream 500s/day"
      _sdk_ok=0
    elif [ "$_cur_major" -eq 0 ] && [ "$_minor_diff" -gt 0 ] && [ "$ACK_JUMP" != "--ack-minor-jump" ]; then
      echo "  FAIL: 0.x minor jump ($_cur_sdk → $_new_sdk) — likely breaking changes"
      echo "  Pass --ack-minor-jump to override (risk: ws-stream errors)"
      echo "  Incident ref: openclaw 2026.3.24→2026.3.28 (SDK 0.16→0.17) → 367 ws-stream 500s/day"
      _sdk_ok=0
    else
      echo "  OK: SDK compatible ($_cur_sdk → $_new_sdk)"
    fi
  fi

  if [ "$_sdk_ok" -eq 0 ]; then
    echo ""
    echo "SDK CHECK FAILED — aborting upgrade."
    echo "If you understand the risk, re-run with --ack-minor-jump"
    exit 1
  fi
fi
echo ""

# --- Step 2.5: Staging canary (fail-closed gate) ---
STAGING_GATEWAY="$HOME/.smartclaw/scripts/staging-gateway.sh"
STAGING_CANARY="$HOME/.smartclaw/scripts/staging-canary.sh"
SKIP_STAGING="${SKIP_STAGING:-}"
if [ "$SKIP_STAGING" = "1" ]; then
  echo "--- Step 2.5/5: Staging canary (SKIPPED — SKIP_STAGING=1) ---"
  echo "  WARNING: Staging validation bypassed by explicit override."
  echo ""
elif [ ! -x "$STAGING_GATEWAY" ] || [ ! -x "$STAGING_CANARY" ]; then
  echo "--- Step 2.5/5: Staging canary FAILED ---"
  echo "  FATAL: Staging scripts not found or not executable:"
  [ ! -x "$STAGING_GATEWAY" ] && echo "    missing: $STAGING_GATEWAY"
  [ ! -x "$STAGING_CANARY" ] && echo "    missing: $STAGING_CANARY"
  echo "  Install staging scripts or set SKIP_STAGING=1 to bypass (not recommended)."
  exit 1
else
  echo "--- Step 2.5/5: Staging canary test ---"
  echo "  Starting staging gateway (port 18790) for pre-upgrade validation..."
  bash "$STAGING_GATEWAY" start
  _staging_start_rc=$?
  if [ "$_staging_start_rc" -ne 0 ]; then
    echo "  FATAL: Staging gateway failed to start (exit $_staging_start_rc)"
    echo "  Cannot validate upgrade without staging. Set SKIP_STAGING=1 to bypass (not recommended)."
    exit 1
  fi
  echo "  Running 6-point canary against staging..."
  bash "$STAGING_CANARY" --port 18790
  _canary_rc=$?
  bash "$STAGING_GATEWAY" stop
  if [ "$_canary_rc" -ne 0 ]; then
    echo ""
    echo "STAGING CANARY FAILED (exit $_canary_rc) — aborting upgrade."
    echo "Fix issues on staging before upgrading production."
    exit 1
  fi
  echo "  Staging canary PASSED — safe to proceed."
  echo ""
fi

# --- Step 3: Show what will change ---
echo "--- Step 3/5: What will change ---"
_current_version=$("$GATEWAY_NPM" list -g openclaw --depth=0 --json 2>/dev/null \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('dependencies',{}).get('openclaw',{}).get('version','(not installed)'))" 2>/dev/null \
  || echo "(not installed)")
echo "  Current version : $_current_version"
echo "  Target version  : $NEW_VERSION"
echo "  Node binary     : $GATEWAY_NODE"
echo "  Module baseline : $BASELINE_FILE"
echo ""

# --- Step 4: Confirm ---
if [ "$YES_FLAG" != "--yes" ]; then
  read -r -p "Proceed with upgrade? [y/N] " _confirm
  case "$_confirm" in
    y|Y|yes|YES) ;;
    *)
      echo "Upgrade cancelled."
      exit 0
      ;;
  esac
fi
echo ""

# --- Step 5a: Backup openclaw.json ---
echo "--- Step 5/5: Backup + upgrade ---"
_backup="$HOME/.smartclaw/openclaw.json.pre-upgrade-$(date +%s)"
if [ -f "$HOME/.smartclaw/openclaw.json" ]; then
  cp "$HOME/.smartclaw/openclaw.json" "$_backup"
  echo "  Backed up openclaw.json → $(basename "$_backup")"
fi

# --- Step 5b: Run upgrade ---
echo "  Running: $GATEWAY_NPM install -g openclaw@${NEW_VERSION}"
"$GATEWAY_NPM" install -g "openclaw@${NEW_VERSION}" || {
  echo ""
  echo "UPGRADE FAILED — npm install returned non-zero"
  echo "openclaw.json backup preserved at: $_backup"
  exit 1
}
echo "  Upgrade complete."
echo ""

# --- Step 5c: Rebuild native modules ---
echo "--- Step 5/5: Rebuild native modules + record baseline ---"
_baseline_recorded=0
if [ -d "$BETTER_SQLITE3_DIR/node_modules/better-sqlite3" ]; then
  echo "  Rebuilding better-sqlite3 for new Node..."
  _rebuild_out=$(cd "$BETTER_SQLITE3_DIR" \
    && "$GATEWAY_NPM" rebuild better-sqlite3 2>&1)
  _rebuild_rc=$?
  if [ "$_rebuild_rc" -eq 0 ]; then
    # Verify the rebuilt module actually loads before recording baseline
    if "$GATEWAY_NODE" -e "require('$BETTER_SQLITE3_DIR/node_modules/better-sqlite3')" 2>/dev/null; then
      echo "  Rebuild OK"
      _baseline_recorded=1
    else
      echo "  WARN: Rebuild succeeded but module still fails to load — not updating baseline"
      echo "  Run mem0-native-module-watchdog.sh --fix to investigate"
    fi
  else
    echo "  WARN: Rebuild failed (exit $_rebuild_rc) — run mem0-native-module-watchdog.sh --fix manually"
    echo "  Not updating baseline after failed rebuild."
  fi
else
  echo "  SKIP: better-sqlite3 not found at $BETTER_SQLITE3_DIR/node_modules/better-sqlite3"
fi

# Record new MODULE_VERSION baseline ONLY after successful rebuild
if [ "$_baseline_recorded" -eq 1 ] && [ -x "$GATEWAY_NODE" ]; then
  _modver=$("$GATEWAY_NODE" -e "process.stdout.write(String(process.versions.modules))" 2>/dev/null || echo "unknown")
  if [ "$_modver" != "unknown" ]; then
    echo "$_modver" > "$BASELINE_FILE"
    echo "  Baseline updated: MODULE_VERSION=$_modver → $BASELINE_FILE"
  fi
fi

# Record new SDK version
if [ -n "${_new_sdk:-}" ] && [ "$_new_sdk" != "unknown" ]; then
  echo "$_new_sdk" > "$HOME/.smartclaw/.current-sdk-version"
  echo "  SDK baseline updated: @agentclientprotocol/sdk=$_new_sdk"
fi
echo ""

echo "=== Upgrade complete: openclaw@${NEW_VERSION} ==="
echo "Run 'openclaw gateway status' to verify the gateway is healthy."
echo "Check logs: tail -f ~/.smartclaw/logs/gateway.log"
