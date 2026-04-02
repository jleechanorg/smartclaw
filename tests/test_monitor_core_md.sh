#!/bin/bash
# test_monitor_core_md.sh - Tests for run_core_md_probe() in monitor-agent.sh
#
# Tests the core markdown file health check that validates openclaw's
# required policy/identity files exist and are non-empty (not broken symlinks).
#
# Each test:
#   1. Creates a temp directory simulating an openclaw home
#   2. Sources the shared probe library (single source of truth)
#   3. Runs the probe and checks the resulting RC and SUMMARY

set -uo pipefail

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

PASSED=0
FAILED=0

log_pass() { echo -e "${GREEN}✓ PASS${NC}: $1"; ((PASSED++)); }
log_fail() { echo -e "${RED}✗ FAIL${NC}: $1"; ((FAILED++)); }
log_info() { echo -e "${YELLOW}ℹ INFO${NC}: $1"; }

# ---------------------------------------------------------------------------
# Source the shared probe library — the same lib/core-md-probe.sh used by
# monitor-agent.sh. This eliminates drift between test and production logic.
# ---------------------------------------------------------------------------
# shellcheck source=lib/core-md-probe.sh
source "$(dirname "$0")/../lib/core-md-probe.sh"

# Thin wrapper: runs _core_md_probe() and captures output to a temp file.
# This mirrors how monitor-agent.sh calls run_core_md_probe() → _core_md_probe().
probe_run() {
  _core_md_probe
}

# Parse probe_run output
probe_parse() {
  local result_file="$1"
  RC=$(awk '/^RC=/ {sub(/^RC=/,""); print}' "$result_file")
  SUMMARY=$(awk '/^SUMMARY=/ {sub(/^SUMMARY=/,""); print}' "$result_file")
}

# ---------------------------------------------------------------------------
# Test 1: All files present and non-empty → RC=0
# ---------------------------------------------------------------------------
test_all_present() {
  log_info "Test: all files present and non-empty → RC=0"

  local oc_dir result_file
  oc_dir=$(mktemp -d "/tmp/test_monitor_core_md.XXXXXX")
  result_file=$(mktemp "/tmp/test_probe_rc.XXXXXX")

  for f in SOUL TOOLS USER IDENTITY HEARTBEAT AGENTS MEMORY; do
    echo "# $f content" > "$oc_dir/${f}.md"
  done
  mkdir -p "$oc_dir/workspace"
  for f in SOUL TOOLS USER IDENTITY HEARTBEAT AGENTS MEMORY; do
    echo "# workspace/$f" > "$oc_dir/workspace/${f}.md"
  done

  OPENCLAW_MONITOR_OC_DIR="$oc_dir" probe_run > "$result_file"
  probe_parse "$result_file"

  rm -rf "$oc_dir" "$result_file"

  if [ "$RC" -eq 0 ] && [ "$SUMMARY" = "all core md files OK" ]; then
    log_pass "all files present → RC=0"
  else
    log_fail "all files present → RC=$RC summary=$SUMMARY"
  fi
}

# ---------------------------------------------------------------------------
# Test 2: Missing file → RC=1
# ---------------------------------------------------------------------------
test_missing_file() {
  log_info "Test: missing file → RC=1"

  local oc_dir result_file
  oc_dir=$(mktemp -d "/tmp/test_monitor_core_md.XXXXXX")
  result_file=$(mktemp "/tmp/test_probe_rc.XXXXXX")
  echo "# Soul" > "$oc_dir/SOUL.md"

  OPENCLAW_MONITOR_OC_DIR="$oc_dir" probe_run > "$result_file"
  probe_parse "$result_file"

  rm -rf "$oc_dir" "$result_file"

  if [ "$RC" -eq 1 ] && [[ "$SUMMARY" == *"TOOLS.md"* ]]; then
    log_pass "missing file → RC=1"
  else
    log_fail "missing file → RC=$RC summary=$SUMMARY (expected RC=1 with TOOLS.md)"
  fi
}

