#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKSPACE_DIR="${HOME}/.openclaw/workspace"
FILES=(AGENTS.md SOUL.md TOOLS.md USER.md IDENTITY.md HEARTBEAT.md)

usage() {
  cat <<USAGE
Usage: $(basename "$0") [--sync]

Checks byte-for-byte parity between repo root policy files and ~/.openclaw/workspace.

Options:
  --sync    Copy repo root files into ~/.openclaw/workspace when differences exist.
  -h, --help  Show help.
USAGE
}

sync_mode=0
case "${1:-}" in
  "") ;;
  --sync) sync_mode=1 ;;
  -h|--help)
    usage
    exit 0
    ;;
  *)
    echo "Unknown argument: $1" >&2
    usage >&2
    exit 2
    ;;
esac

if [[ ! -d "$WORKSPACE_DIR" ]]; then
  echo "Workspace directory not found: $WORKSPACE_DIR" >&2
  exit 1
fi

status=0
for file in "${FILES[@]}"; do
  repo_file="$REPO_ROOT/$file"
  workspace_file="$WORKSPACE_DIR/$file"

  if [[ ! -f "$repo_file" ]]; then
    echo "MISSING_REPO $file"
    status=1
    continue
  fi

  if [[ ! -f "$workspace_file" ]]; then
    echo "MISSING_WORKSPACE $file"
    status=1
    if [[ "$sync_mode" -eq 1 ]]; then
      cp "$repo_file" "$workspace_file"
      echo "SYNCED $file"
    fi
    continue
  fi

  if cmp -s "$repo_file" "$workspace_file"; then
    echo "MATCH $file"
    continue
  fi

  echo "DIFF $file"
  diff -u "$repo_file" "$workspace_file" || true

  if [[ "$sync_mode" -eq 1 ]]; then
    cp "$repo_file" "$workspace_file"
    echo "SYNCED $file"
  else
    status=1
  fi
done

if [[ "$sync_mode" -eq 1 ]]; then
  for file in "${FILES[@]}"; do
    if ! cmp -s "$REPO_ROOT/$file" "$WORKSPACE_DIR/$file"; then
      echo "VERIFY_FAILED $file" >&2
      exit 1
    fi
  done
  echo "All policy files synced and verified."
fi

exit "$status"
