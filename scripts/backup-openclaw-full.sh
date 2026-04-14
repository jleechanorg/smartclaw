#!/usr/bin/env bash
set -euo pipefail

# Backup ~/.smartclaw into this repo with sensitive redaction.
#
# Rsyncs into a single .smartclaw-backups/latest/ directory (--delete keeps it
# current) and commits if anything changed. Git history is the point-in-time
# record — no dated subdirectories needed.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SRC_DIR="${HOME}/.smartclaw"
SNAP_BASE="$REPO_ROOT/.smartclaw-backups"
SNAPSHOT_DIR="$SNAP_BASE/latest"
SNAPSHOT_TS="$(date +"%Y%m%d_%H%M%S")"

mkdir -p "$SNAPSHOT_DIR"

# ---------------------------------------------------------------------------
# Step 1: rsync mirror — incremental, --delete removes files gone from source.
# ---------------------------------------------------------------------------
rsync -a --delete \
  --exclude='.smartclaw-backups' \
  --exclude='.git' \
  --exclude='.DS_Store' \
  --exclude='workspace' \
  --exclude='workspace-*' \
  --exclude='smartclaw' \
  --exclude='credentials/whatsapp' \
  --exclude='*.lock' \
  --exclude='extensions/*/node_modules' \
  "$SRC_DIR/" "$SNAPSHOT_DIR/"

# ---------------------------------------------------------------------------
# Step 2: Post-redaction pass via orchestration.backup_redaction module.
# Symlinks are never followed — prevents writing through symlinks to targets
# outside the snapshot directory.
# ---------------------------------------------------------------------------
export SNAPSHOT_DIR SNAPSHOT_TS SRC_DIR
PYTHONPATH="$REPO_ROOT/src" python3 -m orchestration.backup_redaction "$SNAPSHOT_DIR"

cd "$REPO_ROOT"

git add .smartclaw-backups/latest/
if git diff --quiet --cached -- .smartclaw-backups/latest; then
  echo "No changes to commit."
  git restore --staged .smartclaw-backups/latest 2>/dev/null || true
  exit 0
fi

git commit -m "chore: backup ~/.smartclaw snapshot $SNAPSHOT_TS" -- .smartclaw-backups/latest/

git fetch --quiet origin main
COMMIT_SHA="$(git rev-parse HEAD)"
REMOTE_URL="$(git remote get-url origin)"
if [ -z "${REMOTE_URL}" ]; then
  echo "No origin remote found; skipping push."
  exit 0
fi
REMOTE_HEAD="$(git rev-parse origin/main)"

if ! git merge-base --is-ancestor "$REMOTE_HEAD" "$COMMIT_SHA"; then
  if git merge-base --is-ancestor "$COMMIT_SHA" "$REMOTE_HEAD"; then
    echo "Local branch is behind origin/main; rebasing before push."
    git pull --rebase origin main
    COMMIT_SHA="$(git rev-parse HEAD)"
  else
    echo "Local and origin/main histories diverged. Aborting push."
    echo "Run: git pull --rebase origin main"
    exit 1
  fi
fi

if ! git push origin "HEAD:main"; then
  echo "Push to origin main failed."
  exit 1
fi

if [[ "$REMOTE_URL" == "git@github.com:"* ]]; then
  REPO_PATH="${REMOTE_URL#git@github.com:}"
  REPO_PATH="${REPO_PATH%.git}"
  COMMIT_URL="https://github.com/${REPO_PATH}/commit/${COMMIT_SHA}"
elif [[ "$REMOTE_URL" == "https://github.com/"* ]]; then
  COMMIT_URL="${REMOTE_URL%.git}/commit/${COMMIT_SHA}"
else
  COMMIT_URL="${REMOTE_URL}"
fi

echo "Backup pushed to remote origin/main: ${COMMIT_URL}"
