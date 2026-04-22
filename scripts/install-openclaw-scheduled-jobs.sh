#!/usr/bin/env bash
# Install OpenClaw scheduled jobs as launchd plists (macOS).
# Migrates infrastructure/health jobs from gateway-cron to launchd.
# See scripts/daily-openclaw-research.sh for the research job script.
#
# live-vs-tracked distinction:
#   ~/.smartclaw/cron/jobs.json  -- LIVE, gitignored; gateway-managed PR automation jobs
#                                  (e.g. thread-followup-*, pr-monitor-*). These stay in
#                                  gateway cron and are NOT migrated to launchd.
#   launchd/                     -- TRACKED in repo; infrastructure/health/scheduled
#                                  review jobs (morning-log-review, weekly-error-trends,
#                                  docs-drift-review, cron-backup-sync, daily-research,
#                                  bug-hunt, harness-analyzer, orch-health-weekly).
#
# The gateway-cron jobs that remain live (NOT migrated):
#   - thread-followup-ao263      (ad-hoc, short-lived; gateway owns lifecycle)
#   - Any future pr-automation jobs (managed by AO lifecycle-worker)
set -euo pipefail

# OS detection -- Linux delegates to install-launchagents.sh Linux path
case "$(uname -s)" in
  Darwin) OS="macos" ;;
  Linux)  OS="linux" ;;
  *) echo "Unsupported OS: $(uname -s)" >&2; exit 1 ;;
esac

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAUNCHD_TEMPLATES_DIR="$REPO_ROOT/launchd"
LAUNCHD_DIR="$HOME/Library/LaunchAgents"
LIVE_DIR="$HOME/.smartclaw"
LIVE_JOBS="$LIVE_DIR/cron/jobs.json"
SCHEDULED_LOG_DIR="$LIVE_DIR/logs/scheduled-jobs"

# Job IDs migrated from gateway cron to launchd (will be disabled in jobs.json).
# Format: "gateway job id" -> "launchd plist basename"
declare -A MIGRATED_JOBS=(
  ["c0accca2-3b58-4da6-ba84-e8c929387e30"]="ai.smartclaw.schedule.morning-log-review"
  ["4ec2aa58-5c97-4c46-8775-a7f030d1dec6"]="ai.smartclaw.schedule.weekly-error-trends"
  ["95f858df-0fe8-4434-90c9-c5c89f61889e"]="ai.smartclaw.schedule.docs-drift-review"
  ["d6bb3693-9f5c-4a4e-99ed-bc56eb33e35c"]="ai.smartclaw.schedule.cron-backup-sync"
  ["abf80788-7bb0-4ce7-9e09-6c1a97faa5cd"]="ai.smartclaw.schedule.daily-research"
)
MIGRATED_IDS="${!MIGRATED_JOBS[@]}"

default_migrated_job_ids() {
  for id in "${!MIGRATED_JOBS[@]}"; do
    echo "$id"
  done
}

load_migrated_job_ids() {
  MIGRATED_JOB_IDS=()
  # Read migrated IDs from the live jobs.json (gitignored -- the canonical source)
  while IFS= read -r id; do
    [[ -n "$id" ]] && MIGRATED_JOB_IDS+=("$id")
  done < <(jq -r '.migratedLaunchdJobIds[]?' "$LIVE_JOBS" 2>/dev/null || true)

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
    echo ""
    return
  fi
  bin_dir="$(dirname "$bin_path")"
  case ":$HOME/.bun/bin:$HOME/.local/bin:$HOME/bin:$HOME/Library/pnpm:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:" in
    *":${bin_dir}:"*) echo "" ;;
    *)                echo "${bin_dir}:" ;;
  esac
}

OPENCLAW_EXTRA_PATH="$(detect_openclaw_extra_path)"
# Normalize: strip any trailing colon, then add exactly one so the PATH concat is safe
if [[ -n "$OPENCLAW_EXTRA_PATH" ]]; then
  OPENCLAW_EXTRA_PATH="${OPENCLAW_EXTRA_PATH%:}:"
fi

# Detect gog binary location — required by gmail-daily-recap.sh
detect_gog_extra_path() {
  local bin_path bin_dir
  bin_path="$(command -v gog 2>/dev/null || true)"
  if [[ -z "$bin_path" ]]; then
    echo ""
    return
  fi
  bin_dir="$(dirname "$bin_path")"
  case ":$HOME/.bun/bin:$HOME/.local/bin:$HOME/bin:$HOME/Library/pnpm:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:" in
    *":${bin_dir}:"*) echo "" ;;
    *)                echo "${bin_dir}:" ;;
  esac
}

