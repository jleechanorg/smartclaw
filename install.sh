#!/usr/bin/env bash
# install.sh — One-shot setup for ~/.smartclaw/ (jleechanorg/smartclaw)
#
# Usage (post-clone):
#   bash ~/.smartclaw/install.sh
#
# What it does:
#   1. Recreates symlinks and copies config files needed at runtime
#   2. Installs all LaunchAgents / systemd units (gateway, monitor, startup-check, etc.)
#
# Prerequisites:
#   - openclaw.json must exist at ~/.smartclaw/openclaw.json with real tokens hardcoded
#     (create/update this local runtime file directly; it is gitignored)
#   - openclaw CLI must be in PATH

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS="$REPO_ROOT/scripts"

echo "=== OpenClaw Install ==="
echo "Repo root: $REPO_ROOT"
echo ""

# --- 1. Symlinks and config copies ---

# workspace-monitor skills symlink (gitignored in workspace-monitor/)
if [[ -d "$REPO_ROOT/workspace-monitor" ]]; then
  ln -sf "$REPO_ROOT/skills" "$REPO_ROOT/workspace-monitor/skills"
  echo "✓ Symlink: workspace-monitor/skills -> skills/"
fi

# Agent Orchestrator config
REPO_YAML="$REPO_ROOT/agent-orchestrator.yaml"
AO_DOTFILE="$HOME/.agent-orchestrator.yaml"
if [[ -f "$REPO_YAML" ]]; then
  cp "$REPO_YAML" "$AO_DOTFILE"
  echo "✓ Copied: agent-orchestrator.yaml -> ~/.agent-orchestrator.yaml"
fi

# --- 2. Verify openclaw.json has real tokens ---
if [[ ! -f "$REPO_ROOT/openclaw.json" ]]; then
  echo ""
  echo "ERROR: ~/.smartclaw/openclaw.json not found."
  echo "  Create openclaw.json with real tokens before running install."
  exit 1
fi

if python3 - "$REPO_ROOT/openclaw.json" <<'PY'
import json, sys
data = json.loads(open(sys.argv[1]).read())
def has_placeholder(obj):
    if isinstance(obj, str):
        return obj.startswith("${") and obj.endswith("}")
    if isinstance(obj, dict):
        return any(has_placeholder(v) for v in obj.values())
    if isinstance(obj, list):
        return any(has_placeholder(v) for v in obj)
    return False
if has_placeholder(data):
    print("ERROR: openclaw.json still contains \${PLACEHOLDER} values — fill in real tokens first.", file=__import__("sys").stderr)
    sys.exit(1)
PY
then
  echo "✓ openclaw.json: tokens appear to be real (no placeholders)"
else
  exit 1
fi

echo ""

# --- 3. Install Python orchestration package (local editable) ---
echo "--- Installing Python orchestration package ---"
if command -v python3 >/dev/null 2>&1; then
  if [[ -f "$REPO_ROOT/pyproject.toml" ]] || [[ -f "$REPO_ROOT/setup.py" ]]; then
    if python3 -m pip install -e "$REPO_ROOT" --quiet; then
      echo "✓ Python orchestration package installed (editable)"
    else
      echo "ERROR: pip install -e failed. Ensure python3 + pip are available." >&2
      exit 1
    fi
  else
    echo "  skipping: no pyproject.toml or setup.py found"
  fi
else
  echo "WARNING: python3 not found — orchestration modules won't be available"
fi

echo ""

# --- 4. Install LaunchAgents / systemd units ---
echo "--- Installing services ---"
"$SCRIPTS/install-all.sh"

echo ""
echo "=== Install complete ==="
echo ""
echo "Gateway token and all secrets are read directly from ~/.smartclaw/openclaw.json."
echo "Do NOT add tokens to plists or environment variables — openclaw.json is the"
echo "single source of truth."
