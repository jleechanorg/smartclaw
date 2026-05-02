#!/usr/bin/env bash
# Captures a system snapshot and doc-gap report to docs/context/.
# Output is ANSI-stripped and home-path-normalized for GitHub readability.
set -euo pipefail

ROOT="${HERMES_ROOT:-$HOME/.hermes}"
CTX="$ROOT/docs/context"
SNAP="$CTX/SYSTEM_SNAPSHOT.md"
GAPS="$CTX/DOC_GAPS.md"
OPENCLAW_ROOT="${OPENCLAW_ROOT:-$HOME/.smartclaw}"
TIRITH="$OPENCLAW_ROOT/bin/tirith"

# Strip ANSI color codes and normalize ~/.smartclaw paths to ~/.
sanitize() { sed -E $'s/\x1b\\[[0-9;]*m//g' | sed -E "s|$HOME/.smartclaw/?|~/.smartclaw/|g"; }

mkdir -p "$CTX"

{
  echo "# System Snapshot"
  echo
  echo "Generated: $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
  echo
  echo "## OpenClaw version"
  node -e "try{const p=require('$ROOT/package.json');console.log('openclaw',p.version)}catch(e){console.log('(unknown)')}" 2>&1 | sanitize || echo "(unknown)"
  echo
  echo "## Tirith version"
  "$TIRITH" --version 2>&1 | sanitize || echo "(tirith not found)"
  echo
  echo "## Gateway status"
  curl -s --max-time 5 http://localhost:18789/health 2>&1 | sanitize || echo "(gateway not reachable)"
  echo
  echo "## Cron jobs"
  echo "(see CRON_JOBS_BACKUP.md for current job list)"
  echo
  echo "## Diagnostics flags"
  echo "(see openclaw logs for runtime diagnostics)"
} > "$SNAP"

missing=0
: > "$GAPS"
echo "# Documentation Gap Report" >> "$GAPS"
echo >> "$GAPS"
echo "Generated: $(date -u '+%Y-%m-%d %H:%M:%S UTC')" >> "$GAPS"
echo >> "$GAPS"

for f in PRODUCT.md WORKFLOWS.md FILE_MAP.md LEARNINGS.md PROMPTING_GUIDES.md; do
  if [[ ! -s "$CTX/$f" ]]; then
    echo "- Missing or empty: docs/context/$f" >> "$GAPS"
    missing=1
  fi
done

if [[ $missing -eq 0 ]]; then
  echo "- No required doc gaps detected." >> "$GAPS"
fi

echo "Wrote: $SNAP"
echo "Wrote: $GAPS"
