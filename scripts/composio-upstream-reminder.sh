#!/usr/bin/env bash
set -euo pipefail

TZ="${TZ:-America/Los_Angeles}"; export TZ
LOG_DIR="${HOME}/.smartclaw/logs/scheduled-jobs"
LOG_FILE="${LOG_DIR}/composio-upstream-reminder.log"

mkdir -p "$LOG_DIR"

TITLE="OpenClaw Weekly Reminder"
BODY="Consider pulling Composio upstream commits, then open PRs for most relevant changes."

if command -v osascript >/dev/null 2>&1; then
  osascript -e "display notification \"${BODY}\" with title \"${TITLE}\""
fi

printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S %Z')" "$BODY" >> "$LOG_FILE"
