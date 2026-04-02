#!/usr/bin/env bash
# install-github-intake.sh — Install the GitHub Intake Daemon
#
# macOS: installs launchd plist (ai.smartclaw.github-intake)
# Linux: installs systemd user service + timer
#
# Usage: ./scripts/install-github-intake.sh [--uninstall]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LABEL="ai.smartclaw.github-intake"
UNINSTALL="${1:-}"

# Ensure logs and state dirs exist
mkdir -p "$HOME/.smartclaw/logs"
mkdir -p "$HOME/.smartclaw/state"

if [[ "$(uname)" == "Darwin" ]]; then
  # ── macOS: launchd ──
  PLIST_SRC="$REPO_ROOT/launchd/$LABEL.plist"
  PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"

  if [[ "$UNINSTALL" == "--uninstall" ]]; then
    echo "Uninstalling $LABEL (launchd)..."
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$PLIST_DST"
    echo "  Removed."
    exit 0
  fi

  if [[ ! -f "$PLIST_SRC" ]]; then
    echo "ERROR: plist not found at $PLIST_SRC"
    exit 1
  fi

  echo "Installing $LABEL (launchd)..."
  # Stop if running
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  # Copy plist
  cp "$PLIST_SRC" "$PLIST_DST"
  # Load
  launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"
  echo "  Installed and started."
  echo "  Logs: ~/.smartclaw/logs/github-intake.log"
  echo "  NOTE: Starts in DRY_RUN mode. Set INTAKE_DRY_RUN=0 in plist to enable live dispatch."

else
  # ── Linux: systemd user units ──
  SYSTEMD_DIR="$HOME/.config/systemd/user"
  SERVICE_SRC="$REPO_ROOT/systemd/ai-openclaw-github-intake.service"
  TIMER_SRC="$REPO_ROOT/systemd/ai-openclaw-github-intake.timer"

  if [[ "$UNINSTALL" == "--uninstall" ]]; then
    echo "Uninstalling $LABEL (systemd)..."
    systemctl --user stop ai-openclaw-github-intake.timer 2>/dev/null || true
    systemctl --user disable ai-openclaw-github-intake.timer 2>/dev/null || true
    rm -f "$SYSTEMD_DIR/ai-openclaw-github-intake.service"
    rm -f "$SYSTEMD_DIR/ai-openclaw-github-intake.timer"
    systemctl --user daemon-reload
    echo "  Removed."
    exit 0
  fi

  if [[ ! -f "$SERVICE_SRC" ]] || [[ ! -f "$TIMER_SRC" ]]; then
    echo "ERROR: systemd units not found in $REPO_ROOT/systemd/"
    exit 1
  fi

  echo "Installing $LABEL (systemd)..."
  mkdir -p "$SYSTEMD_DIR"
  cp "$SERVICE_SRC" "$SYSTEMD_DIR/"
  cp "$TIMER_SRC" "$SYSTEMD_DIR/"
  systemctl --user daemon-reload
  systemctl --user enable --now ai-openclaw-github-intake.timer
  echo "  Installed and started."
  echo "  Status: systemctl --user status ai-openclaw-github-intake.timer"
  echo "  NOTE: Starts in DRY_RUN mode. Edit service to set INTAKE_DRY_RUN=0 for live dispatch."
fi
