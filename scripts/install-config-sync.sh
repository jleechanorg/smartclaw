#!/usr/bin/env bash
# install-config-sync.sh — Install the config sync launchd job
#
# Usage: ./scripts/install-config-sync.sh [--uninstall]
#
# Installs:
#   - ai.smartclaw.config-sync (hourly sync of openclaw.json.redacted from live)
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LAUNCHD_DIR="$HOME/Library/LaunchAgents"

UNINSTALL_FLAG="${1:-}"

# Source profile to get environment variables
if [[ -f "$HOME/.profile" ]]; then
  source "$HOME/.profile"
fi

install_plist() {
  local src="$1"
  local label
  label=$(basename "$src" .plist.template)
  local dst="$LAUNCHD_DIR/$label.plist"

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

  if [[ "$UNINSTALL_FLAG" == "--uninstall" ]]; then
    launchctl bootout "gui/$(id -u)" "$dst" 2>/dev/null || true
    rm -f "$dst"
    echo "  ✓ $label uninstalled"
  else
    # Reload if already running
    launchctl bootout "gui/$(id -u)" "$dst" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$dst"
    echo "  ✓ $label installed"
  fi
}

echo "=== Config Sync Service ==="
echo ""

PLIST_TEMPLATE="$REPO_ROOT/launchd/ai.smartclaw.config-sync.plist.template"
if [[ -f "$PLIST_TEMPLATE" ]]; then
  install_plist "$PLIST_TEMPLATE"
else
  echo "ERROR: Template not found: $PLIST_TEMPLATE"
  exit 1
fi

echo ""
echo "Done."
