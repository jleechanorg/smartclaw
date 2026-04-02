#!/usr/bin/env bash
# ============================================================
# sync-to-smartclaw.sh
# Copies general-purpose content from smartclaw to smartclaw,
# sanitizing jleechan-specific tokens, IDs, and org names.
#
# Usage:
#   GITHUB_ORG=jleechanorg GITHUB_REPO=smartclaw ./sync-to-smartclaw.sh
#
# Env vars:
#   GITHUB_ORG       Target GitHub org (default: jleechanorg)
#   GITHUB_USER      Your GitHub username (default: jleechan)
#   GITHUB_REPO      Target repo name (default: smartclaw)
#   SOURCE_DIR       Path to smartclaw source (default: auto-detect)
#   SLACK_CHANNEL_ID Slack channel for notifications (optional; required for Slack notification)
#   SLACK_BOT_TOKEN  Slack bot token for notifications
#   DRY_RUN          Set to 1 to skip push/PR/Slack steps
# ============================================================
set -euo pipefail

# Require Bash 4+ (for associative arrays and portable process substitution)
if [[ ${BASH_VERSINFO[0]:-0} -lt 4 ]]; then
  echo "ERROR: Bash 4+ required (you have ${BASH_VERSION:-unknown}). On macOS, use: brew install bash" >&2
  exit 1
fi

# Capture gh pr create output robustly: extract URL from stdout, fall back to stderr
capture_pr_url() {
  local org="$1"
  local repo="$2"
  local title="$3"
  local body="$4"
  local output
  output=$(gh pr create --repo "$org/$repo" --title "$title" --body "$body" 2>&1) || true
  # stdout always has the URL on success; fall back to extracting from combined output
  echo "$output" | grep -E '^https://github.com/' | head -1 || echo "$output"
}

GITHUB_ORG="${GITHUB_ORG:-jleechanorg}"
GITHUB_USER="${GITHUB_USER:-jleechan}"
GITHUB_REPO="${GITHUB_REPO:-smartclaw}"
SOURCE_REPO="jleechanorg/smartclaw"
SLACK_CHANNEL_ID="${SLACK_CHANNEL_ID:-}"
DRY_RUN="${DRY_RUN:-}"
AUTO_GENERATE_MAP="${AUTO_GENERATE_MAP:-1}"

SOURCE_DIR="${SOURCE_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
MAP_PATH="${MAP_PATH:-$SOURCE_DIR/scripts/smartclaw-export-map.tsv}"
MAP_UPDATER_PATH="${MAP_UPDATER_PATH:-$SOURCE_DIR/scripts/update-smartclaw-export-map.sh}"
# Always use mktemp — never delete a user-provided directory (TARGET_CLONE_DIR override is ignored)
TARGET_CLONE_DIR=$(mktemp -d "${TMPDIR:-/tmp}/smartclaw-sync.XXXXXX")
BRANCH_NAME="feat/sync-from-smartclaw-$(date +%Y%m%d)"

# Clean up temp clone on exit
trap 'rm -rf "$TARGET_CLONE_DIR"' EXIT

log() { echo "[$(date +%T)] $*" >&2; }
warn() { echo "[$(date +%T)] WARNING: $*" >&2; }

load_sync_map() {
  local map_file="$1"
  local src_path dst_path
  declare -n _target_map="$2"

  if [[ ! -f "$map_file" ]]; then
    echo "ERROR: sync map not found: $map_file" >&2
    return 1
  fi

  while IFS=$'\t' read -r src_path dst_path; do
    [[ -n "$src_path" ]] || continue
    [[ "${src_path:0:1}" == "#" ]] && continue
    [[ -n "$dst_path" ]] || continue
    _target_map["$src_path"]="$dst_path"
  done < "$map_file"

  return 0
}

