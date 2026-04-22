#!/usr/bin/env bash
# send-alert-email.sh — Send deploy alerts via email.
# Supports two paths:
#   1) mail / mailx command (system MTA — works without any credentials)
#   2) smtp via python3 smtplib (if EMAIL_USER / EMAIL_PASS are set)
#
# Recommended: set EMAIL_USER / EMAIL_PASS / EMAIL_FROM / EMAIL_TO in ~/.bashrc
#   export EMAIL_USER="your@gmail.com"
#   export EMAIL_PASS="your-16-char-app-password"
#   export EMAIL_FROM="your@gmail.com"
#   export EMAIL_TO="recipient@example.com"
#
# Exit codes: 0 = sent, 1 = failed (non-fatal — no exit in deploy.sh)
set -uo pipefail

SUBJECT="${1:-OpenClaw Alert}"
BODY="${2:-No details provided.}"
EMAIL_FROM="${EMAIL_FROM:-${EMAIL_USER:-}}"
EMAIL_TO="${EMAIL_TO:-jleechan@gmail.com}"

# If neither mail command nor SMTP credentials are available, skip silently
# (email is secondary to Slack alert; do not block on missing email)
have_mail_cmd=false
have_smtp=false

if command -v mail >/dev/null 2>&1; then
  have_mail_cmd=true
elif command -v mailx >/dev/null 2>&1; then
  have_mail_cmd=true
fi

# Source .bashrc to pick up EMAIL_USER / EMAIL_PASS if set there
# (launchd agents don't inherit shell env vars)
if [[ -f "$HOME/.bashrc" ]]; then
  # shellcheck disable=SC1090
  source "$HOME/.bashrc" 2>/dev/null || true
fi

if [[ -n "${EMAIL_USER:-}" ]] && [[ -n "${EMAIL_PASS:-}" ]]; then
  have_smtp=true
fi

send_via_mail_cmd() {
  local to="${EMAIL_TO:-jleechan@gmail.com}"
  if command -v mail >/dev/null 2>&1; then
    echo "$BODY" | mail -s "$SUBJECT" "$to" 2>/dev/null && return 0
  elif command -v mailx >/dev/null 2>&1; then
    echo "$BODY" | mailx -s "$SUBJECT" "$to" 2>/dev/null && return 0
  fi
  return 1
}

send_via_smtp() {
  local user="$EMAIL_USER"
  local pass="$EMAIL_PASS"
  local from="${EMAIL_FROM:-$user}"
  local to="$EMAIL_TO"
  local subject="$SUBJECT"
  local body="$BODY"

  python3 - <<'PYEOF'
import smtplib
import os
import sys

user = os.environ.get('EMAIL_USER', '')
pass_ = os.environ.get('EMAIL_PASS', '')
from_addr = os.environ.get('EMAIL_FROM', user)
to_addr = os.environ.get('EMAIL_TO', 'jleechan@gmail.com')
subject = sys.argv[1] if len(sys.argv) > 1 else 'OpenClaw Alert'
body = sys.argv[2] if len(sys.argv) > 2 else ''

if not user or not pass_:
    print('SMTP: EMAIL_USER or EMAIL_PASS not set', file=sys.stderr)
    sys.exit(1)

msg = f"From: {from_addr}\r\nTo: {to_addr}\r\nSubject: {subject}\r\n\r\n{body}"

try:
    with smtplib.SMTP('smtp.gmail.com', 587, timeout=15) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(user, pass_)
        server.sendmail(from_addr, [to_addr], msg.encode('utf-8') if isinstance(msg, str) else msg)
    print('SMTP email sent successfully')
    sys.exit(0)
except Exception as e:
    print(f'SMTP send failed: {e}', file=sys.stderr)
    sys.exit(1)
PYEOF
  return $?
}

# Try mail command first (no credentials needed), then SMTP
if [[ "$have_mail_cmd" == "true" ]]; then
  if send_via_mail_cmd; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Email alert sent via mail command" >> /tmp/openclaw-email-alerts.log
    exit 0
  fi
fi

if [[ "$have_smtp" == "true" ]]; then
  if send_via_smtp; then
    exit 0
  fi
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Email alert skipped (no mail command and no SMTP credentials)" >> /tmp/openclaw-email-alerts.log
exit 0