#!/usr/bin/env bash
# deploy.sh — Deploy openclaw changes to production via staging canary gate.
#
# Architecture:
#   ~/.smartclaw/      = STAGING (the repo checkout, port 18810)
#   ~/.smartclaw_prod/ = PRODUCTION (separate dir, port 18789, symlinks to shared resources)
#
# Flow:
#   1. Validate staging gateway (port 18810) with canary + monitor
#   2. Push current branch to origin/main (if needed)
#   3. Sync validated config from staging → prod dir
#   4. Restart prod gateway (port 18789) and run canary + monitor
#
# Usage:
#   ./scripts/deploy.sh              # full deploy
#   ./scripts/deploy.sh --skip-push  # skip git push (already pushed)
#   ./scripts/deploy.sh --prod-only  # skip staging, deploy to prod only
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
STAGING_DIR="$HOME/.smartclaw"
PROD_DIR="$HOME/.smartclaw_prod"
STAGING_PORT="${OPENCLAW_STAGING_PORT:-18810}"
PROD_PORT="${OPENCLAW_PROD_PORT:-18789}"
SKIP_PUSH=0
PROD_ONLY=0

for arg in "$@"; do
  case "$arg" in
    --skip-push) SKIP_PUSH=1 ;;
    --prod-only) PROD_ONLY=1 ;;
    -h|--help) echo "Usage: $0 [--skip-push] [--prod-only]"; exit 0 ;;
    *) echo "Unknown arg: $arg"; exit 1 ;;
  esac
done

ts() { date '+%Y-%m-%d %H:%M:%S'; }
die() { echo "DEPLOY FAILED: $1" >&2; exit 1; }
section() { echo ""; echo "=== $1 ==="; echo "$(ts)"; echo ""; }

# ── Preflight ──────────────────────────────────────────────────────────────

section "Preflight"

cd "$REPO_DIR"
BRANCH="$(git branch --show-current)"
REMOTE="$(git remote get-url origin)"
echo "Branch:      $BRANCH"
echo "Remote:      $REMOTE"
echo "Staging dir: $STAGING_DIR"
echo "Prod dir:    $PROD_DIR"

if [[ "$REMOTE" != *"smartclaw"* ]]; then
  die "origin does not point to smartclaw: $REMOTE"
fi

if [[ ! -d "$PROD_DIR" ]]; then
  die "Prod directory does not exist: $PROD_DIR (run scripts/install.sh first)"
fi

echo ""
echo "Running gateway preflight..."
bash "$SCRIPT_DIR/gateway-preflight.sh" || die "gateway-preflight.sh failed"

# ── Stage 1: Staging validation ────────────────────────────────────────────

if [[ "$PROD_ONLY" -eq 0 ]]; then
  section "Stage 1: Staging Gateway Validation (port $STAGING_PORT)"

  STAGING_HEALTH=$(curl -sf --max-time 8 "http://127.0.0.1:${STAGING_PORT}/health" 2>&1 || echo "")
  if [[ -z "$STAGING_HEALTH" ]]; then
    echo "Staging gateway not responding — restarting..."
    launchctl stop "gui/$(id -u)/ai.smartclaw.staging" 2>/dev/null || true
    sleep 2
    launchctl start "gui/$(id -u)/ai.smartclaw.staging" 2>/dev/null || \
      launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/ai.smartclaw.staging.plist" 2>/dev/null || true
    # Gateway needs ~20s to fully initialize (plugins, mem0, Slack)
    echo "Waiting for staging gateway to initialize..."
    sleep 20
    STAGING_HEALTH=$(curl -sf --max-time 8 "http://127.0.0.1:${STAGING_PORT}/health" 2>&1 || echo "")
    [[ -n "$STAGING_HEALTH" ]] || die "Staging gateway failed to start on port $STAGING_PORT"
  fi
  echo "Staging gateway healthy: $STAGING_HEALTH"

  echo ""
  echo "Running staging canary..."
  bash "$SCRIPT_DIR/staging-canary.sh" --port "$STAGING_PORT" || die "Staging canary FAILED"

  echo ""
  echo "STAGING PASSED — all checks green on port $STAGING_PORT"