# ---------------------------------------------------------------------------
# Test 3: Broken symlink → RC=1 (distinct from missing file)
# ---------------------------------------------------------------------------
test_broken_symlink() {
  log_info "Test: broken symlink → RC=1"

  local oc_dir real_dir result_file
  oc_dir=$(mktemp -d "/tmp/test_monitor_core_md.XXXXXX")
  real_dir=$(mktemp -d "/tmp/test_probe_real.XXXXXX")
  result_file=$(mktemp "/tmp/test_probe_rc.XXXXXX")

  for f in SOUL TOOLS USER IDENTITY HEARTBEAT AGENTS MEMORY; do
    echo "# $f" > "$oc_dir/${f}.md"
  done
  mkdir -p "$oc_dir/workspace"
  for f in SOUL TOOLS USER IDENTITY HEARTBEAT AGENTS MEMORY; do
    echo "# $f" > "$oc_dir/workspace/${f}.md"
  done
  rm -f "$oc_dir/USER.md"
  ln -s "$real_dir/does_not_exist.md" "$oc_dir/USER.md"

  OPENCLAW_MONITOR_OC_DIR="$oc_dir" probe_run > "$result_file"
  probe_parse "$result_file"

  rm -rf "$oc_dir" "$real_dir" "$result_file"

  # RC=1 AND SUMMARY mentions "broken symlinks" (not "missing files")
  if [ "$RC" -eq 1 ] && [[ "$SUMMARY" == *"broken symlinks"* ]]; then
    log_pass "broken symlink → RC=1 summary=broken symlinks"
  else
    log_fail "broken symlink → RC=$RC summary=$SUMMARY (expected RC=1 summary='broken symlinks: USER.md')"
  fi
}

# ---------------------------------------------------------------------------
# Test 4: Empty file → RC=2 (warning, not critical)
# ---------------------------------------------------------------------------
test_empty_file() {
  log_info "Test: empty file → RC=2"

  local oc_dir result_file
  oc_dir=$(mktemp -d "/tmp/test_monitor_core_md.XXXXXX")
  result_file=$(mktemp "/tmp/test_probe_rc.XXXXXX")

  for f in SOUL TOOLS USER IDENTITY HEARTBEAT AGENTS MEMORY; do
    echo "# $f" > "$oc_dir/${f}.md"
  done
  mkdir -p "$oc_dir/workspace"
  for f in SOUL TOOLS USER IDENTITY HEARTBEAT AGENTS MEMORY; do
    echo "# $f" > "$oc_dir/workspace/${f}.md"
  done
  : > "$oc_dir/HEARTBEAT.md"

  OPENCLAW_MONITOR_OC_DIR="$oc_dir" probe_run > "$result_file"
  probe_parse "$result_file"

  rm -rf "$oc_dir" "$result_file"

  if [ "$RC" -eq 2 ] && [[ "$SUMMARY" == *"HEARTBEAT.md"* ]]; then
    log_pass "empty file → RC=2"
  else
    log_fail "empty file → RC=$RC summary=$SUMMARY (expected RC=2 with HEARTBEAT.md)"
  fi
}

# ---------------------------------------------------------------------------
# Test 5: Disabled via env var → RC=0 (early return, not a failure)
# ---------------------------------------------------------------------------
test_disabled() {
  log_info "Test: disabled via env var → RC=0 (skipped)"

  local oc_dir result_file
  oc_dir=$(mktemp -d "/tmp/test_monitor_core_md.XXXXXX")
  result_file=$(mktemp "/tmp/test_probe_rc.XXXXXX")

  OPENCLAW_MONITOR_OC_DIR="$oc_dir" \
    OPENCLAW_MONITOR_CORE_MD_ENABLE=0 \
    probe_run > "$result_file"
  probe_parse "$result_file"

  rm -rf "$oc_dir" "$result_file"

  if [ "$RC" -eq 0 ] && [ "$SUMMARY" = "core md check disabled" ]; then
    log_pass "disabled → RC=0 summary=disabled"
  else
    log_fail "disabled → RC=$RC summary=$SUMMARY (expected RC=0 summary='core md check disabled')"
  fi
}

