#!/usr/bin/env bash
# consolidate-workspace-snapshots.sh — ONE-TIME migration script.
#
# Rsyncs existing backup snapshots from the workspace copy of openclaw
# (where the old misconfigured openclaw-cron job deposited them) into
# this repo's .openclaw-backups/ directory.
#
# Source:      ~/.openclaw/workspace/openclaw/.openclaw-backups/
# Destination: <repo>/.openclaw-backups/
#
# Safe to run multiple times: --ignore-existing never overwrites newer data.
# After consolidation: commit + push manually (or let the next backup cycle do it).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DST_BASE="$REPO_ROOT/.openclaw-backups"
SRC_BASE="${HOME}/.openclaw/workspace/openclaw/.openclaw-backups"

if [[ ! -d "$SRC_BASE" ]]; then
  echo "Source not found: $SRC_BASE"
  echo "Nothing to consolidate."
  exit 0
fi

SRC_COUNT="$(ls -1 "$SRC_BASE" | grep -c '^[0-9]' || true)"
echo "Source snapshots: $SRC_COUNT under $SRC_BASE"
echo "Destination: $DST_BASE"

mkdir -p "$DST_BASE"

# --archive        — preserve timestamps/perms
# --ignore-existing — never overwrite snapshots already in destination
# --delete-after   — remove stray files in destination dirs (within each snapshot)
# --prune-empty-dirs — skip empty dirs
rsync -a --ignore-existing --prune-empty-dirs \
  "$SRC_BASE/" "$DST_BASE/"

DST_COUNT="$(ls -1 "$DST_BASE" | grep -c '^[0-9]' || true)"
echo "Destination snapshots after consolidation: $DST_COUNT"
echo ""
echo "Next steps:"
echo "  1. Review: ls $DST_BASE | head -20"
echo "  2. Commit: cd $REPO_ROOT && git add .openclaw-backups/ && git commit -m 'chore: consolidate workspace snapshots into jleechanclaw'"
echo "  3. Push:   git push origin HEAD:main"
echo ""
echo "Done."