fi

# ── Stage 2: Push to origin/main ──────────────────────────────────────────

section "Stage 2: Push to Origin"

if [[ "$SKIP_PUSH" -eq 0 ]]; then
  if [[ "$BRANCH" != "main" ]]; then
    echo "Merging $BRANCH into main..."
    git checkout main
    git pull origin main
    git merge "$BRANCH" --no-edit || die "Merge conflict — resolve manually"
    git push origin main || die "Push to origin/main failed"
    echo "Pushed to origin/main"
  else
    echo "Already on main — pulling latest..."
    git pull origin main || die "Pull failed"
    AHEAD=$(git rev-list origin/main..HEAD --count 2>/dev/null || echo "0")
    if [[ "$AHEAD" -gt 0 ]]; then
      echo "Pushing $AHEAD commit(s) to origin/main..."
      git push origin main || die "Push to origin/main failed"
    else
      echo "Already up to date with origin/main"
    fi
  fi
else
  echo "Skipping push (--skip-push)"
fi

# ── Stage 3: Sync config to prod ─────────────────────────────────────────

section "Stage 3: Sync Config to Production"

echo "Syncing validated config from staging → prod..."

# Copy the main config (the one the staging gateway just validated)
cp "$STAGING_DIR/openclaw.json" "$PROD_DIR/openclaw.json"
echo "  openclaw.json synced"

# Ensure symlinks are current for shared resources
for target in SOUL.md TOOLS.md HEARTBEAT.md extensions agents credentials lcm.db; do
  src="$STAGING_DIR/$target"
  dst="$PROD_DIR/$target"
  if [[ -e "$src" ]] && [[ ! -L "$dst" ]]; then
    ln -sf "$src" "$dst"
    echo "  symlinked $target"
  fi
done

echo "Config sync complete"

# ── Stage 4: Production gateway restart + validation ──────────────────────

section "Stage 4: Production Gateway Validation (port $PROD_PORT)"

echo "Restarting production gateway..."
launchctl stop "gui/$(id -u)/com.smartclaw.gateway" 2>/dev/null || \
  launchctl stop "gui/$(id -u)/ai.smartclaw.gateway" 2>/dev/null || true
sleep 3
launchctl start "gui/$(id -u)/com.smartclaw.gateway" 2>/dev/null || \
  launchctl start "gui/$(id -u)/ai.smartclaw.gateway" 2>/dev/null || true
echo "Waiting for production gateway to initialize..."
sleep 20

PROD_HEALTH=$(curl -sf --max-time 8 "http://127.0.0.1:${PROD_PORT}/health" 2>&1 || echo "")
[[ -n "$PROD_HEALTH" ]] || die "Production gateway failed to start on port $PROD_PORT"
echo "Production gateway healthy: $PROD_HEALTH"

echo ""
echo "Running production canary..."
OPENCLAW_STAGING_CONFIG="$PROD_DIR/openclaw.json" \
  bash "$SCRIPT_DIR/staging-canary.sh" --port "$PROD_PORT" || die "Production canary FAILED — ROLLBACK MAY BE NEEDED"

# ── Done ──────────────────────────────────────────────────────────────────

section "Deploy Complete"
echo "Branch:  $BRANCH"
if [[ "$PROD_ONLY" -eq 0 ]]; then
  echo "Staging: PASS (port $STAGING_PORT, dir: $STAGING_DIR)"
else
  echo "Staging: SKIPPED (--prod-only)"
fi
echo "Prod:    PASS (port $PROD_PORT, dir: $PROD_DIR)"
echo "Commit:  $(git log --oneline -1)"
echo ""
echo "$(ts) — deploy finished successfully"
