#!/usr/bin/env bash
# Backup qdrant_storage/ to Dropbox so it survives WAL corruption or Docker loss.
# Run nightly via cron or launchd. Keeps last 7 daily backups.
#
# Source: ~/.smartclaw/scripts/backup-qdrant-to-dropbox.sh
# Dest:   ~/Dropbox/local/qdrant-backups/YYYY-MM-DD/

set -euo pipefail

SRC="${HOME}/.smartclaw/qdrant_storage"
DEST_ROOT="${HOME}/Dropbox/local/qdrant-backups"
DATE=$(date +%Y-%m-%d)
DEST="${DEST_ROOT}/${DATE}"
KEEP_DAYS=7

if [[ ! -d "$SRC" ]]; then
  echo "ERROR: $SRC not found — is Docker container running?" >&2
  exit 1
fi

mkdir -p "$DEST_ROOT"

# Incremental copy using rsync (fast if little changed)
rsync -a --delete "$SRC/" "$DEST/"

echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) backed up $SRC → $DEST"

# Prune backups older than KEEP_DAYS (macOS-compatible)
mapfile -t ALL_BACKUPS < <(find "$DEST_ROOT" -maxdepth 1 -type d -name "????-??-??" | sort)
TOTAL=${#ALL_BACKUPS[@]}
if (( TOTAL > KEEP_DAYS )); then
  REMOVE=$(( TOTAL - KEEP_DAYS ))
  for ((i=0; i<REMOVE; i++)); do
    rm -rf "${ALL_BACKUPS[$i]}"
  done
fi

echo "Kept last ${KEEP_DAYS} backups in ${DEST_ROOT}:"
ls "$DEST_ROOT"
