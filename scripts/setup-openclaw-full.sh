#!/usr/bin/env bash
set -euo pipefail

# OpenClaw Full Setup Script
# Sets up OpenClaw with automated backups on a new machine
#
# Usage:
#   ./scripts/setup-openclaw-full.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "=== OpenClaw Full Setup ==="
echo "Repository: $REPO_ROOT"
echo

# Check if we're in the right location
if [[ ! -f "$REPO_ROOT/scripts/setup-openclaw-full.sh" ]]; then
    echo "ERROR: Must run from openclaw repository root" >&2
    exit 1
fi

# Step 1: Check prerequisites
echo "[1/4] Checking prerequisites..."
if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 is required but not installed" >&2
    exit 1
fi

if ! command -v git >/dev/null 2>&1; then
    echo "ERROR: git is required but not installed" >&2
    exit 1
fi

echo "  ✓ python3 found: $(python3 --version)"
echo "  ✓ git found: $(git --version)"
echo

# Step 2: Detect if this repo should be placed in ~/.openclaw/workspace/
echo "[2/4] Detecting installation location..."

# Check if we're already in ~/.openclaw/workspace/openclaw
if [[ "$REPO_ROOT" == "$HOME/.openclaw/workspace/openclaw" ]]; then
    echo "  ✓ Already in ~/.openclaw/workspace/openclaw"
    OPENCLAW_REPO="$REPO_ROOT"
elif [[ -d "$HOME/.openclaw/workspace/openclaw" ]]; then
    echo "  ✓ Found existing ~/.openclaw/workspace/openclaw"
    OPENCLAW_REPO="$HOME/.openclaw/workspace/openclaw"
    echo "  ! Using existing installation, will copy scripts there"
else
    echo "  → Creating ~/.openclaw/workspace/openclaw"
    mkdir -p "$HOME/.openclaw/workspace"

    # Ask user if they want to move or copy
    read -p "  Copy (c) or Move (m) this repo to ~/.openclaw/workspace/openclaw? [c/m]: " choice
    case "$choice" in
        m|M)
            echo "  → Moving repository..."
            mv "$REPO_ROOT" "$HOME/.openclaw/workspace/openclaw"
            OPENCLAW_REPO="$HOME/.openclaw/workspace/openclaw"
            cd "$OPENCLAW_REPO"
            ;;
        c|C|*)
            echo "  → Copying repository..."
            cp -R "$REPO_ROOT" "$HOME/.openclaw/workspace/openclaw"
            OPENCLAW_REPO="$HOME/.openclaw/workspace/openclaw"
            ;;
    esac
fi

echo "  OpenClaw repo: $OPENCLAW_REPO"
echo

# Step 3: Copy scripts to openclaw repo if needed
echo "[3/4] Setting up backup scripts..."
if [[ "$REPO_ROOT" != "$OPENCLAW_REPO" ]]; then
    echo "  → Copying scripts to $OPENCLAW_REPO"
    cp -v "$REPO_ROOT"/scripts/*backup* "$OPENCLAW_REPO/scripts/" || true
    cp -v "$REPO_ROOT"/scripts/run-openclaw-backup.sh "$OPENCLAW_REPO/scripts/" || true
    cp -v "$REPO_ROOT"/docs/openclaw-backup-jobs.md "$OPENCLAW_REPO/docs/" || true
fi

# Make scripts executable
chmod +x "$OPENCLAW_REPO"/scripts/*.sh

echo "  ✓ Backup scripts ready"
echo

# Step 4: Install backup jobs
echo "[4/4] Installing backup jobs (launchd only)..."
cd "$OPENCLAW_REPO"
"$OPENCLAW_REPO/scripts/install-openclaw-backup-jobs.sh"

echo
echo "=== Setup Complete! ==="
echo
echo "OpenClaw is now configured with automated backups:"
echo "  • Launchd: Every 4 hours"
echo "  • System crontab: not used for OpenClaw backup automation"
echo "  • Backups: $OPENCLAW_REPO/.openclaw-backups/"
echo
echo "To test the backup:"
echo "  cd $OPENCLAW_REPO"
echo "  ./scripts/run-openclaw-backup.sh"
echo
echo "To view logs:"
echo "  tail -f ~/Library/Logs/openclaw-backup/openclaw-backup.log"
echo