# ------------------------------------------------------------
# Sanitize a single file or directory tree in-place
# ------------------------------------------------------------
sanitize_file() {
  local file="$1"
  [[ -f "$file" ]] || return 0
  local tmp
  # Use a template so mktemp is portable (BSD/macOS require it)
  tmp=$(mktemp "${file}.sanitize.XXXXXX")
  sed \
    -e "s|jleechanorg/smartclaw|${GITHUB_ORG}/${GITHUB_REPO}|g" \
    -e "s|jleechanorg|${GITHUB_ORG}|g" \
    -e "s|smartclaw|${GITHUB_REPO}|g" \
    -e "s|jleechan|${GITHUB_USER}|g" \
    -e "s|${SLACK_CHANNEL_ID}|\${SLACK_CHANNEL_ID}|g" \
    -e "s|${SLACK_CHANNEL_ID}|\${SLACK_CHANNEL_ID}|g" \
    -e "s|${SLACK_CHANNEL_ID}|\${SLACK_CHANNEL_ID}|g" \
    -e "s|${SLACK_CHANNEL_ID}|\${SLACK_CHANNEL_ID}|g" \
    -e "s|SLACK_BOT_TOKEN|SLACK_BOT_TOKEN|g" \
    -e "s|${GITHUB_USER}|\${GITHUB_USER}|g" \
    -e "s|${HOME}/|\${HOME}/|g" \
    -e "s|/Users/jleechan\$|\${HOME}|g" \
    -e "s|\.smartclaw|\.smartclaw|g" \
    "$file" > "$tmp"
  mv "$tmp" "$file"
}

# ------------------------------------------------------------
# 1. Clone source and target repos
# ------------------------------------------------------------
log "=== Syncing smartclaw → smartclaw ==="
log "Source: $SOURCE_REPO"
log "Target: $GITHUB_ORG/$GITHUB_REPO"

# Clone smartclaw source (shallow, for speed)
if [[ ! -d "$SOURCE_DIR/.git" ]]; then
  warn "Source dir $SOURCE_DIR is not a git repo — SOURCE_DIR may be wrong"
fi

# Clone/update smartclaw
if [[ -d "$TARGET_CLONE_DIR/.git" ]]; then
  log "Using existing clone at $TARGET_CLONE_DIR"
  git -C "$TARGET_CLONE_DIR" fetch origin --tags
else
  log "Cloning $GITHUB_ORG/$GITHUB_REPO → $TARGET_CLONE_DIR"
  git clone "https://github.com/$GITHUB_ORG/$GITHUB_REPO" "$TARGET_CLONE_DIR"
fi

# Checkout or create feature branch (check both local and remote)
if git -C "$TARGET_CLONE_DIR" rev-parse --verify "$BRANCH_NAME" &>/dev/null || \
   git -C "$TARGET_CLONE_DIR" rev-parse --verify "origin/$BRANCH_NAME" &>/dev/null; then
  log "Branch $BRANCH_NAME already exists — checking it out"
  git -C "$TARGET_CLONE_DIR" checkout "$BRANCH_NAME"
  git -C "$TARGET_CLONE_DIR" pull origin "$BRANCH_NAME" 2>/dev/null || true
else
  log "Creating branch $BRANCH_NAME"
  git -C "$TARGET_CLONE_DIR" checkout -b "$BRANCH_NAME"
fi

# ------------------------------------------------------------
# 2. Build sync map (source path -> destination path)
# ------------------------------------------------------------
if [[ "$AUTO_GENERATE_MAP" == "1" ]]; then
  if [[ -x "$MAP_UPDATER_PATH" ]]; then
    log "Refreshing smartclaw export map via $MAP_UPDATER_PATH"
    "$MAP_UPDATER_PATH"
  else
    warn "Map updater is not executable: $MAP_UPDATER_PATH"
  fi
fi

declare -A SYNC_MAP
load_sync_map "$MAP_PATH" SYNC_MAP

if [[ "${#SYNC_MAP[@]}" -eq 0 ]]; then
  echo "ERROR: sync map is empty: $MAP_PATH" >&2
  exit 1
fi
log "Loaded ${#SYNC_MAP[@]} sync entries from $(basename "$MAP_PATH")"

# ------------------------------------------------------------
# 3. Sync files
# ------------------------------------------------------------
for src_path in "${!SYNC_MAP[@]}"; do
  dst_path="${SYNC_MAP[$src_path]}"
  src="$SOURCE_DIR/$src_path"
  dst="$TARGET_CLONE_DIR/$dst_path"

  if [[ ! -e "$src" ]]; then
    log "SKIP (not found): $src_path"
    continue
  fi

  log "Syncing: $src_path → $dst_path"
  mkdir -p "$(dirname "$dst")"

  cp "$src" "$dst"

  # Sanitize the copied content
  if [[ -f "$dst" ]]; then
    sanitize_file "$dst"
    log "  sanitized: $dst_path"
  elif [[ -d "$dst" ]]; then
    while IFS= read -r f; do
      sanitize_file "$f"
    done < <(find "$dst" -type f)
    log "  sanitized directory: $dst_path"
  fi
done

# ------------------------------------------------------------
# 4. Commit to feature branch
# ------------------------------------------------------------
cd "$TARGET_CLONE_DIR"
git add -A

