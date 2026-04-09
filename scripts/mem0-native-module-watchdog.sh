#!/usr/bin/env bash
# mem0-native-module-watchdog.sh
# Checks that better-sqlite3 was compiled for the same Node MODULE_VERSION
# currently in use by the OpenClaw gateway. Runs every 4 hours via launchd.
#
# Usage:
#   bash mem0-native-module-watchdog.sh          # check only (exits 1 on mismatch)
#   bash mem0-native-module-watchdog.sh --fix    # check + auto-rebuild on mismatch
#   bash mem0-native-module-watchdog.sh --status # print status and exit 0 always
#
# Exit codes:
#   0 — MODULE_VERSION matches, or baseline created on first run (module verified OK)
#   1 — MODULE_VERSION mismatch (needs rebuild); or --fix failed
#   2 — baseline file missing AND better-sqlite3 fails to load (ABI mismatch — run --fix)
set -uo pipefail

GATEWAY_NODE="/Users/jleechan/.nvm/versions/node/v22.22.0/bin/node"
GATEWAY_NPM="/Users/jleechan/.nvm/versions/node/v22.22.0/bin/npm"
BETTER_SQLITE3_DIR="$HOME/.openclaw/extensions/openclaw-mem0"
BASELINE_FILE="$HOME/.openclaw/.gateway-node-version"
LOG_FILE="$HOME/.openclaw/logs/mem0-watchdog.log"
FIX_MODE="${1:-}"
TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')

# Ensure log directory exists
mkdir -p "$(dirname "$LOG_FILE")"

_log() {
  local msg="[$TIMESTAMP] $*"
  if [ -t 1 ]; then
    # Interactive: print to terminal AND write to log file
    echo "$msg"
    echo "$msg" >> "$LOG_FILE"
  else
    # Non-interactive (launchd): stdout is already the log file via plist redirect
    echo "$msg"
  fi
}

_log_only() {
  echo "[$TIMESTAMP] $*" >> "$LOG_FILE"
}

# Rotate log if > 5 MB
if [ -f "$LOG_FILE" ] && [ "$(wc -c < "$LOG_FILE" 2>/dev/null || echo 0)" -gt 5242880 ]; then
  mv "$LOG_FILE" "${LOG_FILE}.1"
  _log "Log rotated (was >5MB)"
fi

# --- Resolve current MODULE_VERSION ---
if [ ! -x "$GATEWAY_NODE" ]; then
  _log "ERROR: gateway node binary not found: $GATEWAY_NODE"
  exit 1
fi

CURRENT_MODVER=$("$GATEWAY_NODE" -e "process.stdout.write(String(process.versions.modules))" 2>/dev/null || echo "unknown")
if [ "$CURRENT_MODVER" = "unknown" ]; then
  _log "ERROR: could not determine MODULE_VERSION from $GATEWAY_NODE"
  exit 1
fi

# --- Check baseline ---
if [ ! -f "$BASELINE_FILE" ]; then
  # Verify better-sqlite3 actually loads before recording baseline
  if "$GATEWAY_NODE" -e "require('$BETTER_SQLITE3_DIR/node_modules/better-sqlite3')" 2>/dev/null; then
    echo "$CURRENT_MODVER" > "$BASELINE_FILE"
    _log "INFO: No baseline found — verified better-sqlite3 loads, recorded MODULE_VERSION=$CURRENT_MODVER"
    exit 0
  else
    _log "WARN: No baseline found — better-sqlite3 fails to load (ABI mismatch); run with --fix to rebuild"
    exit 2
  fi
fi

STORED_MODVER=$(cat "$BASELINE_FILE" | tr -d '[:space:]')

if [ "$FIX_MODE" = "--status" ]; then
  echo "MODULE_VERSION: current=$CURRENT_MODVER stored=$STORED_MODVER"
  if [ "$CURRENT_MODVER" = "$STORED_MODVER" ]; then
    echo "STATUS: OK"
  else
    echo "STATUS: MISMATCH (run with --fix to rebuild)"
  fi
  exit 0
fi

if [ "$CURRENT_MODVER" = "$STORED_MODVER" ]; then
  # Version numbers match — but also verify the module actually loads.
  # Root cause of recurring mismatch: something (npm install, external rebuild) can
  # recompile better-sqlite3 for a different Node while the baseline version stays the
  # same. Without this load test, the watchdog says OK while openclaw mem0 is broken.
  if "$GATEWAY_NODE" -e "require('$BETTER_SQLITE3_DIR/node_modules/better-sqlite3')" 2>/dev/null; then
    _log_only "OK: MODULE_VERSION=$CURRENT_MODVER matches baseline and module loads"
    exit 0
  else
    _log "WARN: MODULE_VERSION=$CURRENT_MODVER matches baseline but better-sqlite3 FAILS to load — external rebuild likely changed ABI"
    # Fall through to mismatch/fix path below
  fi
fi

# --- Mismatch detected ---
_log "WARN: MODULE_VERSION mismatch — stored=$STORED_MODVER current=$CURRENT_MODVER"
_log "WARN: better-sqlite3 may be compiled for wrong Node — mem0 recall/capture will fail"

if [ "$FIX_MODE" != "--fix" ]; then
  _log "INFO: Run with --fix to auto-rebuild better-sqlite3, or:"
  _log "INFO:   npm rebuild better-sqlite3 --prefix ~/.openclaw/extensions/openclaw-mem0"
  exit 1
fi

# --- Auto-rebuild ---
_log "FIX: Starting better-sqlite3 rebuild for MODULE_VERSION=$CURRENT_MODVER..."

if [ ! -d "$BETTER_SQLITE3_DIR" ]; then
  _log "ERROR: mem0 extension dir not found: $BETTER_SQLITE3_DIR"
  exit 1
fi

if [ ! -d "$BETTER_SQLITE3_DIR/node_modules/better-sqlite3" ]; then
  _log "ERROR: better-sqlite3 not installed at $BETTER_SQLITE3_DIR/node_modules/better-sqlite3"
  _log "INFO: Run: npm install --prefix $BETTER_SQLITE3_DIR"
  exit 1
fi

# Clean stale build artifacts before rebuild
rm -rf "$BETTER_SQLITE3_DIR/node_modules/better-sqlite3/build" \
       "$BETTER_SQLITE3_DIR/node_modules/better-sqlite3/prebuilds" 2>/dev/null || true

REBUILD_OUTPUT=$(
  cd "$BETTER_SQLITE3_DIR" \
  && "$GATEWAY_NPM" rebuild better-sqlite3 2>&1
)
REBUILD_EXIT=$?

if [ "$REBUILD_EXIT" -eq 0 ]; then
  echo "$CURRENT_MODVER" > "$BASELINE_FILE"
  _log "FIX: Rebuild OK — baseline updated to MODULE_VERSION=$CURRENT_MODVER"
  _log "FIX: Restarting openclaw gateway to load rebuilt module..."
  launchctl kickstart -k "gui/$(id -u)/ai.openclaw.gateway" >/dev/null 2>&1 \
    && _log "FIX: Gateway restarted via launchctl kickstart" \
    || _log "WARN: Gateway restart failed — reload manually: launchctl kickstart -k gui/$(id -u)/ai.openclaw.gateway"
  exit 0
else
  _log "ERROR: Rebuild failed (exit $REBUILD_EXIT)"
  _log "ERROR: Output: $REBUILD_OUTPUT"
  exit 1
fi
