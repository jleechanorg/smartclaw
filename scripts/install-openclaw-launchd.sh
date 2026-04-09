#!/usr/bin/env bash
# install-openclaw-launchd.sh
# Central entrypoint for installing all OpenClaw launchd services.
# Single source of truth for install. Called by bootstrap.sh and run standalone.
#
# Installs:
#   Infrastructure (always-on daemons):
#     com.openclaw.gateway        — OpenClaw gateway (port 18789)
#     ai.openclaw.qdrant          — Qdrant vector DB (Docker, port 6333)
#     ai.openclaw.webhook         — GitHub webhook ingress daemon (port 19888)
#     ai.openclaw.startup-check   — Startup verification on login
#     ai.openclaw.monitor-agent   — Periodic health monitoring (every 30 min)
#     ai.agento-manager           — Agent Orchestrator manager (KeepAlive)
#     ai.agento.dashboard         — AO web dashboard (KeepAlive, port 3020)
#     ai.openclaw.lifecycle-manager — AO lifecycle workers (KeepAlive)
#
#   Scheduled jobs (launchd-managed, NOT gateway-cron):
#     ai.openclaw.schedule.morning-log-review     — 8:00 AM PT daily
#     ai.openclaw.schedule.weekly-error-trends   — Mon 9:00 AM PT
#     ai.openclaw.schedule.docs-drift-review      — 8:15 AM PT daily
#     ai.openclaw.schedule.cron-backup-sync       — 8:25 AM PT daily
#     ai.openclaw.schedule.daily-research         — 6:00 PM PT Mon-Fri
#     ai.openclaw.schedule.bug-hunt-9am           — 9:00 AM PT Mon-Fri
#     ai.openclaw.schedule.harness-analyzer-9am  — 9:00 AM PT Mon-Fri
#     ai.openclaw.schedule.orch-health-weekly     — Mon 9:30 AM PT
#     ai.openclaw.schedule.github-intake          — 9:00 AM PT daily
#     ai.openclaw.schedule.qdrant-backup          — 2:00 AM nightly (→ Dropbox)
#
# NOT installed here (remain live in ~/.openclaw/cron/jobs.json, gitignored):
#   Gateway-cron jobs are managed by the OpenClaw gateway itself and are NOT
#   migrated to launchd. This includes ad-hoc and PR-automation jobs:
#     - thread-followup-*   (short-lived follow-up tasks)
#     - Any future pr-automation / AO lifecycle-worker jobs
#
# Usage:
#   ./scripts/install-openclaw-launchd.sh
#   ./scripts/install-openclaw-launchd.sh --dry-run
#
set -euo pipefail

DRY_RUN="${DRY_RUN:-0}"
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

_echo() { printf '%s\n' "$@"; }
_run() {
  if [[ "$DRY_RUN" == "1" ]]; then
    _echo "  [dry-run] $*"
  else
    "$@"
  fi
}

echo "=== OpenClaw Launchd Installer ==="
echo "Repo root: $REPO_ROOT"
echo ""

# Step 1: Core infrastructure services (install-launchagents.sh)
echo "[1/3] Installing core infrastructure services..."
_infra="$REPO_ROOT/scripts/install-launchagents.sh"
if [[ -x "$_infra" ]]; then
  if [[ "$DRY_RUN" == "1" ]]; then
    _echo "  [dry-run] would run: CALLED_AS_PART_OF_CENTRAL=1 bash $_infra"
  else
    CALLED_AS_PART_OF_CENTRAL=1 bash "$_infra"
  fi
else
  echo "  ✗ install-launchagents.sh not found or not executable: $_infra" >&2
fi

echo ""

# Step 2: Scheduled jobs (install-openclaw-scheduled-jobs.sh)
echo "[2/3] Installing scheduled launchd jobs (migrating from gateway cron)..."
_sched="$REPO_ROOT/scripts/install-openclaw-scheduled-jobs.sh"
if [[ -x "$_sched" ]]; then
  if [[ "$DRY_RUN" == "1" ]]; then
    _echo "  [dry-run] would run: $_sched"
  else
    bash "$_sched"
  fi
else
  echo "  ✗ install-openclaw-scheduled-jobs.sh not found: $_sched" >&2
fi

echo ""

