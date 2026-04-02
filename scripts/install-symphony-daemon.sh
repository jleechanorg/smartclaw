#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_ROOT="${SYMPHONY_DAEMON_RUNTIME:-$HOME/Library/Application Support/smartclaw/symphony_daemon}"
METADATA="$RUNTIME_ROOT/daemon_metadata.json"

if [[ "${1:-}" == "--uninstall" ]]; then
  if [[ -f "$METADATA" ]]; then
    LABEL="$(jq -r '.label' "$METADATA")"
    PLIST_PATH="$(jq -r '.plist_path' "$METADATA")"
    launchctl bootout "gui/$(id -u)" "$PLIST_PATH" 2>/dev/null || true
    rm -f "$PLIST_PATH"
    echo "Uninstalled $LABEL"
  else
    echo "No metadata found at $METADATA; nothing to uninstall"
  fi
  exit 0
fi

PYTHONPATH="$ROOT_DIR/src" python3 "$ROOT_DIR/scripts/setup-symphony-daemon.py"
