#!/usr/bin/env bash
set -euo pipefail
SRC="$HOME/.smartclaw"
DROPBOX="${1:-$HOME/Library/CloudStorage/Dropbox}"
DST="$DROPBOX/openclaw_backup/latest"
LOG="$HOME/Library/Logs/openclaw-backup/dropbox-backup.log"
mkdir -p "$(dirname "$LOG")" "$DST"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting openclaw Dropbox backup" >> "$LOG"

rsync -a --delete \
  --exclude='.smartclaw-backups' \
  --exclude='.git' \
  --exclude='.DS_Store' \
  --exclude='workspace' \
  --exclude='workspace-*' \
  --exclude='smartclaw' \
  --exclude='credentials/whatsapp' \
  --exclude='*.lock' \
  --exclude='extensions/*/node_modules' \
  "$SRC/" "$DST/" >> "$LOG" 2>&1 \
  && echo "[$(date '+%Y-%m-%d %H:%M:%S')] Done." >> "$LOG" \
  || echo "[$(date '+%Y-%m-%d %H:%M:%S')] FAILED (exit $?)" >> "$LOG"
