#!/usr/bin/env bash
# staging-promote.sh — Promote staging branch → main when staging canary passes
#
# Called by: scripts/staging-canary-cron.sh (orch-1ps.3 — cron-triggered after
#            staging PR merges, or after a manual staging-branch merge).
#            Also callable manually after confirming staging is healthy.
#
# Design: 3-stage openclaw dev pipeline (orch-1ps epic).
#   Stage 1: PR → merge to staging branch → ~/.smartclaw-staging/ picks up via worktree
#   Stage 2: This script runs canary against staging gateway; merges staging→main if green
#   Stage 3: CI gate (staging-canary.sh in GHA before merge) — orch-1ps.3
#
# Fail-closed: any canary failure = no promotion.
# Idempotent: safe to run multiple times (canary re-verifies health each run).
set -euo pipefail

# ── Paths ────────────────────────────────────────────────────────────────────
# Note: OPENCLAW_STAGING_DIR is used by staging-canary.sh for the staging config
# dir (~/.smartclaw/staging). This script uses the staging WORKTREE, which is a
# different path (~/.smartclaw-staging/). The env var for this script's worktree
# path is OPENCLAW_STAGING_WORKTREE (not OPENCLAW_STAGING_DIR).
STAGING_WORKTREE="${OPENCLAW_STAGING_WORKTREE:-$HOME/.smartclaw-staging}"
# Backward-compat alias — code below uses STAGING_WORKTREE throughout
STAGING_DIR="$STAGING_WORKTREE"
PROD_DIR="$HOME/.smartclaw"
STAGING_CANARY="${OPENCLAW_STAGING_CANARY:-$HOME/.smartclaw/scripts/staging-canary.sh}"
CANARY_PORT="${OPENCLAW_STAGING_CANARY_PORT:-18790}"

# ── Guard: staging must be a git worktree, not a plain directory ───────────────
# A worktree has .git as a file pointing to the parent repo; a plain dir has no .git
is_worktree() {
    local dir="$1"
    [[ -f "$dir/.git" ]] && git -C "$dir" rev-parse --git-dir > /dev/null 2>&1
}

echo "=== Staging Promote ==="
echo "Timestamp: $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo ""

if [[ ! -d "$STAGING_DIR" ]]; then
    echo "ERROR: Staging directory does not exist: $STAGING_DIR"
    echo "  Create it as a git worktree: git worktree add ~/.smartclaw-staging origin/staging"
    exit 1
fi

if ! is_worktree "$STAGING_DIR"; then
    echo "ERROR: $STAGING_DIR is not a git worktree — refusing to promote."
    echo "  A plain directory (non-worktree) is not a valid staging environment."
    echo "  Remove it and re-add as worktree:"
    echo "    rm -rf $STAGING_DIR"
    echo "    git worktree add $STAGING_DIR origin/staging"
    exit 1
fi

# ── Guard: staging worktree must be on the staging branch ─────────────────────
STAGING_BRANCH=$(git -C "$STAGING_DIR" symbolic-ref --short HEAD 2>/dev/null || echo "")
if [[ "$STAGING_BRANCH" != "staging" ]]; then
    echo "ERROR: Staging worktree is on branch '$STAGING_BRANCH', expected 'staging'."
    exit 1
fi

echo "Staging worktree: $STAGING_DIR (branch: staging)"
echo "Canary target:    port $CANARY_PORT"
echo ""

# ── Step 1: Run staging canary ───────────────────────────────────────────────
echo ">>> Running staging canary (port $CANARY_PORT)..."

if [[ ! -x "$STAGING_CANARY" ]]; then
    echo "ERROR: Staging canary script not found or not executable: $STAGING_CANARY"
    exit 1
fi

# Capture canary output for reporting; canary exits 0 on pass, 1 on failure
CANARY_OUTPUT=$("$STAGING_CANARY" --port "$CANARY_PORT" 2>&1) || {
    CANARY_RC=$?
    echo ""
    echo "Canary failed (exit $CANARY_RC). Output:"
    echo "$CANARY_OUTPUT"
    echo ""
    # Extract failure reason from canary output
    FAIL_REASON=$(echo "$CANARY_OUTPUT" | grep -E "^[[:space:]]+FAIL" | head -1 | sed 's/^[[:space:]]*//' || echo "unknown")
    echo "Canary failed: $FAIL_REASON, not promoting."
    exit 1
}

echo "Canary passed (all 6 checks green)."

# ── Step 2: Merge staging → main in the prod worktree ──────────────────────────
echo ""
echo ">>> Merging staging → main in production (~/.smartclaw/)..."

if [[ ! -d "$PROD_DIR/.git" ]]; then
    echo "ERROR: Production directory $PROD_DIR is not a git repository."
    exit 1
fi

# Verify prod worktree is on main
PROD_BRANCH=$(git -C "$PROD_DIR" symbolic-ref --short HEAD 2>/dev/null || echo "")
if [[ "$PROD_BRANCH" != "main" ]]; then
    echo "ERROR: Production worktree is on branch '$PROD_BRANCH', expected 'main'."
    echo "  staging-promote.sh must be run from the main worktree (~/.smartclaw/)."
    exit 1
fi

# Abort any in-progress merge from a previous failed run before proceeding.
# Idempotent when no merge is in progress (--abort exits 0 with "fatal: no merge is in progress").
git -C "$PROD_DIR" merge --abort 2>/dev/null || true

# Stash any local changes before promoting (direct edits should not be there per CLAUDE.md)
if ! git -C "$PROD_DIR" diff-index --quiet HEAD -- 2>/dev/null; then
    echo "WARNING: $PROD_DIR has uncommitted local changes — stashing before promotion."
    git -C "$PROD_DIR" stash push -m "local changes before staging-promote pull" 2>&1 || {
        echo "ERROR: git stash failed. Resolve manually before re-running."
        exit 1
    }
fi

# Fetch the latest staging branch refs into prod worktree
git -C "$PROD_DIR" fetch origin staging 2>&1 || {
    echo "ERROR: git fetch origin staging failed in prod."
    exit 1
}

# Check if there is anything to merge: is origin/staging ahead of HEAD?
if git -C "$PROD_DIR" merge-base --is-ancestor origin/staging HEAD 2>/dev/null; then
    echo "staging is already merged (origin/staging is ancestor of HEAD) — nothing to do."
else
    # Merge staging into main --no-ff to always produce a merge commit for traceability
    echo "Merging origin/staging into main..."
    git -C "$PROD_DIR" merge --no-ff origin/staging \
        -m "promote staging → main (via staging-promote.sh)" 2>&1 || {
        MERGE_RC=$?
        echo "ERROR: Merge staging→main failed in prod (exit $MERGE_RC). Output above."
        echo "Not promoting."
        exit 1
    }
fi

# Push merged main to origin — this is what updates production (~/.smartclaw/ is already on main)
echo ">>> Pushing merged main to origin..."
git -C "$PROD_DIR" push origin main 2>&1 || {
    PUSH_RC=$?
    echo "ERROR: Failed to push main to origin (exit $PUSH_RC). Output above."
    exit 1
}

echo ""
echo ">>> Promotion complete."
echo ""
echo "Promoted staging to prod at $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo ""
echo "Summary:"
echo "  Staging worktree: $STAGING_DIR (branch: staging)"
echo "  Canary:           PASSED (port $CANARY_PORT, 6/6 checks)"
echo "  Prod directory:   $PROD_DIR"
echo "  Prod branch:      main"
echo ""
echo "Verify prod is healthy:"
echo "  bash $STAGING_CANARY --port 18789"
