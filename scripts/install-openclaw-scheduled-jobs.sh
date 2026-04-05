#!/usr/bin/env bash
set -euo pipefail

# OS detection — Linux delegates to install-launchagents.sh Linux path
case "$(uname -s)" in
  Darwin) OS="macos" ;;
  Linux)  OS="linux" ;;
  *) echo "Unsupported OS: $(uname -s)" >&2; exit 1 ;;
esac

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_DIR="$REPO_ROOT/openclaw-config"
LAUNCHD_DIR="$HOME/Library/LaunchAgents"
LIVE_DIR="$HOME/.openclaw"
LIVE_JOBS="$LIVE_DIR/cron/jobs.json"
RUNNER_SRC="$CONFIG_DIR/run-scheduled-job.sh"
RUNNER_DST="$LIVE_DIR/run-scheduled-job.sh"
SCHEDULED_LOG_DIR="$LIVE_DIR/logs/scheduled-jobs"
SYNC_SCRIPT="$REPO_ROOT/scripts/sync-openclaw-config.sh"

default_migrated_job_ids() {
  cat <<'EOF'
522e23a7-c7c1-41f2-b117-a3af05661578
7424ea0d-2c8a-4a59-b58e-09b242c6c58e
5192e214-2754-49d5-b567-07c7b24cb116
882c6964-1deb-4b4b-936d-9edcab83fbda
genesis-memory-curation-weekly
genesis-pattern-extraction-weekly
EOF
}