GOG_EXTRA_PATH="$(detect_gog_extra_path)"
if [[ -n "$GOG_EXTRA_PATH" ]]; then
  GOG_EXTRA_PATH="${GOG_EXTRA_PATH%:}:"
fi

if [[ "$OS" == "linux" ]]; then
  echo "Linux: scheduled jobs handled by install-launchagents.sh (systemd timers)."
  exit 0
fi

printf 'Installing OpenClaw launchd scheduled jobs\n'
printf 'Repo: %s\n\n' "$REPO_ROOT"

mkdir -p "$LAUNCHD_DIR" "$LIVE_DIR" "$LIVE_DIR/cron" "$LIVE_DIR/scripts" "$SCHEDULED_LOG_DIR"

# Install job scripts (morning-log-review, weekly-error-trends, etc.)
echo "Installing job scripts..."
declare -a JOB_SCRIPTS=(
  "$REPO_ROOT/scripts/morning-log-review.sh"
  "$REPO_ROOT/scripts/weekly-error-trends.sh"
  "$REPO_ROOT/scripts/docs-audit.sh"
  "$REPO_ROOT/scripts/cron-backup-sync.sh"
  "$REPO_ROOT/scripts/docs-drift-review.sh"
  "$REPO_ROOT/scripts/daily-openclaw-research.sh"
  "$REPO_ROOT/scripts/bug-hunt-daily.sh"
  "$REPO_ROOT/scripts/harness-analyzer.sh"
  "$REPO_ROOT/scripts/gmail-daily-recap.sh"
  "$REPO_ROOT/scripts/composio-upstream-reminder.sh"
  "$REPO_ROOT/scripts/commit-pending-changes.sh"
)
for script in "${JOB_SCRIPTS[@]}"; do
  if [[ ! -f "$script" ]]; then
    echo "ERROR: required script missing: $script" >&2
    exit 1
  fi
  if [[ ! -x "$script" ]]; then
    echo "ERROR: script is not executable: $script" >&2
    exit 1
  fi
  dst="$LIVE_DIR/scripts/$(basename "$script")"
  # When the repo lives at ~/.smartclaw, REPO_ROOT/scripts and LIVE_DIR/scripts are the same path;
  # BSD install(1) errors with "same file" and a non-zero exit.
  if [[ "$(realpath "$script" 2>/dev/null)" != "$(realpath "$dst" 2>/dev/null)" ]]; then
    install -m 755 "$script" "$dst"
  fi
  echo "  - installed $(basename "$script")"
done

# Render and load each scheduled plist template (or standalone .plist without .template)
_install_plist() {
  local src="$1"
  local label
  if [[ "$src" == *.plist.template ]]; then
    label="$(basename "$src" .plist.template)"
  else
    label="$(basename "$src" .plist)"
  fi
  local dst="$LAUNCHD_DIR/$label.plist"

  sed \
    -e "s|@HOME@|$HOME|g" \
    -e "s|@OPENCLAW_EXTRA_PATH@|${OPENCLAW_EXTRA_PATH}|g" \
    -e "s|@GOG_EXTRA_PATH@|${GOG_EXTRA_PATH}|g" \
    -e "s|@REPO_ROOT@|$REPO_ROOT|g" \
    "$src" >"$dst"

  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  sleep 0.35
  local _attempt=1
  while [[ "$_attempt" -le 3 ]]; do
    if launchctl bootstrap "gui/$(id -u)" "$dst"; then
      break
    fi
    if [[ "$_attempt" -lt 3 ]]; then
      sleep 0.5
      launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
      sleep 0.35
    else
      echo "  ✗ $label FAILED to load after 3 attempts" >&2
      return 1
    fi
    _attempt=$((_attempt + 1))
  done
  launchctl enable "gui/$(id -u)/$label" || true
  echo "  - loaded $label"
}

echo "Installing launchd scheduled job plists..."
for plist in "$LAUNCHD_TEMPLATES_DIR"/ai.smartclaw.schedule.*.plist.template; do
  [[ -f "$plist" ]] || continue
  _install_plist "$plist"
done

