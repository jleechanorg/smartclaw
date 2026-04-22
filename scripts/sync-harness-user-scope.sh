#!/usr/bin/env bash
# Copy tracked user-scope harness files from smartclaw to ~/.claude/
# Optional: keep global harness in sync with docs/harness/ after git pull.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DRY_RUN=0
case "${1:-}" in
  "") ;;
  --dry-run) DRY_RUN=1 ;;
  *)
    echo "usage: $(basename "$0") [--dry-run]" >&2
    exit 1
    ;;
esac

USER_CMD_SRC="$REPO_ROOT/docs/harness/user-command-harness.md"
USER_CMD_DST="${HOME}/.claude/commands/harness.md"

if [[ ! -f "$USER_CMD_SRC" ]]; then
  echo "error: missing $USER_CMD_SRC" >&2
  exit 1
fi

copy_one() {
  local src="$1" dst="$2"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "would copy: $src -> $dst"
  else
    mkdir -p "$(dirname "$dst")"
    cp -f "$src" "$dst"
    echo "updated: $dst"
  fi
}

copy_one "$USER_CMD_SRC" "$USER_CMD_DST"

echo ""
echo "harness-engineering: merge docs/harness/harness-engineering-scope-snippet.md manually into ~/.claude/skills/harness-engineering/SKILL.md"
echo "  (or keep ~/.claude copy in sync with repo — snippet is documented in-repo only)"
if [[ "$DRY_RUN" -eq 0 ]]; then
  echo "Done. User-scope command synced."
fi
