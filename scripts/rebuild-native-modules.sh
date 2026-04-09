#!/usr/bin/env bash
# rebuild-native-modules.sh
# Detects and fixes NODE_MODULE_VERSION mismatch for native addons in openclaw extensions.
# Called by health-check.sh on gateway start, or manually after `brew upgrade node`.
#
# Exit codes: 0 = all OK or rebuilt successfully, 1 = rebuild failed

set -euo pipefail

LOG_DIR="${HOME}/.openclaw/logs"
LOG_FILE="${LOG_DIR}/native-module-rebuild.log"
mkdir -p "$LOG_DIR"

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%dT%H:%M:%S')" "$*" | tee -a "$LOG_FILE"; }

# Resolve the node binary the gateway actually uses.
# Priority: env override > running gateway process > launchd plist > nvm fallback
# NOTE: Do NOT fall back to /opt/homebrew/bin/node — that is Node 24 (modules=137)
#       but the gateway plist uses nvm Node 22 (modules=127). Using the wrong Node
#       causes rebuild-native-modules to recompile better-sqlite3 for the wrong ABI,
#       silently breaking memory lookup until manually fixed.
detect_gateway_node() {
  # 1. Explicit override
  [[ -n "${OPENCLAW_GATEWAY_NODE:-}" ]] && echo "$OPENCLAW_GATEWAY_NODE" && return

  # 2. Check the running gateway process (most reliable)
  local gw_pid gw_node
  gw_pid="$(pgrep -f 'openclaw-gateway' 2>/dev/null | head -1)" || true
  if [[ -n "$gw_pid" ]]; then
    gw_node="$(lsof -p "$gw_pid" 2>/dev/null | awk '/\/node/{print $NF; exit}')" || true
    [[ -n "$gw_node" && -x "$gw_node" ]] && echo "$gw_node" && return
  fi

  # 3. Read the gateway launchd plist — ground truth for which node the gateway uses
  local plist_node
  for plist in \
    "$HOME/Library/LaunchAgents/ai.openclaw.gateway.plist" \
    "$HOME/Library/LaunchAgents/com.openclaw.gateway.plist"
  do
    if [[ -f "$plist" ]]; then
      plist_node="$(python3 -c "
import plistlib, sys
with open('$plist', 'rb') as f:
    d = plistlib.load(f)
args = d.get('ProgramArguments', [])
for a in args:
    if '/node' in a and '/bin/node' in a:
        print(a)
        break
" 2>/dev/null)" || true
      [[ -n "$plist_node" && -x "$plist_node" ]] && echo "$plist_node" && return
    fi
  done

  # 4. nvm Node 22 — matches gateway plist default; NEVER use Homebrew node as fallback
  local nvm_node="$HOME/.nvm/versions/node/v22.22.0/bin/node"
  [[ -x "$nvm_node" ]] && echo "$nvm_node" && return

  # 5. Last resort: PATH node (log a warning so this mismatch is visible)
  local path_node
  path_node="$(command -v node 2>/dev/null)" || true
  if [[ -n "$path_node" ]]; then
    printf '[%s] WARN: detect_gateway_node fell back to PATH node %s — verify this matches the gateway plist\n' \
      "$(date '+%Y-%m-%dT%H:%M:%S')" "$path_node" >> "$LOG_FILE"
    echo "$path_node" && return
  fi
  echo /usr/local/bin/node
}
GATEWAY_NODE="$(detect_gateway_node)"
GATEWAY_MODULE_VERSION="$("$GATEWAY_NODE" -e 'console.log(process.versions.modules)' 2>/dev/null)" || {
  log "ERROR: cannot determine gateway node MODULE_VERSION from $GATEWAY_NODE"
  exit 1
}

EXTENSIONS_DIR="${HOME}/.openclaw/extensions"
REBUILD_NEEDED=0
REBUILD_OK=0
REBUILD_FAIL=0

