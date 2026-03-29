#!/usr/bin/env bash
set -euo pipefail

# SmartClaw Installation Script
# Quick setup for the SmartClaw prototype
#
# Usage:
#   ./install.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$SCRIPT_DIR"

echo "=== SmartClaw Installation ==="
echo "Repository: $REPO_ROOT"
echo "⚠️  This is a WORK IN PROGRESS prototype - not for production use"
echo

# Check prerequisites
echo "[1/3] Checking prerequisites..."

if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 is required but not installed" >&2
    exit 1
fi
echo "  ✓ python3: $(python3 --version)"

if ! command -v git >/dev/null 2>&1; then
    echo "ERROR: git is required but not installed" >&2
    exit 1
fi
echo "  ✓ git: $(git --version)"

if command -v rsync >/dev/null 2>&1; then
    echo "  ✓ rsync: available"
else
    echo "  ⚠ rsync not found (optional - needed for backups)"
fi

echo

# Check environment
echo "[2/3] Checking environment..."

if [[ -f "$REPO_ROOT/.env" ]]; then
    echo "  ✓ .env file found"
elif [[ -f "$HOME/.smartclaw.env" ]]; then
    echo "  ✓ Found ~/.smartclaw.env"
    echo "  → Linking to repository..."
    ln -sf "$HOME/.smartclaw.env" "$REPO_ROOT/.env"
else
    echo "  ⚠ No .env file found"
    echo "  → Creating example .env..."
    cp "$REPO_ROOT/.env.example" "$REPO_ROOT/.env" 2>/dev/null || \
        echo "OPENCLAW_SLACK_BOT_TOKEN=xoxb-example" > "$REPO_ROOT/.env"
    echo "  ! Please edit .env with your credentials"
fi

echo

# Installation complete
echo "[3/3] Installation complete!"
echo

echo "Next steps:"
echo "  1. Edit .env with your Slack/GitHub tokens"
echo "  2. Review README.md for configuration"
echo "  3. Run: source ~/.bashrc  # or ~/.zshrc"
echo

echo "For help, see:"
echo "  - README.md"
echo "  - https://github.com/jleechanorg/smartclaw"
echo

# Verify the .env is gitignored
if [[ -f "$REPO_ROOT/.env" ]] && ! grep -q "^\.env$" "$REPO_ROOT/.gitignore" 2>/dev/null; then
    echo "WARNING: .env is not in .gitignore - secrets may be committed!"
    echo "Add '.env' to .gitignore before committing"
fi

echo "✓ SmartClaw installation complete"
