#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_SCRIPT="$SCRIPT_DIR/backup-openclaw-full.sh"
LOG_DIR="${HOME}/Library/Logs/openclaw-backup"
LOCK_FILE="$LOG_DIR/openclaw-backup.lock"
LOG_FILE="$LOG_DIR/openclaw-backup.log"
mkdir -p "$LOG_DIR"

SMTP_SERVER="smtp.gmail.com"
SMTP_PORT="587"
EMAIL_FROM="openclaw-backup@worldarchitect.ai"

TS() {
  date +"%Y-%m-%d %H:%M:%S"
}

acquire_lock() {
  if ! command -v flock >/dev/null 2>&1; then
    return 0
  fi

  exec 9>"$LOCK_FILE"
  if ! flock -n 9; then
    echo "[$(TS)] Backup skipped: another backup runner is already active" >> "$LOG_FILE"
    exit 0
  fi
}

send_failure_email() {
  local exit_code="$1"
  local reason="$2"
  local timestamp="$(date +"%Y-%m-%d %H:%M:%S %Z")"
  local failure_ts="$(date +"%Y%m%d_%H%M%S")"
  local report_file="$LOG_DIR/openclaw-backup-failure-${failure_ts}.txt"

  cat <<EOF > "$report_file"
Subject: ALERT: OpenClaw backup failed on $(hostname) (${timestamp})
From: $EMAIL_FROM
To: ${BACKUP_EMAIL:-backup-alerts@worldarchitect.ai}

OpenClaw backup wrapper failure.

Failure time: ${timestamp}
Exit code: ${exit_code}
Failure reason: ${reason}
Script: ${BACKUP_SCRIPT}
Host: $(hostname)

Recent output:
EOF
  tail -n 120 "$LOG_FILE" >> "$report_file" 2>/dev/null || true

  if [ -z "${EMAIL_USER:-}" ] || [ -z "${EMAIL_PASS:-}" ] || [ -z "${BACKUP_EMAIL:-}" ]; then
    echo "[$(TS)] Email config missing; cannot send failure alert."
    echo "[$(TS)] Failure report saved to: $report_file"
    return 0
  fi

  if command -v curl >/dev/null 2>&1; then
    if curl --url "smtp://$SMTP_SERVER:$SMTP_PORT" \
       --ssl-reqd \
       --mail-from "$EMAIL_USER" \
       --mail-rcpt "$BACKUP_EMAIL" \
       --user "$EMAIL_USER:$EMAIL_PASS" \
       --upload-file "$report_file" >/dev/null 2>&1; then
      echo "[$(TS)] Failure alert sent to ${BACKUP_EMAIL}."
      return 0
    fi
  fi

  local manual_dir="$SCRIPT_DIR/tmp/backup_alerts"
  mkdir -p "$manual_dir"
  cp "$report_file" "$manual_dir/openclaw_backup_failure_${failure_ts}.txt"
  echo "[$(TS)] Failure alert could not be sent; report saved to $manual_dir/openclaw_backup_failure_${failure_ts}.txt"
  return 0
}

acquire_lock

echo "[$(TS)] Starting ~/.openclaw backup" >> "$LOG_FILE"

if [ "${OPENCLAW_BACKUP_FORCE_FAILURE:-0}" = "1" ]; then
  echo "[$(TS)] Forced failure enabled for verification: OPENCLAW_BACKUP_FORCE_FAILURE=1" >> "$LOG_FILE"
  send_failure_email 1 "Forced failure test (OPENCLAW_BACKUP_FORCE_FAILURE=1)" >> "$LOG_FILE" 2>&1
  echo "[$(TS)] Backup failed (exit=1): Forced failure test (OPENCLAW_BACKUP_FORCE_FAILURE=1)"
  exit 1
fi

if "$BACKUP_SCRIPT" >> "$LOG_FILE" 2>&1; then
  echo "[$(TS)] Backup complete"
  echo "[$(TS)] Backup complete" >> "$LOG_FILE"
else
  backup_rc=$?
  reason="Backup command returned non-zero status"
  echo "[$(TS)] Backup failed (exit=$backup_rc): $reason"
  send_failure_email "$backup_rc" "$reason"
  exit "$backup_rc"
fi >> "$LOG_FILE"
