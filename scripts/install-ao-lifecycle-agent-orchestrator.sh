#!/usr/bin/env bash
# Install the ao lifecycle-worker launchd job for the agent-orchestrator project.
#
# This runs `ao lifecycle-worker agent-orchestrator` which polls GitHub for open PRs
# in jleechanorg/agent-orchestrator and fires reactions (ci-failed, changes-requested,
# merge-conflicts, etc.) to spawn/resume agent sessions.
#
# Usage:
#   ./scripts/install-ao-lifecycle-agent-orchestrator.sh [--uninstall]
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LAUNCHD_DIR="$HOME/Library/LaunchAgents"
PLIST_TEMPLATE="$REPO_ROOT/launchd/com.agentorchestrator.lifecycle-agent-orchestrator.plist"
PLIST_NAME="com.agentorchestrator.lifecycle-agent-orchestrator.plist"
LABEL="com.agentorchestrator.lifecycle-agent-orchestrator"

UNINSTALL=false
[[ "${1:-}" == "--uninstall" ]] && UNINSTALL=true

if $UNINSTALL; then
  echo "Uninstalling ao lifecycle-worker (agent-orchestrator)..."
  launchctl bootout "gui/$(id -u)" "$LAUNCHD_DIR/$PLIST_NAME" 2>/dev/null || true
  rm -f "$LAUNCHD_DIR/$PLIST_NAME"
  echo "  ✓ $PLIST_NAME uninstalled"
  exit 0
fi

# Validate prerequisites
if [[ ! -f "$PLIST_TEMPLATE" ]]; then
  echo "ERROR: plist template not found at $PLIST_TEMPLATE" >&2
  exit 1
fi

AO_BIN="$HOME/bin/ao"
if [[ ! -x "$AO_BIN" ]]; then
  echo "ERROR: ao binary not found at $AO_BIN" >&2
  echo "  Install: npm i -g @agent-orchestrator/cli" >&2
  exit 1
fi

# Detect node path: prefer pinned nvm Node 22 (same as OpenClaw gateway; avoid Homebrew node ABI drift).
_detect_node_bin_dir() {
  local nvm_22="$HOME/.nvm/versions/node/v22.22.0/bin"
  if [[ -x "$nvm_22/node" ]]; then echo "$nvm_22"; return 0; fi
  local nvm_current="$HOME/.nvm/versions/node/current/bin"
  if [[ -x "$nvm_current/node" ]]; then echo "$nvm_current"; return 0; fi
  local p; p="$(command -v node 2>/dev/null || true)"
  if [[ -n "$p" && -x "$p" ]]; then dirname "$p"; return 0; fi
  return 1
}

NODE_BIN_DIR="$(_detect_node_bin_dir)" || {
  echo "ERROR: Node.js not found" >&2; exit 1
}
NODE_PATH="$NODE_BIN_DIR/node"

# Resolve GITHUB_TOKEN: env > ~/.bashrc
GITHUB_TOKEN_VAL="${GITHUB_TOKEN:-}"
if [[ -z "$GITHUB_TOKEN_VAL" ]]; then
  GITHUB_TOKEN_VAL=$(grep -E '^export GITHUB_TOKEN=' ~/.bashrc 2>/dev/null | tail -1 | sed "s/^export GITHUB_TOKEN=[\"']*//;s/[\"']*\$//" || true)
fi
if [[ -z "$GITHUB_TOKEN_VAL" ]]; then
  echo "ERROR: GITHUB_TOKEN not set. Export it or add to ~/.bashrc." >&2
  echo "  Without it, ao lifecycle-worker cannot poll GitHub for PR events." >&2
  exit 1
fi

_esc_sed() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/&/\\&/g; s/|/\\|/g'; }

mkdir -p "$LAUNCHD_DIR" "$HOME/.openclaw/logs"

# Install plist with placeholder substitution
launchctl bootout "gui/$(id -u)" "$LAUNCHD_DIR/$PLIST_NAME" 2>/dev/null || true
sed \
  -e "s|@HOME@|$(_esc_sed "$HOME")|g" \
  -e "s|@NODE_BIN_DIR@|$(_esc_sed "$NODE_BIN_DIR")|g" \
  -e "s|@NODE_PATH@|$(_esc_sed "$NODE_PATH")|g" \
  -e "s|@GITHUB_TOKEN@|$(_esc_sed "$GITHUB_TOKEN_VAL")|g" \
  "$PLIST_TEMPLATE" > "$LAUNCHD_DIR/$PLIST_NAME"

launchctl bootstrap "gui/$(id -u)" "$LAUNCHD_DIR/$PLIST_NAME"

echo "Installing ao lifecycle-worker (agent-orchestrator)..."
echo "  ✓ $PLIST_NAME installed and loaded"
echo ""
echo "Logs:"
echo "  tail -f ~/.openclaw/logs/ao-lifecycle-agent-orchestrator.log"
echo "  tail -f ~/.openclaw/logs/ao-lifecycle-agent-orchestrator.err.log"
echo ""
echo "Status:"
echo "  launchctl list | grep $LABEL"