# Step 2b: mem0 native module watchdog
echo "[2b] Installing mem0 native module watchdog..."
_watchdog_plist="$HOME/Library/LaunchAgents/com.openclaw.mem0-watchdog.plist"
_watchdog_template="$REPO_ROOT/launchd/com.openclaw.mem0-watchdog.plist"
if [[ ! -f "$_watchdog_plist" ]] && [[ -f "$_watchdog_template" ]]; then
  # Substitute __HOME__ with actual $HOME in plist before deploying
  sed "s|__HOME__|$HOME|g" "$_watchdog_template" > "$_watchdog_plist"
  _run cat "$_watchdog_plist" > /dev/null
  _echo "  Copied plist to $_watchdog_plist (with HOME substituted)"
fi
if [[ -f "$_watchdog_plist" ]]; then
  if [[ "$DRY_RUN" == "1" ]]; then
    _echo "  [dry-run] would bootstrap com.openclaw.mem0-watchdog"
  else
    _bs_err=$(launchctl bootstrap "gui/$(id -u)" "$_watchdog_plist" 2>&1)
    _bs_rc=$?
    if [[ "$_bs_rc" -eq 0 ]]; then
      _echo "  ✓ com.openclaw.mem0-watchdog bootstrapped"
    else
      _echo "  bootstrap failed (exit $_bs_rc): $_bs_err"
      _echo "  (may already be loaded — verify with: launchctl print gui/\$(id -u)/com.openclaw.mem0-watchdog)"
    fi
  fi
else
  _echo "  WARN: watchdog plist not found — skipping (run manually or re-run installer)"
fi

# Step 2c: Record NODE_MODULE_VERSION baseline for mem0/better-sqlite3
# Only record if no baseline exists — do NOT clobber an existing baseline
echo "[2c] Checking NODE_MODULE_VERSION baseline..."
_gateway_node="/Users/jleechan/.nvm/versions/node/v22.22.0/bin/node"
_baseline="$HOME/.openclaw/.gateway-node-version"
if [[ "$DRY_RUN" == "1" ]]; then
  _echo "  [dry-run] would check and record MODULE_VERSION from $_gateway_node → $_baseline"
elif [[ -x "$_gateway_node" ]]; then
  _modver=$("$_gateway_node" -e "process.stdout.write(String(process.versions.modules))" 2>/dev/null || echo "unknown")
  if [[ "$_modver" != "unknown" ]]; then
    if [[ -f "$_baseline" ]]; then
      _existing=$(cat "$_baseline" | tr -d '[:space:]')
      _echo "  SKIP: baseline already exists (MODULE_VERSION=$_existing) — not clobbering"
    else
      echo "$_modver" > "$_baseline"
      _echo "  ✓ Baseline recorded: MODULE_VERSION=$_modver → $_baseline"
    fi
  else
    _echo "  WARN: could not read MODULE_VERSION from $_gateway_node"
  fi
else
  _echo "  WARN: $_gateway_node not found — baseline not checked"
fi

echo ""

# Step 3: Summary
echo "[3/3] Install summary..."
echo ""
echo "Launchd labels (macOS):"
if command -v launchctl >/dev/null 2>&1; then
  for label in \
    com.openclaw.gateway \
    ai.openclaw.qdrant \
    ai.openclaw.schedule.morning-log-review \
    ai.openclaw.schedule.weekly-error-trends \
    ai.openclaw.schedule.docs-drift-review \
    ai.openclaw.schedule.cron-backup-sync \
    ai.openclaw.schedule.daily-research \
    ai.openclaw.schedule.bug-hunt-9am \
    ai.openclaw.schedule.harness-analyzer-9am \
    ai.openclaw.schedule.orch-health-weekly \
    ai.openclaw.schedule.github-intake \
    ai.openclaw.claude-memory-sync \
    ai.openclaw.monitor-agent; do
    if launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
      _echo "  ✓ $label"
    else
      _echo "  ✗ $label NOT loaded"
    fi
  done
else
  _echo "  (launchctl not available — not on macOS?)"
fi

echo ""
echo "Log locations:"
echo "  Scheduled job logs: ~/.openclaw/logs/scheduled-jobs/"
echo "  Gateway logs:       ~/.openclaw/logs/gateway.log"
echo ""
echo "Docs: see docs/CRON_MIGRATION.md for live-vs-tracked distinction."
