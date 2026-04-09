#!/usr/bin/env bash
#
# Daily OpenClaw Research — 6:00 PM PT daily
# Sends a tips/research prompt to the openclaw agent session.
# Launchd equivalent of gateway cron job "tips:daily-openclaw-research"
#
# Bead: orch-sq2 (launchd-migration)

set -euo pipefail

ROOT="${OPENCLAW_ROOT:-$HOME/.openclaw}"
THINKING="${OPENCLAW_SCHEDULED_THINKING:-low}"
TIMEOUT="${OPENCLAW_SCHEDULED_TIMEOUT_SECONDS:-1200}"

mkdir -p "$ROOT/logs/scheduled-jobs"

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "[$(ts)] $*"; }

MESSAGE="Before suggesting tips, verify current state with local checks (openclaw gateway status, openclaw cron list --json, openclaw config get diagnostics.flags, openclaw update status). Only include tips that are NOT already implemented, or explicitly label as 'Validation: already configured'. At least 2 tips must cite concrete evidence from local command output (quote the relevant output line). Prioritize jleechan's active setup (Slack, cron automations, diagnostics, docs/context, AO workflows). Provide exactly 3-5 tips with: What changed/why now, concrete command, and source link (docs.openclaw.ai or official GitHub/release notes). No generic beginner advice."

if ! command -v openclaw >/dev/null 2>&1; then
  log "fail: openclaw not in PATH"
  exit 1
fi

log "start daily-research (thinking=$THINKING timeout=${TIMEOUT}s)"
set +e
openclaw agent --thinking "$THINKING" --timeout "$TIMEOUT" --message "$MESSAGE" --json
rc=$?
set -e
log "finish rc=$rc"
exit "$rc"