# ---------------------------------------------------------------------------
# Test 6: Custom OC_DIR via env var is used (SOUL.md found in custom dir)
# ---------------------------------------------------------------------------
test_custom_dir() {
  log_info "Test: custom OC_DIR used (SOUL.md found in custom dir)"

  local custom_dir result_file
  custom_dir=$(mktemp -d "/tmp/test_monitor_core_md.XXXXXX")
  result_file=$(mktemp "/tmp/test_probe_rc.XXXXXX")
  echo "# Custom SOUL" > "$custom_dir/SOUL.md"

  # With only SOUL.md present in custom_dir, RC=1 (15 files missing),
  # but SOUL.md should NOT be in the missing list (it was found in custom_dir).
  OPENCLAW_MONITOR_OC_DIR="$custom_dir" probe_run > "$result_file"
  probe_parse "$result_file"

  rm -rf "$custom_dir" "$result_file"

  # Check SOUL.md is NOT in the missing list.
  # Extract tokens from "missing files: A B C" and look for exact match.
  local missing_list="${SUMMARY#missing files:}"
  local soul_found=0
  for tok in $missing_list; do
    [ "$tok" = "SOUL.md" ] && soul_found=1 && break
  done

  if [ "$RC" -eq 1 ] && [ "$soul_found" -eq 0 ]; then
    log_pass "custom OC_DIR → respected (SOUL.md found in custom dir, others missing)"
  else
    log_fail "custom OC_DIR → RC=$RC summary=$SUMMARY (expected SOUL.md NOT in missing list)"
  fi
}

# ---------------------------------------------------------------------------
# Test 7: Both primary and workspace/ layers checked
# ---------------------------------------------------------------------------
test_both_layers_checked() {
  log_info "Test: primary file missing but workspace/ copy exists → RC=1"

  local oc_dir result_file
  oc_dir=$(mktemp -d "/tmp/test_monitor_core_md.XXXXXX")
  result_file=$(mktemp "/tmp/test_probe_rc.XXXXXX")
  mkdir -p "$oc_dir/workspace"
  for f in SOUL TOOLS USER IDENTITY HEARTBEAT AGENTS MEMORY; do
    echo "# $f" > "$oc_dir/workspace/${f}.md"
  done

  OPENCLAW_MONITOR_OC_DIR="$oc_dir" probe_run > "$result_file"
  probe_parse "$result_file"

  rm -rf "$oc_dir" "$result_file"

  if [ "$RC" -eq 1 ] && [[ "$SUMMARY" == *"SOUL.md"* ]]; then
    log_pass "primary missing but workspace exists → RC=1 (both layers checked)"
  else
    log_fail "primary missing → RC=$RC summary=$SUMMARY (expected RC=1)"
  fi
}