check_and_rebuild() {
  local ext_dir="$1"
  local ext_name
  ext_name="$(basename "$ext_dir")"

  # Find packages with native addons (skip test_*.node artifacts)
  local node_files
  node_files="$(find "$ext_dir/node_modules" -name '*.node' -not -name 'test_*' -type f 2>/dev/null)" || return 0
  [[ -z "$node_files" ]] && return 0

  # Extract unique package names containing native addons.
  # For scoped packages (@scope/name), extract the full @scope/name so we test
  # the actual sub-package rather than just the bare scope (@scope).
  # e.g. node_modules/@rolldown/binding-darwin-arm64/file.node → @rolldown/binding-darwin-arm64
  local native_pkgs
  native_pkgs="$(echo "$node_files" | awk -F'node_modules/' '{
    p = $2
    if (p ~ /^@/) {
      # Extract @scope/name (up to second /)
      slash1 = index(p, "/")
      rest = substr(p, slash1 + 1)
      slash2 = index(rest, "/")
      print substr(p, 1, slash1 + slash2 - 1)
    } else {
      sub(/\/.*/, "", p)
      print p
    }
  }' | sort -u)"

  # Test if any native package fails to load with the gateway's node
  local test_failed=0
  while IFS= read -r pkg; do
    [[ -z "$pkg" ]] && continue
    # Skip packages without a package.json — monorepo container packages
    # (e.g. @rolldown) have platform-specific sub-packages but no root entry point.
    if [[ ! -f "$ext_dir/node_modules/$pkg/package.json" ]]; then
      log "SKIP: $pkg has no package.json (monorepo container)"
      continue
    fi
    if ! "$GATEWAY_NODE" -e "require('$ext_dir/node_modules/$pkg')" >/dev/null 2>&1; then
      test_failed=1
      log "MISMATCH: $pkg fails to load with $GATEWAY_NODE (modules=$GATEWAY_MODULE_VERSION)"
      break
    fi
  done <<< "$native_pkgs"

  [[ "$test_failed" -eq 0 ]] && return 0

  REBUILD_NEEDED=1
  log "Rebuilding native modules in $ext_dir with $GATEWAY_NODE..."

  # Use npm rebuild with the gateway's node
  if (cd "$ext_dir" && "$GATEWAY_NODE" "$(dirname "$GATEWAY_NODE")/npm" rebuild 2>&1 | tail -5 >> "$LOG_FILE"); then
    # Verify after rebuild
    local still_broken=0
    while IFS= read -r pkg; do
      [[ -z "$pkg" ]] && continue
      if [[ ! -f "$ext_dir/node_modules/$pkg/package.json" ]]; then
        continue
      fi
      if ! "$GATEWAY_NODE" -e "require('$ext_dir/node_modules/$pkg')" >/dev/null 2>&1; then
        still_broken=1
        break
      fi
    done <<< "$native_pkgs"

    if [[ "$still_broken" -eq 0 ]]; then
      log "OK: $ext_name native modules rebuilt successfully"
      ((REBUILD_OK++))
    else
      log "FAIL: $ext_name still broken after rebuild"
      ((REBUILD_FAIL++))
    fi
  else
    log "FAIL: npm rebuild failed in $ext_dir"
    ((REBUILD_FAIL++))
  fi
}

# Scan all extensions
if [[ -d "$EXTENSIONS_DIR" ]]; then
  for ext in "$EXTENSIONS_DIR"/*/; do
    [[ -d "$ext/node_modules" ]] || continue
    check_and_rebuild "$ext"
  done
fi

if [[ "$REBUILD_NEEDED" -eq 0 ]]; then
  log "All native modules OK (gateway node modules=$GATEWAY_MODULE_VERSION)"
elif [[ "$REBUILD_FAIL" -gt 0 ]]; then
  log "SUMMARY: rebuilt=$REBUILD_OK failed=$REBUILD_FAIL — manual intervention needed"
  exit 1
else
  log "SUMMARY: rebuilt=$REBUILD_OK — all native modules now compatible"
fi
