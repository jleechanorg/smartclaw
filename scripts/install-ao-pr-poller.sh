#!/usr/bin/env bash
# Install the ao-pr-poller launchd job.
#
# The ao-pr-poller polls GitHub for open PRs that need agent orchestration
# and handles idle session detection/reaping.
#
# Usage:
#   ./scripts/install-ao-pr-poller.sh [--uninstall]
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LAUNCHD_DIR="$HOME/Library/LaunchAgents"
PLIST_SOURCE="$REPO_ROOT/launchd/ai.ao-pr-poller.plist"
PLIST_NAME="ai.ao-pr-poller.plist"

UNINSTALL=false
[[ "${1:-}" == "--uninstall" ]] && UNINSTALL=true

if $UNINSTALL; then
  echo "Uninstalling ao-pr-poller..."
  launchctl bootout "gui/$UID" "$LAUNCHD_DIR/$PLIST_NAME" 2>/dev/null || true
  rm -f "$LAUNCHD_DIR/$PLIST_NAME"
  echo "  ✓ $PLIST_NAME uninstalled"
  exit 0
fi

# Validate prerequisites
if [[ ! -f "$PLIST_SOURCE" ]]; then
  echo "ERROR: plist not found at $PLIST_SOURCE" >&2
  exit 1
fi

POLLER_SCRIPT="$REPO_ROOT/scripts/ao-pr-poller.sh"
if [[ ! -x "$POLLER_SCRIPT" ]]; then
  echo "ERROR: ao-pr-poller.sh not found or not executable at $POLLER_SCRIPT" >&2
  exit 1
fi

# Validate GITHUB_TOKEN
GITHUB_TOKEN_VAL="${GITHUB_TOKEN:-}"
if [[ -z "$GITHUB_TOKEN_VAL" ]]; then
  GITHUB_TOKEN_VAL=$(grep -E '^export GITHUB_TOKEN=' ~/.bashrc 2>/dev/null | tail -1 | sed "s/^export GITHUB_TOKEN=[\"']*//;s/[\"']*\$//" || true)
fi
if [[ -z "$GITHUB_TOKEN_VAL" ]]; then
  echo "ERROR: GITHUB_TOKEN not set. Export it or add to ~/.bashrc." >&2
  exit 1
fi

mkdir -p "$LAUNCHD_DIR"

# Copy plist to LaunchAgents
cp "$PLIST_SOURCE" "$LAUNCHD_DIR/$PLIST_NAME"

# Unload existing job if any, then load new one
launchctl bootout "gui/$UID" "$LAUNCHD_DIR/$PLIST_NAME" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$LAUNCHD_DIR/$PLIST_NAME"

echo "Installing ao-pr-poller..."
echo "  ✓ $PLIST_NAME installed and loaded"
echo ""
echo "Logs:"
echo "  tail -f /tmp/ao-pr-poller-launchd.log"
echo ""
echo "Status:"
echo "  launchctl list | grep ao-pr-poller"
