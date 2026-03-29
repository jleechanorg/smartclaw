#!/usr/bin/env bash
set -euo pipefail

JOB_ID="${1:-}"
if [[ -z "$JOB_ID" ]]; then
  echo "Usage: $0 <job-id>" >&2
  exit 2
fi

LIVE_JOBS="$HOME/.openclaw/cron/jobs.json"
LOG_DIR="$HOME/.openclaw/logs/scheduled-jobs"
LOCK_ROOT="$LOG_DIR/.locks"
mkdir -p "$LOG_DIR" "$LOCK_ROOT"

LOG_FILE="$LOG_DIR/${JOB_ID}.log"
LOCK_DIR="$LOCK_ROOT/${JOB_ID}.lock"

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  printf '[%s] skip: lock exists for %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$JOB_ID" >>"$LOG_FILE"
  exit 0
fi
cleanup() {
  rmdir "$LOCK_DIR" 2>/dev/null || true
}
trap cleanup EXIT

if [[ ! -f "$LIVE_JOBS" ]]; then
  printf '[%s] fail: missing %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$LIVE_JOBS" >>"$LOG_FILE"
  exit 1
fi

if ! command -v jq >/dev/null 2>&1; then
  printf '[%s] fail: jq is not available\n' "$(date '+%Y-%m-%d %H:%M:%S')" >>"$LOG_FILE"
  exit 1
fi

if ! command -v openclaw >/dev/null 2>&1; then
  printf '[%s] fail: openclaw is not available in PATH\n' "$(date '+%Y-%m-%d %H:%M:%S')" >>"$LOG_FILE"
  exit 1
fi

job_enabled=$(jq -r --arg id "$JOB_ID" '.jobs[]? | select(.id == $id) | .enabled // false' "$LIVE_JOBS" | head -n1)
job_name=$(jq -r --arg id "$JOB_ID" '.jobs[]? | select(.id == $id) | .name // empty' "$LIVE_JOBS" | head -n1)
job_kind=$(jq -r --arg id "$JOB_ID" '.jobs[]? | select(.id == $id) | .payload.kind // empty' "$LIVE_JOBS" | head -n1)
job_message=$(jq -r --arg id "$JOB_ID" '.jobs[]? | select(.id == $id) | .payload.message // empty' "$LIVE_JOBS")

if [[ -z "$job_name" ]]; then
  printf '[%s] fail: job id not found in %s: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$LIVE_JOBS" "$JOB_ID" >>"$LOG_FILE"
  exit 1
fi

if [[ "$job_enabled" != "true" ]]; then
  if [[ "${OPENCLAW_SCHEDULED_REQUIRE_ENABLED:-0}" == "1" ]]; then
    printf '[%s] skip: job disabled in %s (%s)\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$LIVE_JOBS" "$job_name" >>"$LOG_FILE"
    exit 0
  fi
  printf '[%s] warn: running disabled job id=%s (%s) due launchd migration mode\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$JOB_ID" "$job_name" >>"$LOG_FILE"
fi

if [[ "$job_kind" != "agentTurn" ]]; then
  printf '[%s] fail: unsupported payload kind for %s: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$JOB_ID" "$job_kind" >>"$LOG_FILE"
  exit 1
fi

if [[ -z "$job_message" ]]; then
  printf '[%s] fail: empty payload.message for %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$JOB_ID" >>"$LOG_FILE"
  exit 1
fi

thinking_level="${OPENCLAW_SCHEDULED_THINKING:-low}"
timeout_seconds="${OPENCLAW_SCHEDULED_TIMEOUT_SECONDS:-1200}"

{
  printf '\n[%s] start %s (%s)\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$JOB_ID" "$job_name"
  printf '[%s] command: openclaw agent --thinking %s --timeout-seconds %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$thinking_level" "$timeout_seconds"
  set +e
  openclaw agent --thinking "$thinking_level" --timeout-seconds "$timeout_seconds" --message "$job_message" --json
  rc=$?
  set -e
  printf '[%s] finish rc=%s %s (%s)\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$rc" "$JOB_ID" "$job_name"
  exit "$rc"
} >>"$LOG_FILE" 2>&1