load_migrated_job_ids() {
  MIGRATED_JOB_IDS=()
  while IFS= read -r id; do
    [[ -n "$id" ]] && MIGRATED_JOB_IDS+=("$id")
  done < <(jq -r '.migratedLaunchdJobIds[]?' "$CONFIG_DIR/cron/jobs.json" 2>/dev/null || true)

  if [[ ${#MIGRATED_JOB_IDS[@]} -eq 0 ]]; then
    while IFS= read -r id; do
      [[ -n "$id" ]] && MIGRATED_JOB_IDS+=("$id")
    done < <(default_migrated_job_ids)
  fi
}

detect_local_timezone() {
  local target
  target="$(readlink /etc/localtime 2>/dev/null || true)"
  if [[ "$target" == *"/zoneinfo/"* ]]; then
    echo "${target##*/zoneinfo/}"
    return 0
  fi
  echo "${TZ:-unknown}"
}

# Validate required tools before mutating anything (avoids half-migrated state)
if ! command -v jq >/dev/null 2>&1; then
  echo "Error: jq is required but not installed. Install with: brew install jq (macOS) or apt install jq (Linux)" >&2
  exit 1
fi
if [[ "$OS" == "macos" ]] && ! command -v launchctl >/dev/null 2>&1; then
  echo "Error: launchctl is required on macOS" >&2
  exit 1
fi
if [[ "$OS" == "linux" ]]; then
  echo "Note: On Linux, scheduled jobs are installed via install-launchagents.sh (systemd timers)."
  echo "This script manages the launchd-to-gateway-cron migration step only."
fi

# Detect openclaw binary location so launchd PATH gets the right dir injected.
# launchd does not inherit the user's shell PATH, so nvm/pyenv/bun-installed
# binaries are invisible unless we add their directory explicitly here.
detect_openclaw_extra_path() {
  local bin_path bin_dir
  bin_path="$(command -v openclaw 2>/dev/null || true)"
  if [[ -z "$bin_path" ]]; then
    echo "Warning: 'openclaw' not found in current PATH — launchd jobs will fail at runtime." >&2
    echo "  Install openclaw or ensure it is on your PATH before running this script." >&2
    # Still continue; the placeholder will be replaced with empty string.
    echo ""
    return
  fi
  bin_dir="$(dirname "$bin_path")"
  # Only inject if not already covered by the static PATH in the plist template.
  case ":$HOME/.bun/bin:$HOME/.local/bin:$HOME/bin:$HOME/Library/pnpm:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:" in
    *":${bin_dir}:"*) echo "" ;;  # already present, no prefix needed
    *)                echo "${bin_dir}:" ;;
  esac
}

OPENCLAW_EXTRA_PATH="$(detect_openclaw_extra_path)"
if [[ -n "$OPENCLAW_EXTRA_PATH" ]]; then
  echo "  ✓ openclaw found at $(command -v openclaw) — injecting '${OPENCLAW_EXTRA_PATH%:}' into launchd PATH"
else
  echo "  ✓ openclaw bin dir already in static launchd PATH (no extra prefix needed)"
fi

# Sync repo openclaw-config to live before loading (ensures jobs.json and skills are current)
if [[ -x "$SYNC_SCRIPT" ]]; then
  OPENCLAW_SYNC_REFRESH_BACKUP=0 "$SYNC_SCRIPT" --execute
fi

load_migrated_job_ids

LOCAL_TZ="$(detect_local_timezone)"
if [[ "$LOCAL_TZ" != "America/Los_Angeles" && "${OPENCLAW_ALLOW_NON_PT_SCHEDULE:-0}" != "1" ]]; then
  echo "Error: local timezone is '$LOCAL_TZ' but migrated schedules are defined for America/Los_Angeles." >&2
  echo "Set OPENCLAW_ALLOW_NON_PT_SCHEDULE=1 to override." >&2
  exit 1
fi

if [[ "$OS" == "linux" ]]; then
  # On Linux, just handle the jobs.json disable step (gateway cron migration) and exit.
  # Systemd timers are installed by install-launchagents.sh.
  if [[ -f "$LIVE_JOBS" ]]; then
    load_migrated_job_ids
    tmp_jobs="$LIVE_JOBS.tmp"
    if jq --argjson ids "$(printf '%s\n' "${MIGRATED_JOB_IDS[@]}" | jq -R . | jq -s .)" \
      '.jobs = ((.jobs // []) | map(if (.id as $id | ($ids | index($id)) != null) then .enabled = false else . end))' \
      "$LIVE_JOBS" >"$tmp_jobs"; then
      mv "$tmp_jobs" "$LIVE_JOBS"
      echo "  ✓ disabled migrated in-app cron jobs in $LIVE_JOBS"
    else
      rm -f "$tmp_jobs"
      echo "  ✗ failed to update $LIVE_JOBS" >&2
    fi
  else
    echo "  ! live cron file missing: $LIVE_JOBS (skipped disable step)"
  fi
  echo "Done. Use install-launchagents.sh to install systemd timers on Linux."
  exit 0
fi

render_and_load_plist() {
  local src="$1"
  local label
  local dst

  label="$(basename "$src" .plist)"
  dst="$LAUNCHD_DIR/$label.plist"

  sed \
    -e "s|@HOME@|$HOME|g" \
    -e "s|@OPENCLAW_EXTRA_PATH@|${OPENCLAW_EXTRA_PATH}|g" \
    "$src" >"$dst"

  if ! launchctl bootstrap "gui/$(id -u)" "$dst" 2>/dev/null; then
    launchctl bootout "gui/$(id -u)" "$dst" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$dst"
  fi

  launchctl enable "gui/$(id -u)/$label" || true
  echo "  ✓ loaded $label"
}

printf 'Installing OpenClaw launchd scheduled jobs\n'
printf 'Repo: %s\n\n' "$REPO_ROOT"

if [[ ! -x "$RUNNER_SRC" ]]; then
  echo "Missing executable runner: $RUNNER_SRC" >&2
  exit 1
fi

mkdir -p "$LAUNCHD_DIR" "$LIVE_DIR" "$LIVE_DIR/cron" "$SCHEDULED_LOG_DIR"
install -m 755 "$RUNNER_SRC" "$RUNNER_DST"
echo "  ✓ installed runner $RUNNER_DST"

echo "Installing launchd scheduled job plists..."
for plist in "$CONFIG_DIR"/ai.openclaw.schedule.*.plist; do
  [[ -f "$plist" ]] || continue
  render_and_load_plist "$plist"
done

if [[ -f "$LIVE_JOBS" ]]; then
  tmp_jobs="$LIVE_JOBS.tmp"
  if jq --argjson ids "$(printf '%s\n' "${MIGRATED_JOB_IDS[@]}" | jq -R . | jq -s .)" '
    .jobs = ((.jobs // []) | map(if (.id as $id | ($ids | index($id)) != null) then .enabled = false else . end))
  ' "$LIVE_JOBS" >"$tmp_jobs"; then
    mv "$tmp_jobs" "$LIVE_JOBS"
  else
    rm -f "$tmp_jobs"
    echo "  ✗ failed to update $LIVE_JOBS with migrated job disable list" >&2
    exit 1
  fi
  echo "  ✓ disabled migrated in-app cron jobs in $LIVE_JOBS"

  gateway_pid="$(pgrep -f 'openclaw.*gateway' | head -n1 || true)"
  if [[ -n "$gateway_pid" ]]; then
    kill -HUP "$gateway_pid" 2>/dev/null || true
    echo "  ✓ signaled gateway reload (pid=$gateway_pid)"
  else
    echo "  ! no running gateway pid found for HUP reload"
  fi
else
  echo "  ! live cron file missing: $LIVE_JOBS (skipped disable step)"
fi

printf '\nVerifying loaded labels...\n'
for plist in "$CONFIG_DIR"/ai.openclaw.schedule.*.plist; do
  label="$(basename "$plist" .plist)"
  if launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
    echo "  ✓ $label registered"
  else
    echo "  ✗ $label not registered"
  fi
done

# Smoke-test: verify 'openclaw' is reachable using the PATH baked into the installed plists.
# This catches nvm/pyenv/bun installs that are invisible to launchd's stripped environment.
printf '\nSmoke-testing launchd PATH for openclaw...\n'
_installed_plist="$LAUNCHD_DIR/ai.openclaw.schedule.backup-4h20.plist"
if [[ -f "$_installed_plist" ]]; then
  _launchd_path="$(plutil -extract EnvironmentVariables.PATH raw -o - "$_installed_plist" 2>/dev/null || true)"
  if [[ -n "$_launchd_path" ]]; then
    if PATH="$_launchd_path" command -v openclaw >/dev/null 2>&1; then
      echo "  ✓ openclaw reachable via launchd PATH ($(PATH="$_launchd_path" command -v openclaw))"
    else
      echo "  ✗ WARN: openclaw NOT reachable via launchd PATH: ${_launchd_path}" >&2
      echo "         Scheduled jobs will fail. Re-run this script after fixing your PATH." >&2
    fi
  fi
fi

echo
echo "Done. Scheduled OpenClaw jobs now run via launchd labels ai.openclaw.schedule.*"