if ! git diff --cached --quiet; then
  git commit -m "$(cat <<'EOF'
feat: sync portable content from smartclaw

- Export files selected by smartclaw portability audit map
- Apply standard sanitization (org/user/slack/path token replacement)
- Keep smartclaw templates aligned with current smartclaw harness docs/scripts
EOF
)"
  log "Committed to $BRANCH_NAME"
else
  log "No changes to commit — nothing new to sync."
  exit 0
fi

# ------------------------------------------------------------
# 5. Push
# ------------------------------------------------------------
if [[ -n "$DRY_RUN" ]]; then
  log "DRY_RUN=1 — skipping push and PR creation"
  log "Would push branch: $BRANCH_NAME"
  git -C "$TARGET_CLONE_DIR" log --oneline -3
  exit 0
fi

log "Pushing $BRANCH_NAME to origin..."
git push -u origin "$BRANCH_NAME"

# ------------------------------------------------------------
# 6. Create PR
# ------------------------------------------------------------
log "Creating PR on $GITHUB_ORG/$GITHUB_REPO..."

PR_URL=$(capture_pr_url "$GITHUB_ORG" "$GITHUB_REPO" \
  "[P2] feat: sync general-purpose content from smartclaw" \
  "$(cat <<'EOF'
## Summary
Syncs general-purpose, non-personal content from jleechanorg/smartclaw to this repo.

## What was synced
- **Docs**: HARNESS_ENGINEERING, ZERO_TOUCH
- **Workflows**: skeptic-cron.yml, coderabbit-ping-on-push.yml
- **Launchd plist templates**: lifecycle-manager, health-check, monitor-agent, scheduler, agento-manager
- **Skills**: er (evidence review), dispatch-task, cmux, antigravity-computer-use, claude-code-computer-use

## Sanitization applied
| Pattern | Replacement |
|---------|-------------|
| `jleechanorg` | `$GITHUB_ORG` |
| `smartclaw` | `$GITHUB_REPO` |
| `jleechanorg/smartclaw` | `$GITHUB_ORG/$GITHUB_REPO` |
| `jleechan` | `$GITHUB_USER` |
| `${SLACK_CHANNEL_ID}`, `${SLACK_CHANNEL_ID}`, etc. | `$SLACK_CHANNEL_ID` |
| `~/.smartclaw` | `~/.smartclaw` |
| `${HOME}/` or `/Users/jleechan` | `$HOME/` or `$HOME` |
| `SLACK_BOT_TOKEN` | `SLACK_BOT_TOKEN` |

## Testing
Manual review only — no automated tests on this seed PR.

## ⚠️ DO NOT AUTO-MERGE
This PR requires HUMAN REVIEW before merge.
EOF
)")
if [[ "$PR_URL" != http* ]]; then
  warn "gh pr create returned non-URL output: $PR_URL"
fi
log "PR created: $PR_URL"

# ------------------------------------------------------------
# 7. Slack notification
# ------------------------------------------------------------
if [[ -n "${SLACK_BOT_TOKEN:-}" ]] || [[ -n "${SLACK_BOT_TOKEN:-}" ]]; then
  if [[ -z "${SLACK_CHANNEL_ID:-}" ]]; then
    warn "SLACK_CHANNEL_ID is empty — set it to notify Slack on PR creation"
  else
    BOT_TOKEN="${SLACK_BOT_TOKEN:-${SLACK_BOT_TOKEN}}"
    SLACK_TEXT="[AI Terminal: ao-spawn] smartclaw sync PR ready for HUMAN REVIEW (do not auto-merge): $PR_URL — sanitized content from smartclaw (notify: ${SLACK_CHANNEL_ID:-<set SLACK_CHANNEL_ID>})"
    SLACK_RESP=$(curl -s -X POST "https://slack.com/api/chat.postMessage" \
      -H "Authorization: Bearer $BOT_TOKEN" \
      -H "Content-Type: application/json" \
      -d "$(jq -n --arg channel "$SLACK_CHANNEL_ID" --arg text "$SLACK_TEXT" \
        '{channel: $channel, text: $text}')")
    if echo "$SLACK_RESP" | jq -e '.ok == true' &>/dev/null; then
      log "Slack notification sent."
    else
      warn "Slack notification failed: $SLACK_RESP"
    fi
  fi
else
  warn "No SLACK_BOT_TOKEN set — skipping Slack notification"
  log "To notify manually: post the PR URL to your Slack channel"
fi

log "=== Done ==="
log "PR: $PR_URL"
log "Clone will be cleaned up on script exit"
