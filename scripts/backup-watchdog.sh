#!/usr/bin/env bash
# backup-watchdog.sh — fires Slack webhook + email if newest backup is >6h old.
#
# Intended to run hourly via cron: 0 * * * * /path/to/backup-watchdog.sh
# Install via: scripts/install-openclaw-backup-jobs.sh
#
# Required env vars for alerts (set in ~/.profile or cron env):
#   SLACK_BACKUP_WEBHOOK  — Slack incoming webhook URL
#   EMAIL_USER            — SMTP sender address
#   EMAIL_PASS            — SMTP password
#   BACKUP_EMAIL          — alert recipient
#
# Always exits 0 so cron does not spam logs on normal operation.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SNAP_BASE="$REPO_ROOT/.openclaw-backups"
MAX_AGE_SECONDS=21600  # 6 hours
SMTP_SERVER="smtp.gmail.com"
SMTP_PORT="587"
LOG_DIR="${HOME}/Library/Logs/openclaw-backup"
LOG_FILE="$LOG_DIR/backup-watchdog.log"
mkdir -p "$LOG_DIR"

TS() { date +"%Y-%m-%d %H:%M:%S"; }

# ---------------------------------------------------------------------------
# Find newest snapshot timestamp.
# Uses latest/ symlink if present; otherwise scans directory names.
# ---------------------------------------------------------------------------
newest_backup_ts() {
  local latest_link="$SNAP_BASE/latest"
  if [[ -L "$latest_link" && -d "$latest_link" ]]; then
    # Resolve symlink target name (e.g. "20260303_120000")
    basename "$(readlink "$latest_link")"
    return
  fi
  # Fallback: newest directory sorted lexicographically (YYYYMMDD_HHMMSS sorts correctly)
  ls -1 "$SNAP_BASE" 2>/dev/null | grep -E '^[0-9]{8}_[0-9]{6}$' | sort | tail -1
}

if [[ ! -d "$SNAP_BASE" ]]; then
  echo "[$(TS)] WARN: Snapshot base not found: $SNAP_BASE" >> "$LOG_FILE"
  exit 0
fi

NEWEST_TS="$(newest_backup_ts)"

if [[ -z "$NEWEST_TS" ]]; then
  SUBJECT="ALERT: OpenClaw backup — no snapshots found on $(hostname)"
  BODY="No backup snapshots found under $SNAP_BASE on $(hostname) at $(TS)."
  AGE_HOURS="unknown"
else
  # Parse timestamp: YYYYMMDD_HHMMSS
  YEAR="${NEWEST_TS:0:4}"
  MON="${NEWEST_TS:4:2}"
  DAY="${NEWEST_TS:6:2}"
  HOUR="${NEWEST_TS:9:2}"
  MIN="${NEWEST_TS:11:2}"
  SEC="${NEWEST_TS:13:2}"

  BACKUP_EPOCH="$(date -j -f "%Y%m%d %H%M%S" "${YEAR}${MON}${DAY} ${HOUR}${MIN}${SEC}" "+%s" 2>/dev/null \
    || date -d "${YEAR}-${MON}-${DAY} ${HOUR}:${MIN}:${SEC}" "+%s" 2>/dev/null \
    || echo 0)"

  NOW_EPOCH="$(date +%s)"
  AGE_SECONDS=$(( NOW_EPOCH - BACKUP_EPOCH ))
  AGE_HOURS=$(( AGE_SECONDS / 3600 ))

  if (( AGE_SECONDS <= MAX_AGE_SECONDS )); then
    echo "[$(TS)] OK: newest backup $NEWEST_TS is ${AGE_HOURS}h old (within 6h threshold)" >> "$LOG_FILE"
    exit 0
  fi

  SUBJECT="ALERT: OpenClaw backup stale — last backup ${AGE_HOURS}h ago on $(hostname)"
  BODY="OpenClaw backup watchdog alert.

Host: $(hostname)
Newest snapshot: $NEWEST_TS
Age: ${AGE_HOURS}h (threshold: 6h)
Snapshot base: $SNAP_BASE
Time: $(TS)

All three schedulers (launchd, openclaw-cron, system cron) appear to have missed.
Check:
  tail -50 ~/Library/Logs/openclaw-backup/openclaw-backup.log
  launchctl list | grep openclaw
  crontab -l"
fi

echo "[$(TS)] ALERT: backup stale (${AGE_HOURS}h). Firing alerts." >> "$LOG_FILE"

# ---------------------------------------------------------------------------
# Alert 1: Slack webhook
# ---------------------------------------------------------------------------
if [[ -n "${SLACK_BACKUP_WEBHOOK:-}" ]]; then
  # Use python3 to build valid JSON — shell string interpolation produces
  # invalid JSON when SUBJECT/BODY contain newlines or special characters.
  SLACK_PAYLOAD="$(python3 -c "
import json, sys
subject = sys.argv[1]
body = sys.argv[2]
print(json.dumps({'text': subject + '\n\n' + body}))
" "$SUBJECT" "$BODY")"
  if curl -sf -X POST -H 'Content-type: application/json' \
       --data "$SLACK_PAYLOAD" "$SLACK_BACKUP_WEBHOOK" >/dev/null 2>&1; then
    echo "[$(TS)] Slack alert sent." >> "$LOG_FILE"
  else
    echo "[$(TS)] Slack alert failed (curl non-zero)." >> "$LOG_FILE"
  fi
else
  echo "[$(TS)] SLACK_BACKUP_WEBHOOK not set; skipping Slack alert." >> "$LOG_FILE"
fi

# ---------------------------------------------------------------------------
# Alert 2: Email via SMTP
# ---------------------------------------------------------------------------
if [[ -n "${EMAIL_USER:-}" && -n "${EMAIL_PASS:-}" && -n "${BACKUP_EMAIL:-}" ]]; then
  ALERT_TS="$(date +"%Y%m%d_%H%M%S")"
  REPORT_FILE="$LOG_DIR/backup-watchdog-alert-${ALERT_TS}.txt"
  cat <<EOF > "$REPORT_FILE"
Subject: $SUBJECT
From: ${EMAIL_USER}
To: ${BACKUP_EMAIL}

${BODY}
EOF
  if command -v curl >/dev/null 2>&1; then
    if curl --url "smtp://$SMTP_SERVER:$SMTP_PORT" \
         --ssl-reqd \
         --mail-from "$EMAIL_USER" \
         --mail-rcpt "$BACKUP_EMAIL" \
         --user "$EMAIL_USER:$EMAIL_PASS" \
         --upload-file "$REPORT_FILE" >/dev/null 2>&1; then
      echo "[$(TS)] Email alert sent to $BACKUP_EMAIL." >> "$LOG_FILE"
    else
      echo "[$(TS)] Email alert failed (curl non-zero)." >> "$LOG_FILE"
    fi
  fi
else
  echo "[$(TS)] Email vars not set; skipping email alert." >> "$LOG_FILE"
fi

exit 0
