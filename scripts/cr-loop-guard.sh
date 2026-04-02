#!/usr/bin/env bash
# cr-loop-guard.sh — Gate @coderabbitai all good? calls to prevent incremental review stalls.
# Usage: cr-loop-guard.sh <owner/repo> <pr_number> fix-mode
# Output: cr-trigger | copilot-expanded | skip
#
# Logic:
#   1. Get current PR head SHA
#   2. Get latest CR formal review commit_id (REST API field, NOT commit_sha)
#   3. If SHA unchanged: check loop count → if >= 3, return "skip"; else "copilot-expanded"
#   4. If SHA changed: reset loop count, return "cr-trigger"

set -euo pipefail

DATA_DIR="${CR_LOOP_GUARD_DATA_DIR:-$HOME/.agent-orchestrator/guard-data}"
MAX_LOOPS="${CR_LOOP_GUARD_MAX_LOOPS:-3}"

usage() {
  echo "Usage: $0 <owner/repo> <pr_number> fix-mode" >&2
  exit 1
}

[[ $# -ge 3 ]] && [[ "${*: -1}" == "fix-mode" ]] || usage

OWNER_REPO="$1"
PR="$2"

OWNER="${OWNER_REPO%%/*}"
REPO="${OWNER_REPO#*/}"
KEY="${OWNER}__${REPO}__${PR}"

LOOP_FILE="$DATA_DIR/loop-counts.json"
mkdir -p "$DATA_DIR"

# ── Helpers ────────────────────────────────────────────────────────────────────
_set_loop_count() {
  local count="$1"
  # Use subshell so mktemp temp file is cleaned up automatically on exit
  if [[ -f "$LOOP_FILE" ]]; then
    (
      tmp=$(mktemp)
      jq --arg key "$KEY" --argjson count "$count" '.[$key] = $count' "$LOOP_FILE" > "$tmp" && mv "$tmp" "$LOOP_FILE"
    )
  else
    (
      tmp=$(mktemp)
      jq -n --arg key "$KEY" --argjson count "$count" '{($key): $count}' > "$tmp" && mv "$tmp" "$LOOP_FILE"
    )
  fi
}

_reset_loop_count() {
  (
    tmp=$(mktemp)
    if [[ -f "$LOOP_FILE" ]]; then
      # jq exits 5 when key not found; capture and ignore it
      jq --arg key "$KEY" 'del(.[$key])' "$LOOP_FILE" > "$tmp" 2>/dev/null && mv "$tmp" "$LOOP_FILE" || mv "$tmp" "$LOOP_FILE"
    fi
  )
}

_get_loop_count() {
  if [[ -f "$LOOP_FILE" ]]; then
    jq -r --arg key "$KEY" '.[$key] // 0' "$LOOP_FILE" 2>/dev/null || echo 0
  else
    echo 0
  fi
}

# ── Step 1: current PR head SHA ────────────────────────────────────────────────
HEAD_SHA=$(gh api repos/"$OWNER"/"$REPO"/pulls/"$PR" --jq '.head.sha')

# ── Step 2: latest CR formal review commit_id ──────────────────────────────────
# IMPORTANT: GitHub REST API uses "commit_id", NOT "commit_sha" (that is a GraphQL field).
# Without "add", --paginate feeds pages one at time to jq, so sort_by operates per-page.
CR_REVIEW=$(gh api repos/"$OWNER"/"$REPO"/pulls/"$PR"/reviews \
  --paginate \
  --jq '[.[] | select(.user.login == "coderabbitai[bot]")] |
        sort_by(.submitted_at) |
        last |
        {commit_id, state}')

CR_ID=$(echo "$CR_REVIEW" | jq -r '.commit_id // "null"')

# null commit_id means CR acknowledged but never formally reviewed this SHA (incremental stall)
if [[ "$CR_ID" == "null" ]] || [[ -z "$CR_ID" ]]; then
  echo "cr-loop-guard: CR review commit_id is null (incremental stall detected)" >&2
  # Increment existing count so repeated null-SHA calls still hit the loop limit.
  # Do NOT reset to 1 — that defeated the entire loop-count enforcement.
  COUNT=$(_get_loop_count)
  _set_loop_count $((COUNT + 1))
  if [[ "$COUNT" -ge "$MAX_LOOPS" ]]; then
    echo "cr-loop-guard: loop limit ($MAX_LOOPS) reached (null SHA) — skipping CR ping" >&2
    echo "skip"
    exit 0
  fi
  echo "cr-trigger"
  exit 0
fi

# ── Step 3: SHA unchanged — check loop count ───────────────────────────────────
if [[ "$HEAD_SHA" == "$CR_ID" ]]; then
  COUNT=$(_get_loop_count)
  if [[ "$COUNT" -ge "$MAX_LOOPS" ]]; then
    echo "cr-loop-guard: loop limit ($MAX_LOOPS) reached for $KEY — skipping CR ping" >&2
    echo "skip"
    exit 0
  fi
  _set_loop_count $((COUNT + 1))
  echo "copilot-expanded"
  exit 0
fi

# ── Step 4: SHA changed — new commits, reset and allow CR ping ─────────────────
_reset_loop_count
echo "cr-trigger"
exit 0
