#!/usr/bin/env bash
# install.sh — One-shot setup for ~/.smartclaw/ (jleechanorg/smartclaw)
#
# Usage (post-clone):
#   bash install.sh
#
# What it does:
#   1. Installs Hermes Agent (hermes-agent) if not present
#   2. Creates ~/.smartclaw/hermes/ and ~/.smartclaw/hermes_prod/ runtime dirs
#   3. Installs systemd services (Linux) or LaunchAgents (macOS)
#   4. Sets up symlinks and config
#
# Prerequisites:
#   - hermes CLI via pip or hermes-agent install.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS="$REPO_ROOT/scripts"

# --- OS detection ---
case "$(uname -s)" in
  Darwin) OS="macos" ;;
  Linux)  OS="linux" ;;
  *) echo "Unsupported OS: $(uname -s)" >&2; exit 1 ;;
esac
echo "=== Smartclaw Install ==="
echo "Repo root: $REPO_ROOT"
echo "OS: $OS"
echo ""

# ============================================================================
# 1. Hermes Agent Installation
# ============================================================================
echo "--- Checking Hermes Agent ---"

HERMES_BIN="${HERMES_BIN:-hermes}"
HERMES_VENV_BIN="$HOME/.hermes/hermes-agent/venv/bin/hermes"

if [[ -x "$HERMES_VENV_BIN" ]]; then
  echo "✓ Hermes already installed at $HERMES_VENV_BIN"
elif command -v hermes >/dev/null 2>&1; then
  echo "✓ Hermes found in PATH"
else
  echo "  Installing Hermes Agent..."
  curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash
  # Symlink hermes to ~/.local/bin/
  if [[ ! -L "$HOME/.local/bin/hermes" ]] && [[ -x "$HERMES_VENV_BIN" ]]; then
    ln -sf "$HERMES_VENV_BIN" "$HOME/.local/bin/hermes"
    echo "✓ Symlinked hermes to ~/.local/bin/"
  fi
fi

# ============================================================================
# 2. Smartclaw Directory Structure
# ============================================================================
echo ""
echo "--- Setting up ~/.smartclaw directories ---"

mkdir -p "$HOME/.hermes_prod"/{skills,memories,sessions,logs,cron}

  # Staging: use default Hermes runtime (~/.hermes/)
HERMES_STAGING_HOME="${HERMES_STAGING_HOME:-$HOME/.hermes}"

# Prod: ensure ~/.hermes_prod exists (standard Hermes prod dir)
if [[ ! -d "$HOME/.hermes_prod" ]]; then
  mkdir -p "$HOME/.hermes_prod"/{skills,memories,sessions,logs,cron}
  echo "✓ Created ~/.hermes_prod/ runtime directories"
fi

echo "✓ ~/.smartclaw/hermes/       → staging runtime (symlink to ~/.hermes/)"
echo "✓ ~/.hermes_prod/       → prod runtime (default Hermes prod dir)"

# ============================================================================
# 3. Hermes Gateway Services (systemd on Linux, launchd on macOS)
# ============================================================================
echo ""
echo "--- Installing Hermes gateway services ---"

if [[ "$OS" == "linux" ]]; then
  echo "Installing systemd services (Linux)..."

  # Staging service
  sudo tee /etc/systemd/system/smartclaw-hermes-staging.service > /dev/null <<EOF
[Unit]
Description=Hermes Agent Gateway (Staging)
After=network.target

[Service]
Type=simple
User=$(whoami)
Group=$(id -gn)
Environment=HERMES_HOME=$HOME/.hermes
ExecStart=$HOME/.local/bin/hermes gateway run
Restart=on-failure
RestartSec=5
StandardOut=append:$HOME/.hermes/logs/gateway.log
StandardError=append:$HOME/.hermes/logs/gateway.err.log
WorkingDirectory=$HOME

[Install]
WantedBy=multi-user.target
EOF
  sudo systemctl daemon-reload
  sudo systemctl enable --now smartclaw-hermes-staging.service 2>/dev/null || \
    sudo systemctl enable smartclaw-hermes-staging.service
  echo "✓ smartclaw-hermes-staging.service installed"

  # Prod service
  sudo tee /etc/systemd/system/smartclaw-hermes-prod.service > /dev/null <<EOF
