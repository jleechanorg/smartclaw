#!/usr/bin/env bash
# Captures a system snapshot and doc-gap report to docs/context/.
# Output is ANSI-stripped and home-path-normalized for GitHub readability.
set -euo pipefail

ROOT="${OPENCLAW_ROOT:-$HOME/.openclaw}"
CTX="$ROOT/docs/context"
SNAP="$CTX/SYSTEM_SNAPSHOT.md"
GAPS="$CTX/DOC_GAPS.md"

# Strip ANSI color codes and normalize ~/.openclaw paths to ~/.
sanitize() { sed -E $'s/\x1b\\[[0-9;]*m//g' | sed -E "s|$HOME/.openclaw/?|~/.openclaw/|g"; }

mkdir -p "$CTX"

{
  echo "# System Snapshot"
  echo
  echo "Generated: $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
  echo
  echo "## OpenClaw version"
  NO_COLOR=1 openclaw --version 2>&1 | sanitize || true
  echo
  echo "## Gateway status"
  NO_COLOR=1 openclaw status 2>&1 | sanitize || true
  echo
  echo "## Cron jobs"
  NO_COLOR=1 openclaw cron list --json 2>&1 | sanitize || true
  echo
  echo "## Diagnostics flags"
  NO_COLOR=1 openclaw config get diagnostics.flags 2>&1 | sanitize || true
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