# Standalone schedule plists (no .template suffix), e.g. stability-report.
# Skip when a .plist.template exists for the same label (avoid double bootstrap).
for plist in "$LAUNCHD_TEMPLATES_DIR"/ai.smartclaw.schedule.*.plist; do
  [[ -f "$plist" ]] || continue
  [[ "$plist" == *.plist.template ]] && continue
  _base="${plist%.plist}"
  [[ -f "${_base}.plist.template" ]] && continue
  _install_plist "$plist"
done

# Also install ai.smartclaw.claude-memory-sync (background sync service).
# Does not use the ai.smartclaw.schedule.* pattern — it's a persistent service, not
# a cron-style scheduled job — but is installed alongside them for discoverability.
# The template is named ai.smartclaw.claude-memory-sync.plist.template so that
# _install_plist derives label=ai.smartclaw.claude-memory-sync, matching the plist
# <string>ai.smartclaw.claude-memory-sync</string> (launchctl enable/disable work).
#
# Note: install-launchagents.sh also installs this plist as part of infrastructure.
# Both paths use _install_plist / install_plist with @OPENCLAW_EXTRA_PATH@ substitution,
# so double-install is safe (same result each time). This is intentional redundancy for
# standalone-vs-central-installer portability.
MEMORY_SYNC_PLIST="$LAUNCHD_TEMPLATES_DIR/ai.smartclaw.claude-memory-sync.plist.template"
if [[ -f "$MEMORY_SYNC_PLIST" ]]; then
  _install_plist "$MEMORY_SYNC_PLIST"
fi

# Disable migrated jobs in live gateway cron (leave them in jobs.json -- gitignored locally)
if [[ -f "$LIVE_JOBS" ]]; then
  ids_json="$(printf '%s\n' $MIGRATED_IDS | jq -R . | jq -s .)"
  tmp_jobs="$LIVE_JOBS.tmp"
  if jq --argjson ids "$ids_json" '
    .jobs = ((.jobs // []) | map(
      if (.id as $id | ($ids | index($id)) != null)
      then .enabled = false
      else .
      end
    ))
  ' "$LIVE_JOBS" >"$tmp_jobs"; then
    mv "$tmp_jobs" "$LIVE_JOBS"
    echo "  - disabled migrated gateway cron jobs in $LIVE_JOBS"
  else
    rm -f "$tmp_jobs"
    echo "  - failed to update $LIVE_JOBS" >&2
  fi

  # Signal gateway to reload jobs.json
  gateway_pid="$(pgrep -f 'openclaw.*gateway' | head -n1 || true)"
  if [[ -n "$gateway_pid" ]]; then
    kill -HUP "$gateway_pid" 2>/dev/null || true
    echo "  - signaled gateway reload (pid=$gateway_pid)"
  else
    echo "  ! no running gateway pid found for HUP reload"
  fi
else
  echo "  ! live cron file missing: $LIVE_JOBS (skipping disable step)"
fi

printf '\nVerifying loaded labels...\n'
for plist in "$LAUNCHD_TEMPLATES_DIR"/ai.smartclaw.schedule.*.plist.template; do
  label="$(basename "$plist" .plist.template)"
  if launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
    echo "  - $label registered"
  else
    echo "  - $label not registered"
  fi
done

# Smoke-test: verify openclaw is callable via the launchd PATH
printf '\nSmoke-testing launchd PATH for openclaw and gog...\n'
if command -v openclaw >/dev/null 2>&1; then
  echo "  - openclaw found at $(command -v openclaw) -- PATH injection successful"
else
  echo "  - openclaw not found in launchd PATH (expected in ~/.bun/bin or PATH)"
fi
if command -v gog >/dev/null 2>&1; then
  echo "  - gog found at $(command -v gog) -- PATH injection successful"
else
  echo "  - gog not found in launchd PATH (expected in ~/.bun/bin or PATH)"
fi

echo
echo "Done. Scheduled OpenClaw jobs now run via launchd labels ai.smartclaw.schedule.*"
echo ""
echo "Migration status:"
echo "  Migrated to launchd (disabled in gateway cron):"
for id in $MIGRATED_IDS; do
  echo "    - $id -> ${MIGRATED_JOBS[$id]}"
done
echo ""
echo "  Remaining in gateway cron (live, gitignored):"
echo "    - thread-followup-ao263 (ad-hoc, short-lived)"
echo "    - Any future pr-automation / AO lifecycle jobs"