[Unit]
Description=Hermes Agent Gateway (Prod)
After=network.target

[Service]
Type=simple
User=$(whoami)
Group=$(id -gn)
Environment=HERMES_HOME=$HOME/.hermes_prod
ExecStart=$HOME/.local/bin/hermes gateway run
Restart=on-failure
RestartSec=5
StandardOut=append:$HOME/.hermes_prod/logs/gateway.log
StandardError=append:$HOME/.hermes_prod/logs/gateway.err.log
WorkingDirectory=$HOME

[Install]
WantedBy=multi-user.target
EOF
  sudo systemctl daemon-reload
  sudo systemctl enable --now smartclaw-hermes-prod.service 2>/dev/null || \
    sudo systemctl enable smartclaw-hermes-prod.service
  echo "✓ smartclaw-hermes-prod.service installed"

  echo ""
  echo "Manage with:"
  echo "  sudo systemctl start  smartclaw-hermes-staging.service"
  echo "  sudo systemctl stop   smartclaw-hermes-staging.service"
  echo "  sudo systemctl status smartclaw-hermes-staging.service"

else
  echo "Installing LaunchAgents (macOS)..."

  HERMES_STAGING_PLIST="$REPO_DIR/launchd/smartclaw.hermes-staging.plist.template"
  HERMES_PROD_PLIST="$REPO_DIR/launchd/smartclaw.hermes-prod.plist.template"

  if [[ -f "$HERMES_STAGING_PLIST" ]]; then
    sed -e "s|@HERMES_STAGING_HOME@|$HOME/.hermes|g" \
        -e "s|@HOME@|$HOME|g" \
        -e "s|@HERMES_BIN@|$HOME/.local/bin/hermes|g" \
        "$HERMES_STAGING_PLIST" > "$HOME/Library/LaunchAgents/ai.smartclaw.hermes-staging.plist"
    launchctl bootout "gui/$(id -u)/ai.smartclaw.hermes-staging" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/ai.smartclaw.hermes-staging.plist"
    echo "✓ ai.smartclaw.hermes-staging installed"
  fi

  if [[ -f "$HERMES_PROD_PLIST" ]]; then
    sed -e "s|@HERMES_PROD_HOME@|$HOME/.hermes_prod|g" \
        -e "s|@HOME@|$HOME|g" \
        -e "s|@HERMES_BIN@|$HOME/.local/bin/hermes|g" \
        "$HERMES_PROD_PLIST" > "$HOME/Library/LaunchAgents/ai.smartclaw.hermes.prod.plist"
    launchctl bootout "gui/$(id -u)/ai.smartclaw.hermes.prod" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/ai.smartclaw.hermes.prod.plist"
    echo "✓ ai.smartclaw.hermes.prod installed"
  fi

  echo ""
  echo "Manage with:"
  echo "  launchctl start gui/\$(id -u)/ai.smartclaw.hermes-staging"
  echo "  launchctl stop  gui/\$(id -u)/ai.smartclaw.hermes-staging"
fi

# ============================================================================
# 4. Agent Orchestrator Config
# ============================================================================
echo ""
echo "--- Copying agent-orchestrator.yaml ---"
REPO_YAML="$REPO_ROOT/agent-orchestrator.yaml"
AO_DOTFILE="$HOME/.agent-orchestrator.yaml"
if [[ -f "$REPO_YAML" ]]; then
  cp "$REPO_YAML" "$AO_DOTFILE"
  echo "✓ Copied: agent-orchestrator.yaml -> ~/.agent-orchestrator.yaml"
fi

# ============================================================================
# 5. Verify Hermes
# ============================================================================
echo ""
echo "--- Verifying Hermes installation ---"
if HERMES_HOME="$HOME/.hermes" hermes status >/dev/null 2>&1; then
  echo "✓ Hermes is functional"
  HERMES_HOME="$HOME/.hermes" hermes status 2>&1 | head -20
else
  echo "⚠ Hermes may need tokens configured — run hermes status to check"
fi

echo ""
echo "=== Install complete ==="
echo ""
echo "Next steps:"
echo "  1. Configure Slack tokens in ~/.hermes/.env (staging)"
echo "  2. Configure Slack tokens in ~/.hermes_prod/.env (prod)"
echo "  3. HERMES_HOME=\$HOME/.hermes hermes chat  # test staging"
