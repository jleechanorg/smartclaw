#!/usr/bin/env bash
# install-ao-manager.sh — Install the unified AO manager launchd plist.
#
# Creates ~/Library/LaunchAgents/ai.agento.manager.plist from the template,
# then bootstrap it. ao-manager.sh reads projects from agent-orchestrator.yaml
# so no hardcoded project list in the plist.
#
# Usage:
#   ./scripts/install-ao-manager.sh     # install + start
#   ./scripts/install-ao-manager.sh --uninstall  # stop + remove
#
set -euo pipefail

# Canonical openclaw home — always use ~/.openclaw, resolved at runtime
OPENCLAW_HOME="$(python3 -c 'import os; print(os.path.expanduser("~/.openclaw"))')"
LAUNCHD_DIR="$HOME/Library/LaunchAgents"
PLIST_TEMPLATE="$OPENCLAW_HOME/launchd/ai.agento-manager.plist.template"
PLIST_NAME="ai.agento.manager.plist"
PLIST_PATH="$LAUNCHD_DIR/$PLIST_NAME"
LABEL="ai.agento.manager"

UNINSTALL=false
[[ "${1:-}" == "--uninstall" ]] && UNINSTALL=true

# ── helpers ──────────────────────────────────────────────────────────────────

_esc_sed() {
  printf '%s' "$1" | sed 's/\\/\\\\/g; s/&/\\&/g; s/|/\\|/g; s/\//\\\//g'
}

_install() {
  # Detect node (required for plist substitution)
  _detect_node_bin_dir() {
    local nvm_current="$HOME/.nvm/versions/node/current/bin"
    if [[ -x "$nvm_current/node" ]]; then echo "$nvm_current"; return 0; fi
    local p; p="$(command -v node 2>/dev/null || true)"
    if [[ -n "$p" && -x "$p" ]]; then dirname "$p"; return 0; fi
    return 1
  }
  local node_bin_dir
  node_bin_dir="$(_detect_node_bin_dir)" || {
    echo "ERROR: Node.js not found" >&2
    exit 1
  }

  mkdir -p "$LAUNCHD_DIR" "$HOME/.openclaw/logs"

  # Stop existing manager if running
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true

  # Generate plist from template
  # Use Homebrew bash explicitly to ensure Bash 4+ features (declare -A, mapfile)
  # work on Apple Silicon where /usr/bin/bash is Bash 3.2.
  local homebrew_bash="/opt/homebrew/bin/bash"
  if [[ ! -x "$homebrew_bash" ]]; then homebrew_bash="/usr/local/bin/bash"; fi
  if [[ ! -x "$homebrew_bash" ]]; then homebrew_bash="/bin/bash"; fi

  sed \
    -e "s|@HOME@|$(_esc_sed "$HOME")|g" \
    -e "s|@NODE_BIN_DIR@|$(_esc_sed "$node_bin_dir")|g" \
    -e "s|@HOMEBREW_BASH@|$(_esc_sed "$homebrew_bash")|g" \
    "$PLIST_TEMPLATE" > "$PLIST_PATH"

  launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"

  echo "✓ $PLIST_NAME installed and loaded"
  echo ""
  echo "Status:"
  echo "  launchctl print gui/\$(id -u)/$LABEL"
  echo ""
  echo "Logs:"
  echo "  tail -f ~/.openclaw/logs/agento-manager.log   # launchd stdout/stderr"
  echo "  tail -f /tmp/ao-manager.log                   # ao-manager internal log"
  echo ""
  echo "Manager commands:"
  echo "  ao-manager.sh --status    # health check all components"
  echo "  ao-manager.sh --once      # start once without monitor loop"
  echo ""
  echo "launchctl stop  gui/\$(id -u)/$LABEL   # stop"
  echo "launchctl start gui/\$(id -u)/$LABEL   # start"
}

_uninstall() {
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$PLIST_PATH"
  echo "✓ $PLIST_NAME uninstalled"
}

# ── dispatch ──────────────────────────────────────────────────────────────────

if $UNINSTALL; then
  echo "Uninstalling AO manager..."
  _uninstall
else
  if [[ ! -f "$PLIST_TEMPLATE" ]]; then
    echo "ERROR: template not found: $PLIST_TEMPLATE" >&2
    exit 1
  fi
  echo "Installing AO manager..."
  _install
fi
