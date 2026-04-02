#!/usr/bin/env bash
# install-claude-memory-sync.sh — Install the Claude memory sync launchd job
#
# Usage: ./scripts/install-claude-memory-sync.sh [--uninstall]
#
# Installs:
#   - ai.smartclaw.claude-memory-sync (every-15-min sync of Claude Code memory)
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LAUNCHD_DIR="$HOME/Library/LaunchAgents"

UNINSTALL_FLAG="${1:-}"

# Use inherited environment only — no dotfile sourcing for determinism.
# Export OPENCLAW_EXTRA_PATH before invoking this script if a custom path is needed.
: "${OPENCLAW_EXTRA_PATH:=}"

install_plist() {
  local src="$1"
  local label
  label=$(basename "$src" .plist.template)
  local dst="$LAUNCHD_DIR/$label.plist"

  if [[ "$UNINSTALL_FLAG" == "--uninstall" ]]; then
    launchctl bootout "gui/$(id -u)" "$dst" 2>/dev/null || true
    rm -f "$dst"
    echo "  $label uninstalled"
    return
  fi

  mkdir -p "$LAUNCHD_DIR"
  mkdir -p "$HOME/.smartclaw/logs"

  # Normalize OPENCLAW_EXTRA_PATH: ensure trailing colon if non-empty
  local extra_path_prefix=""
  if [[ -n "${OPENCLAW_EXTRA_PATH:-}" ]]; then
    extra_path_prefix="${OPENCLAW_EXTRA_PATH%:}:"
  fi

  # Substitute placeholders
  sed \
    -e "s|@HOME@|$HOME|g" \
    -e "s|@REPO_ROOT@|$REPO_ROOT|g" \
    -e "s|@OPENCLAW_EXTRA_PATH@|$extra_path_prefix|g" \
    "$src" > "$dst"

  # Reload if already running
  launchctl bootout "gui/$(id -u)" "$dst" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$dst"
  echo "  $label installed"
}

echo "=== Claude Memory Sync Service ==="
echo ""

PLIST_TEMPLATE="$REPO_ROOT/launchd/ai.smartclaw.claude-memory-sync.plist.template"
if [[ -f "$PLIST_TEMPLATE" ]]; then
  install_plist "$PLIST_TEMPLATE"
else
  echo "ERROR: Template not found: $PLIST_TEMPLATE"
  exit 1
fi

echo ""
echo "Done."
