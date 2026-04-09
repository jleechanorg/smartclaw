#!/usr/bin/env bash
#
# Docs Drift Review — 8:15 AM PT daily
# Runs scripts/docs_audit.sh and reviews docs/context/ for missing/outdated files.
# Launchd equivalent of gateway cron job "healthcheck:docs-drift-review"
#
# Bead: orch-sq2 (launchd-migration)

set -euo pipefail

ROOT="${OPENCLAW_ROOT:-$HOME/.openclaw}"
REPO_DIR="$(git -C "$ROOT" rev-parse --show-toplevel 2>/dev/null || echo "$ROOT")"
# Support both naming conventions: docs_audit.sh (underscore) and docs-audit.sh (hyphen)
DOCS_AUDIT="${REPO_DIR}/scripts/docs_audit.sh"
[[ -x "$DOCS_AUDIT" ]] || DOCS_AUDIT="${REPO_DIR}/scripts/docs-audit.sh"

mkdir -p "$ROOT/logs/scheduled-jobs"

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "[$(ts)] $*"; }

echo "=== Docs Drift Review — $(date '+%Y-%m-%d %H:%M PT') ==="
echo "Repo: $REPO_DIR"
echo ""

if [[ -x "$DOCS_AUDIT" ]]; then
  log "Running docs_audit.sh..."
  if "$DOCS_AUDIT"; then
    log "docs_audit.sh completed successfully."
  else
    log "docs_audit.sh exited with non-zero (rc=$?)"
  fi
else
  log "WARNING: docs_audit.sh not found or not executable at $DOCS_AUDIT"
fi

echo ""

# Summarize docs/context/ freshness
DOCS_CONTEXT="$REPO_DIR/docs/context"
if [[ -d "$DOCS_CONTEXT" ]]; then
  log "Docs context files:"
  for f in "$DOCS_CONTEXT"/*.md "$DOCS_CONTEXT"/*.json; do
    [[ -e "$f" ]] || continue
    lastmod="$(stat -f '%Sm' "$f" 2>/dev/null || stat -c '%y' "$f" 2>/dev/null || echo 'unknown')"
    echo "  $(basename "$f") (last modified: $lastmod)"
  done
else
  log "docs/context/ not found at $DOCS_CONTEXT"
fi

echo ""
log "Done."
