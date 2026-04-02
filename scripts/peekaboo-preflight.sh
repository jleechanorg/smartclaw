#!/usr/bin/env bash
# peekaboo-preflight.sh — Verify macOS environment is ready for Peekaboo UI automation.
# Usage: bash scripts/peekaboo-preflight.sh
# Exit 0 = all checks pass; non-zero = at least one failed.

set -euo pipefail

PASS=0
FAIL=0

check() {
  local label="$1"
  shift
  if "$@" >/dev/null 2>&1; then
    printf "  ✅  %s\n" "$label"
    PASS=$((PASS + 1))
  else
    printf "  ❌  %s\n" "$label"
    FAIL=$((FAIL + 1))
  fi
}

check_output() {
  # Same as check but prints captured output on success
  local label="$1"
  shift
  local out
  if out=$("$@" 2>&1); then
    printf "  ✅  %s — %s\n" "$label" "$out"
    PASS=$((PASS + 1))
  else
    printf "  ❌  %s\n" "$label"
    FAIL=$((FAIL + 1))
  fi
}

echo "═══════════════════════════════════════════"
echo "  Peekaboo Antigravity Preflight Check"
echo "═══════════════════════════════════════════"
echo ""

# 1. Peekaboo CLI
echo "1) Peekaboo CLI"
if command -v peekaboo >/dev/null 2>&1; then
  check_output "peekaboo found" peekaboo --version
else
  printf "  ❌  peekaboo not found\n"
  printf "      → brew install steipete/tap/peekaboo\n"
  FAIL=$((FAIL + 1))
fi
echo ""

# 2. macOS permissions
echo "2) macOS Permissions"
if command -v peekaboo >/dev/null 2>&1; then
  PERMS=$(peekaboo permissions --json 2>/dev/null || echo '{}')
  # Parse permission status using awk to handle multi-line JSON.
  # The JSON lists objects with "name" and "isGranted" fields.
  perm_granted() {
    echo "$PERMS" | awk -v perm="$1" '
      /"name"/ { found = 0; if ($0 ~ perm) found = 1 }
      /"isGranted"/ && found { if ($0 ~ /true/) exit 0; else exit 1 }
    '
  }
  # Check Accessibility
  if perm_granted "Accessibility"; then
    printf "  ✅  Accessibility granted\n"
    PASS=$((PASS + 1))
  else
    printf "  ❌  Accessibility not granted\n"
    printf "      → System Settings > Privacy & Security > Accessibility\n"
    FAIL=$((FAIL + 1))
  fi
  # Check Screen Recording
  if perm_granted "Screen Recording"; then
    printf "  ✅  Screen Recording granted\n"
    PASS=$((PASS + 1))
  else
    printf "  ❌  Screen Recording not granted\n"
    printf "      → System Settings > Privacy & Security > Screen Recording\n"
    FAIL=$((FAIL + 1))
  fi
else
  printf "  ⏭️  Skipped (peekaboo not installed)\n"
fi
echo ""

# 3. Antigravity app
echo "3) Antigravity IDE"
if mdfind "kMDItemDisplayName == 'Antigravity'" 2>/dev/null | head -1 | grep -q .; then
  printf "  ✅  Antigravity found\n"
  PASS=$((PASS + 1))
elif [ -d "/Applications/Antigravity.app" ]; then
  printf "  ✅  Antigravity found in /Applications\n"
  PASS=$((PASS + 1))
else
  printf "  ❌  Antigravity not found\n"
  FAIL=$((FAIL + 1))
fi
echo ""

# 4. PeekabooBridge socket
echo "4) PeekabooBridge"
SOCKET_PATH="$HOME/Library/Application Support/OpenClaw/PeekabooBridge.sock"
if [ -e "$SOCKET_PATH" ]; then
  printf "  ✅  Bridge socket exists at %s\n" "$SOCKET_PATH"
  PASS=$((PASS + 1))
else
  printf "  ❌  Bridge socket not found\n"
  printf "      → Ensure OpenClaw macOS app is running with PeekabooBridge enabled\n"
  FAIL=$((FAIL + 1))
fi
echo ""

# 5. Local skill file
echo "5) Local Peekaboo Skill"
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SKILL_PATH="$SCRIPT_DIR/openclaw-config/skills/peekaboo/SKILL.md"
if [ -f "$SKILL_PATH" ]; then
  printf "  ✅  Skill file exists at %s\n" "$SKILL_PATH"
  PASS=$((PASS + 1))
else
  printf "  ❌  Skill file missing\n"
  printf "      → Expected at openclaw-config/skills/peekaboo/SKILL.md\n"
  FAIL=$((FAIL + 1))
fi
echo ""

# Summary
echo "═══════════════════════════════════════════"
printf "  Results: %d passed, %d failed\n" "$PASS" "$FAIL"
echo "═══════════════════════════════════════════"

if [ "$FAIL" -gt 0 ]; then
  exit 1
fi
