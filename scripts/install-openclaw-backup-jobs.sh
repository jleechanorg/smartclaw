#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPTS="$REPO_ROOT/scripts"
RUNNER="$SCRIPTS/run-openclaw-backup.sh"
WATCHDOG="$SCRIPTS/backup-watchdog.sh"
PLIST_TEMPLATE="$SCRIPTS/openclaw-backup.plist.template"
LAUNCHD_DIR="$HOME/Library/LaunchAgents"
PLIST_DST="$LAUNCHD_DIR/com.smartclaw.backup.plist"
CRON_MARKER="# OpenClaw 4h backup for ~/.smartclaw"
WATCHDOG_MARKER="# OpenClaw backup watchdog (hourly)"
DROPBOX_MARKER="# OpenClaw Dropbox backup (4h, offset :10)"
DROPBOX_BACKUP="$SCRIPTS/dropbox-openclaw-backup.sh"

mkdir -p "$LAUNCHD_DIR"
mkdir -p "$HOME/Library/Logs/openclaw-backup"

if [[ ! -x "$RUNNER" ]]; then
  echo "Runner script not executable: $RUNNER" >&2
  exit 1
fi
if [[ ! -x "$WATCHDOG" ]]; then
  echo "Watchdog script not executable: $WATCHDOG" >&2
  exit 1
fi

# ---------- launchd ----------
# Generate plist with machine-specific paths from template
sed -e "s|@REPO_ROOT@|$REPO_ROOT|g" \
    -e "s|@HOME@|$HOME|g" \
  "$PLIST_TEMPLATE" > "$PLIST_DST"

BOOTSTRAP_ERR_FILE="$(mktemp)"
if ! launchctl bootstrap gui/$(id -u) "$PLIST_DST" 2>"$BOOTSTRAP_ERR_FILE"; then
  # If service already exists, unload/reload.
  if [[ -s "$BOOTSTRAP_ERR_FILE" ]]; then
    cat "$BOOTSTRAP_ERR_FILE" >&2
  fi
  launchctl bootout gui/$(id -u) "$PLIST_DST" 2>/dev/null || true
  launchctl bootstrap gui/$(id -u) "$PLIST_DST"
fi
rm -f "$BOOTSTRAP_ERR_FILE"
launchctl enable gui/$(id -u)/com.smartclaw.backup
launchctl kickstart -k gui/$(id -u)/com.smartclaw.backup

echo "Installed launchd job at $PLIST_DST"

# ---------- legacy cron cleanup ----------
if command -v crontab >/dev/null 2>&1; then
  CRON_BEFORE="$(mktemp)"
  CRON_AFTER="$(mktemp)"
  crontab -l > "$CRON_BEFORE" 2>/dev/null || true

  if [[ -s "$CRON_BEFORE" ]]; then
    grep -Fv "$CRON_MARKER" "$CRON_BEFORE" \
      | grep -Fv "$RUNNER" \
      | grep -Fv "$WATCHDOG_MARKER" \
      | grep -Fv "$WATCHDOG" \
      | grep -Fv "$DROPBOX_MARKER" \
      | grep -Fv "$DROPBOX_BACKUP" \
      > "$CRON_AFTER" || true
    if ! cmp -s "$CRON_BEFORE" "$CRON_AFTER"; then
      crontab "$CRON_AFTER"
      echo "Removed legacy OpenClaw backup entries from system crontab."
    fi
  fi

  rm -f "$CRON_BEFORE" "$CRON_AFTER"
fi

echo "Done. OpenClaw backup scheduling now uses launchd only."
