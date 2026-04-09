#!/usr/bin/env bash
# Gateway pre-flight check — run before any upgrade, doctor --fix, or restart
#
# OpenClaw's gateway status / service audit reads the LaunchAgent plist as UTF-8
# and matches XML tags (RunAtLoad, KeepAlive, EnvironmentVariables/PATH). Apple
# *binary* plists fail those checks and produce false warnings ("missing"
# RunAtLoad/KeepAlive/PATH) even when launchd has the correct job. Fix: keep the
# on-disk plist in XML form: plutil -convert xml1 ~/Library/LaunchAgents/ai.smartclaw.gateway.plist
#
# Usage: bash gateway-preflight.sh [--fix]
set -uo pipefail

FIX_MODE="${1:-}"
ERRORS=0

echo "=== Gateway Pre-flight Check ==="
echo ""

# 1. Check for competing plists
PLIST_COUNT=$(ls ~/Library/LaunchAgents/*openclaw*gateway* ~/Library/LaunchAgents/*com.smartclaw.gateway* 2>/dev/null | sort -u | wc -l | tr -d ' ')
echo "[1] Gateway plists: $PLIST_COUNT"
if [ "$PLIST_COUNT" -gt 1 ]; then
  echo "  FAIL: Multiple gateway plists detected:"
  ls -1 ~/Library/LaunchAgents/*openclaw*gateway* ~/Library/LaunchAgents/*com.smartclaw.gateway* 2>/dev/null | sort -u
  if [ "$FIX_MODE" = "--fix" ]; then
    echo "  FIX: Keeping ai.smartclaw.gateway, removing others"
    for plist in ~/Library/LaunchAgents/*openclaw*gateway*; do
      label=$(defaults read "$plist" Label 2>/dev/null || true)
      if [ "$label" != "ai.smartclaw.gateway" ] && [ -n "$label" ]; then
        launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
        rm -f "$plist"
        echo "  Removed: $plist ($label)"
      fi
    done
  else
    ERRORS=$((ERRORS + 1))
  fi
elif [ "$PLIST_COUNT" -eq 0 ]; then
  echo "  WARN: No gateway plist found"
else
  echo "  OK"
fi

# 2. Check ThrottleInterval (must be >= 10 to prevent restart storms)
ACTIVE_PLIST=$(ls ~/Library/LaunchAgents/ai.smartclaw.gateway.plist 2>/dev/null | head -1)
if [ -n "$ACTIVE_PLIST" ]; then
  THROTTLE=$(defaults read "$ACTIVE_PLIST" ThrottleInterval 2>/dev/null || echo "30")
  echo "[2] ThrottleInterval: $THROTTLE"
  if [ "$THROTTLE" -lt 10 ]; then
    echo "  FAIL: ThrottleInterval=$THROTTLE is too low (causes restart storms)"
    if [ "$FIX_MODE" = "--fix" ]; then
      defaults write "$ACTIVE_PLIST" ThrottleInterval -int 30
      echo "  FIX: Set ThrottleInterval to 30"
    else
      ERRORS=$((ERRORS + 1))
    fi
  else
    echo "  OK"
  fi
fi

# 2b. Plist must be XML (not binary) so openclaw gateway status / doctor can parse it
if [ -n "$ACTIVE_PLIST" ] && [ -f "$ACTIVE_PLIST" ]; then
  _gw_plist_binary=0
  _hdr=$(head -c 8 "$ACTIVE_PLIST" 2>/dev/null || true)
  if [[ "$_hdr" == bplist* ]]; then
    _gw_plist_binary=1
  elif command -v file >/dev/null 2>&1 && file "$ACTIVE_PLIST" 2>/dev/null | grep -qi "binary property list"; then
    _gw_plist_binary=1
  fi
  echo "[2b] Gateway plist encoding (XML required for openclaw CLI audit):"
  if [ "$_gw_plist_binary" -eq 1 ]; then
    echo "  FAIL: plist is binary; openclaw treats RunAtLoad/KeepAlive/PATH as absent"
    if [ "$FIX_MODE" = "--fix" ]; then
      plutil -convert xml1 "$ACTIVE_PLIST" || { echo "  plutil failed"; ERRORS=$((ERRORS + 1)); }
      echo "  FIX: converted to XML (plutil -convert xml1)"
      _domain="gui/$(id -u)"
      if launchctl kickstart -k "${_domain}/ai.smartclaw.gateway" >/dev/null 2>&1; then
        echo "  FIX: launchctl kickstart -k ${_domain}/ai.smartclaw.gateway (reload from disk)"
      else
        echo "  WARN: kickstart failed; when convenient: launchctl kickstart -k ${_domain}/ai.smartclaw.gateway"
      fi
    else
      ERRORS=$((ERRORS + 1))
    fi
  else
    echo "  OK (XML/text plist)"
  fi
fi

# 3. Check for multiple gateway processes
GW_PIDS=$(pgrep -f 'openclaw-gateway\|openclaw.*gateway.*18789' 2>/dev/null | wc -l | tr -d ' ')
echo "[3] Gateway processes: $GW_PIDS"
if [ "$GW_PIDS" -gt 1 ]; then
  echo "  FAIL: Multiple gateway processes detected"
  ps aux | grep -E 'openclaw.*(gateway|18789)' | grep -v grep
  ERRORS=$((ERRORS + 1))
else
  echo "  OK"
fi

# 4. Check config JSON validity
echo -n "[4] Config JSON: "
if python3 -c "import json; json.load(open('$HOME/.smartclaw/openclaw.json'))" 2>/dev/null; then
  echo "valid"
else
  echo "INVALID"
  ERRORS=$((ERRORS + 1))
fi

# 5. Check critical config keys
echo "[5] Critical config keys:"
python3 -c "
import json, sys
with open('$HOME/.smartclaw/openclaw.json') as f:
    d = json.load(f)
checks = {
    'channels.slack.appToken': d.get('channels',{}).get('slack',{}).get('appToken'),
    'channels.slack.botToken': d.get('channels',{}).get('slack',{}).get('botToken'),
    'gateway.auth.token': d.get('gateway',{}).get('auth',{}).get('token'),
}
errors = 0
for path, val in checks.items():
    status = 'OK' if val else 'MISSING'
    print(f'  {path}: {status}')
    if not val: errors += 1
sys.exit(errors)
" 2>/dev/null || ERRORS=$((ERRORS + $?))

# 5b. Check consensus config version vs running binary (version mismatch → AJV stack overflow)
# If meta.lastTouchedVersion in consensus config is NEWER than the running binary,
# openclaw enters infinite console.error → loadConfig recursion → RangeError crash.
CONSENSUS_CFG="$HOME/.smartclaw-consensus/openclaw.json"
echo -n "[5b] Consensus config version vs binary: "
if [ -f "$CONSENSUS_CFG" ]; then
  python3 -c "
import json, subprocess, sys, re

# Get running binary version
try:
    result = subprocess.run(['openclaw', '--version'], capture_output=True, text=True, timeout=5)
    bin_ver_raw = result.stdout.strip() + result.stderr.strip()
    # Extract YYYY.M.D pattern
    m = re.search(r'(\d{4}\.\d+\.\d+)', bin_ver_raw)
    bin_ver = m.group(1) if m else None
except:
    bin_ver = None

with open('$CONSENSUS_CFG') as f:
    d = json.load(f)
cfg_ver = d.get('meta', {}).get('lastTouchedVersion')

if not bin_ver:
    print('SKIP: cannot determine binary version')
    sys.exit(0)
if not cfg_ver:
    print('SKIP: consensus config has no meta.lastTouchedVersion')
    sys.exit(0)

# Compare: if cfg_ver > bin_ver (lexicographic works for YYYY.M.D dates)
if cfg_ver > bin_ver:
    print(f'MISMATCH: config={cfg_ver} > binary={bin_ver} — will cause AJV stack overflow')
    sys.exit(1)
else:
    print(f'OK (config={cfg_ver}, binary={bin_ver})')
    sys.exit(0)
" 2>/dev/null
  EXIT_CODE=$?
  if [ $EXIT_CODE -ne 0 ]; then
    if [ "$FIX_MODE" = "--fix" ]; then
      # Auto-correct: set meta.lastTouchedVersion to match binary
      python3 -c "
import json, subprocess, re
result = subprocess.run(['openclaw', '--version'], capture_output=True, text=True, timeout=5)
m = re.search(r'(\d{4}\.\d+\.\d+)', result.stdout + result.stderr)
if not m:
    print('  FIX SKIPPED: cannot determine binary version')
    exit(0)
bin_ver = m.group(1)
path = '$CONSENSUS_CFG'
with open(path) as f: d = json.load(f)
old_ver = d.get('meta', {}).get('lastTouchedVersion', 'unknown')
d.setdefault('meta', {})['lastTouchedVersion'] = bin_ver
with open(path, 'w') as f: json.dump(d, f, indent=2)
print(f'  FIX: updated meta.lastTouchedVersion {old_ver} -> {bin_ver}')
" 2>/dev/null || echo "  FIX FAILED: could not update consensus config"
    else
      ERRORS=$((ERRORS + 1))
    fi
  fi
else
  echo "  SKIP: $CONSENSUS_CFG not found"
fi

# 6. Check native modules
echo -n "[6] Native modules: "
NODE=$(${HOME}/.nvm/versions/node/v22.22.0/bin/node --version 2>/dev/null || echo "missing")
if [ -f "$HOME/.openclaw/extensions/openclaw-mem0/node_modules/better-sqlite3/build/Release/better_sqlite3.node" ]; then
  if ${HOME}/.nvm/versions/node/v22.22.0/bin/node -e "require('$HOME/.openclaw/extensions/openclaw-mem0/node_modules/better-sqlite3')" 2>/dev/null; then
    echo "OK (Node $NODE)"
  else
    echo "MISMATCH (needs rebuild)"
    if [ "$FIX_MODE" = "--fix" ]; then
      cd "$HOME/.openclaw/extensions/openclaw-mem0"
      rm -rf node_modules/better-sqlite3/build node_modules/better-sqlite3/prebuilds
      ${HOME}/.nvm/versions/node/v22.22.0/bin/npx node-gyp rebuild --directory=node_modules/better-sqlite3 2>/dev/null
      echo "  FIX: Rebuilt better-sqlite3"
    else
      ERRORS=$((ERRORS + 1))
    fi
  fi
else
  echo "not installed (OK if mem0 not used)"
fi

# 7. @agentclientprotocol/sdk version baseline
# Records current sdk version; compare against upgrade target via validate_sdk_compatibility()
echo "[7] @agentclientprotocol/sdk baseline:"
_oc_ver=$(${HOME}/.nvm/versions/node/v22.22.0/bin/npm list -g openclaw --depth=0 --json 2>/dev/null \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('dependencies',{}).get('openclaw',{}).get('version','unknown'))" 2>/dev/null \
  || echo "unknown")
if [ "$_oc_ver" = "unknown" ]; then
  echo "  SKIP: openclaw not found in global npm (cannot check SDK version)"
else
  _sdk_ver=$(${HOME}/.nvm/versions/node/v22.22.0/bin/npm view "openclaw@${_oc_ver}" dependencies 2>/dev/null \
    | grep -i agentclientprotocol | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)
  if [ -n "${_sdk_ver:-}" ]; then
    echo "$_sdk_ver" > "$HOME/.openclaw/.current-sdk-version"
    echo "  OK: openclaw=$_oc_ver, @agentclientprotocol/sdk=$_sdk_ver (stored .current-sdk-version)"
  else
    echo "  WARN: openclaw=$_oc_ver but could not resolve @agentclientprotocol/sdk version"
  fi
fi

# validate_sdk_compatibility — call before any openclaw upgrade
# Usage: validate_sdk_compatibility <new-openclaw-version> [--ack-minor-jump]
# Returns 0 if compatible, 1 if breaking jump detected
validate_sdk_compatibility() {
  local new_version="${1:-}"
  local ack_flag="${2:-}"
  if [ -z "$new_version" ]; then
    echo "Usage: validate_sdk_compatibility <new-openclaw-version> [--ack-minor-jump]"
    return 1
  fi
  local cur_sdk="unknown"
  # Migration fallback: read from legacy path if new path doesn't exist
  if [ -f "$HOME/.openclaw/.current-sdk-version" ]; then
    cur_sdk=$(tr -d '[:space:]' < "$HOME/.openclaw/.current-sdk-version")
  elif [ -f "$HOME/.smartclaw/.current-sdk-version" ]; then
    cur_sdk=$(tr -d '[:space:]' < "$HOME/.smartclaw/.current-sdk-version")
  fi
  local new_sdk
  new_sdk=$(${HOME}/.nvm/versions/node/v22.22.0/bin/npm view "openclaw@${new_version}" dependencies 2>/dev/null \
    | grep -i agentclientprotocol | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)
  new_sdk="${new_sdk:-unknown}"

  echo "=== SDK Compatibility Check: openclaw@${new_version} ==="
  echo "  Current @agentclientprotocol/sdk : $cur_sdk"
  echo "  Target  @agentclientprotocol/sdk : $new_sdk"

  if [ "$new_sdk" = "unknown" ] || [ "$cur_sdk" = "unknown" ]; then
    echo "  WARN: Could not determine one or both SDK versions — check manually before upgrading"
    return 0
  fi

  local cur_major cur_minor new_major new_minor
  cur_major=$(echo "$cur_sdk" | cut -d. -f1); cur_minor=$(echo "$cur_sdk" | cut -d. -f2)
  new_major=$(echo "$new_sdk" | cut -d. -f1); new_minor=$(echo "$new_sdk" | cut -d. -f2)
  local major_diff=$(( new_major - cur_major ))
  local minor_diff=$(( new_minor - cur_minor ))

  if [ "$major_diff" -gt 0 ]; then
    echo "  FAIL: Major SDK version jump ($cur_sdk → $new_sdk) — protocol mismatch WILL cause errors"
    echo "  Incident: openclaw 2026.3.24→2026.3.28 (SDK 0.16→0.17) produced 367 ws-stream 500s/day"
    return 1
  elif [ "$cur_major" -eq 0 ] && [ "$minor_diff" -gt 0 ] && [ "$ack_flag" != "--ack-minor-jump" ]; then
    echo "  FAIL: 0.x minor jump ($cur_sdk → $new_sdk) — likely breaking (0.x = no stability guarantee)"
    echo "  Incident: openclaw 2026.3.24→2026.3.28 (SDK 0.16→0.17) produced 367 ws-stream 500s/day"
    echo "  Pass --ack-minor-jump to override (risk accepted)"
    return 1
  else
    echo "  OK: SDK compatible ($cur_sdk → $new_sdk)"
    return 0
  fi
}
export -f validate_sdk_compatibility 2>/dev/null || true

# 8. checkCompatibility key location check
# Valid ONLY inside extensions.<name>.vectorStore — top-level gateway/agents.defaults crashes startup
echo "[8] checkCompatibility placement:"
python3 -c "
import json, sys
try:
    with open('$HOME/.smartclaw/openclaw.json') as f:
        d = json.load(f)
except Exception as e:
    print('  SKIP: cannot parse openclaw.json (' + str(e) + ')')
    sys.exit(0)

bad = []
# Top-level and top-level keys
if 'checkCompatibility' in d:
    bad.append('(top-level)')
if 'checkCompatibility' in d.get('gateway', {}):
    bad.append('gateway')
if 'checkCompatibility' in d.get('agents', {}).get('defaults', {}):
    bad.append('agents.defaults')
# Under extensions but not inside extensions.<name>.vectorStore
for name, ext_val in d.get('extensions', {}).items():
    if not isinstance(ext_val, dict):
        continue
    # Valid: extensions.<name>.vectorStore.checkCompatibility
    if 'checkCompatibility' in ext_val.get('vectorStore', {}):
        continue  # valid placement
    # Invalid: extensions.<name>.checkCompatibility directly
    if 'checkCompatibility' in ext_val:
        bad.append('extensions.' + name + '.checkCompatibility')

if bad:
    print('  FAIL: checkCompatibility found in invalid location(s): ' + ', '.join(bad))
    print('  Only valid inside extensions.<name>.vectorStore — other placements cause gateway crash on reload')
    sys.exit(1)
else:
    print('  OK')
" 2>/dev/null || ERRORS=$((ERRORS + 1))

# 9. NODE_MODULE_VERSION baseline tracking
# Gateway plist uses nvm Node 22 (MODULE_VERSION 127); mismatch causes silent mem0 failures
GATEWAY_NODE_BIN="${HOME}/.nvm/versions/node/v22.22.0/bin/node"
MODVER_BASELINE="$HOME/.openclaw/.gateway-node-version"
echo "[9] NODE_MODULE_VERSION baseline:"
if [ -x "$GATEWAY_NODE_BIN" ]; then
  _cur_modver=$("$GATEWAY_NODE_BIN" -e "process.stdout.write(String(process.versions.modules))" 2>/dev/null || echo "unknown")
  if [ -f "$MODVER_BASELINE" ]; then
    _stored_modver=$(cat "$MODVER_BASELINE" | tr -d '[:space:]')
    if [ "$_cur_modver" != "$_stored_modver" ]; then
      echo "  WARN: MODULE_VERSION changed — stored=$_stored_modver, current=$_cur_modver"
      echo "        better-sqlite3 compiled for wrong Node version — mem0 will fail silently"
      if [ "$FIX_MODE" = "--fix" ]; then
        echo "  FIX: Rebuilding better-sqlite3 for MODULE_VERSION $_cur_modver..."
        _rebuild_out=$(cd "$HOME/.openclaw/extensions/openclaw-mem0" \
          && ${HOME}/.nvm/versions/node/v22.22.0/bin/npm rebuild better-sqlite3 2>&1)
        _rebuild_rc=$?
        if [ "$_rebuild_rc" -eq 0 ]; then
          # Verify the rebuilt module actually loads before updating baseline
          if ${HOME}/.nvm/versions/node/v22.22.0/bin/node -e "require('$HOME/.openclaw/extensions/openclaw-mem0/node_modules/better-sqlite3')" 2>/dev/null; then
            echo "$_cur_modver" > "$MODVER_BASELINE"
            echo "  FIX: Rebuild OK — baseline updated to $_cur_modver"
          else
            echo "  FIX FAILED: rebuild succeeded but module still fails to load"
            ERRORS=$((ERRORS + 1))
          fi
        else
          echo "  FIX FAILED: rebuild failed (exit $_rebuild_rc)"
          echo "  Output: $_rebuild_out"
          ERRORS=$((ERRORS + 1))
        fi
      else
        echo "  Run with --fix to auto-rebuild, or:"
        echo "    npm rebuild better-sqlite3 --prefix ~/.openclaw/extensions/openclaw-mem0"
        ERRORS=$((ERRORS + 1))
      fi
    else
      echo "  OK (MODULE_VERSION $_cur_modver matches stored baseline)"
    fi
  else
    echo "$_cur_modver" > "$MODVER_BASELINE"
    echo "  OK (no baseline — recorded MODULE_VERSION $_cur_modver)"
  fi
else
  echo "  SKIP: $GATEWAY_NODE_BIN not found"
fi

# Summary
echo ""
if [ "$ERRORS" -gt 0 ]; then
  echo "PREFLIGHT FAILED: $ERRORS issue(s) found"
  echo "Run with --fix to auto-repair, or fix manually before proceeding"
  exit 1
else
  echo "PREFLIGHT PASSED: all checks OK"
  exit 0
fi