# ---------------------------------------------------------------------------
# Test 8: Wrong symlink target → RC=1 (points to wrong workspace file)
# ---------------------------------------------------------------------------
test_wrong_symlink_target() {
  log_info "Test: wrong symlink target → RC=1"

  local oc_dir result_file
  oc_dir=$(mktemp -d "/tmp/test_monitor_core_md.XXXXXX")
  result_file=$(mktemp "/tmp/test_probe_rc.XXXXXX")

  mkdir -p "$oc_dir/workspace"
  # Create all workspace files
  for f in SOUL TOOLS USER IDENTITY HEARTBEAT AGENTS MEMORY; do
    echo "# $f" > "$oc_dir/workspace/${f}.md"
  done
  # Create primary files as symlinks pointing to the WRONG workspace file
  rm -f "$oc_dir/SOUL.md"
  ln -s "$oc_dir/workspace/USER.md" "$oc_dir/SOUL.md"
  # All other primary files also as symlinks (so none are treated as "missing")
  for f in TOOLS USER IDENTITY HEARTBEAT AGENTS MEMORY; do
    rm -f "$oc_dir/${f}.md"
    ln -s "$oc_dir/workspace/${f}.md" "$oc_dir/${f}.md"
  done

  OPENCLAW_MONITOR_OC_DIR="$oc_dir" probe_run > "$result_file"
  probe_parse "$result_file"

  rm -rf "$oc_dir" "$result_file"

  # RC=1 AND SUMMARY mentions "wrong symlink targets"
  if [ "$RC" -eq 1 ] && [[ "$SUMMARY" == *"wrong symlink targets"* ]]; then
    log_pass "wrong symlink target → RC=1 summary=wrong symlink targets"
  else
    log_fail "wrong symlink target → RC=$RC summary=$SUMMARY (expected RC=1 summary='wrong symlink targets: ...')"
  fi
}

# ---------------------------------------------------------------------------
# Test 9: Out-of-tree symlink target → RC=1, path redacted in summary
# ---------------------------------------------------------------------------
test_out_of_tree_symlink() {
  log_info "Test: out-of-tree symlink → RC=1, path redacted"

  local oc_dir real_dir result_file
  oc_dir=$(mktemp -d "/tmp/test_monitor_core_md.XXXXXX")
  real_dir=$(mktemp -d "/tmp/test_probe_real.XXXXXX")
  result_file=$(mktemp "/tmp/test_probe_rc.XXXXXX")

  mkdir -p "$oc_dir/workspace"
  for f in SOUL TOOLS USER IDENTITY HEARTBEAT AGENTS MEMORY; do
    echo "# $f" > "$oc_dir/workspace/${f}.md"
  done
  # Create the out-of-tree target file so it's NOT broken, just wrong
  echo "# wrong file" > "$real_dir/totally_wrong.md"
  rm -f "$oc_dir/SOUL.md"
  ln -s "$real_dir/totally_wrong.md" "$oc_dir/SOUL.md"
  for f in TOOLS USER IDENTITY HEARTBEAT AGENTS MEMORY; do
    rm -f "$oc_dir/${f}.md"
    ln -s "$oc_dir/workspace/${f}.md" "$oc_dir/${f}.md"
  done

  OPENCLAW_MONITOR_OC_DIR="$oc_dir" probe_run > "$result_file"
  probe_parse "$result_file"

  rm -rf "$oc_dir" "$real_dir" "$result_file"

  # RC=1 AND SUMMARY contains "wrong symlink targets" but NOT an absolute path
  if [ "$RC" -eq 1 ] && [[ "$SUMMARY" == *"wrong symlink targets"* ]]; then
    if [[ "$SUMMARY" != *"$real_dir"* ]] && [[ "$SUMMARY" != *"/tmp/"* ]]; then
      log_pass "out-of-tree → RC=1, path redacted"
    else
      log_fail "out-of-tree → RC=$RC summary=$SUMMARY (absolute path leaked into summary)"
    fi
  else
    log_fail "out-of-tree → RC=$RC summary=$SUMMARY (expected RC=1 with 'wrong symlink targets')"
  fi
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
  echo "========================================"
  echo "Core MD Probe Tests (monitor-agent.sh)"
  echo "========================================"
  echo ""

  test_all_present
  test_missing_file
  test_broken_symlink
  test_empty_file
  test_disabled
  test_custom_dir
  test_both_layers_checked
  test_wrong_symlink_target
  test_out_of_tree_symlink

  echo ""
  echo "========================================"
  echo "Results: $PASSED passed, $FAILED failed"
  echo "========================================"

  if [[ $FAILED -gt 0 ]]; then
    exit 1
  fi
  exit 0
}

main
