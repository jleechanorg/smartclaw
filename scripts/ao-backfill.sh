#!/usr/bin/env bash
# ao-backfill.sh — auto-spawn AO sessions for [agento]-tagged PRs (or all PRs if backfillAllPRs is enabled)
#
# Runs every 15min via launchd (ai.agento.backfill).
# For each managed repo in agent-orchestrator.yaml:
#   - Find open PRs with [agento] in the title (or all open PRs if backfillAllPRs is enabled)
#   - Check if AO already has a session claiming that PR
#   - If not → ao spawn <project> --claim-pr <N>

set -euo pipefail

AO_CONFIG="${AO_CONFIG_PATH:-$HOME/agent-orchestrator.yaml}"
AO_BIN="${AO_BIN:-${HOME}/bin/ao}"
LOG_FILE="/tmp/ao-backfill.log"

# Check for flock availability
USE_FLOCK=true
command -v flock >/dev/null 2>&1 || USE_FLOCK=false

log() { echo "$(date '+%Y-%m-%dT%H:%M:%S') $*" | tee -a "$LOG_FILE"; }

export AO_CONFIG_PATH="$AO_CONFIG"

log "ao-backfill: starting"

# Extract repo→projectId→defaultBranch→backfillAllPRs mappings from agent-orchestrator.yaml
# Format: tab-separated "owner/repo\tprojectId\tdefaultBranch\tbackfillAllPRs"
# (tabs prevent field corruption when defaultBranch is empty)
MAPPINGS=$(python3 - <<'EOF'
import yaml, sys, os
path = os.environ.get('AO_CONFIG_PATH')
with open(path) as f:
    config = yaml.safe_load(f)
for pid, proj in config.get('projects', {}).items():
    repo = proj.get('repo', '')
    branch = proj.get('defaultBranch', '')
    all_prs = 'true' if proj.get('backfillAllPRs', False) else 'false'
    if repo:
        print(f"{repo}\t{pid}\t{branch}\t{all_prs}")
EOF
)

if [ -z "$MAPPINGS" ]; then
    log "ao-backfill: no projects found in config"
    exit 0
fi

# Prune stale worktrees for each managed repo to unblock branch checkouts
log "ao-backfill: pruning stale worktrees"
while IFS=$'\t' read -r REPO PROJECT_ID DEFAULT_BRANCH ALL_PRS; do
    REPO_PATH=$(AO_CONFIG_ARG="${AO_CONFIG}" PROJECT_ID_ARG="${PROJECT_ID}" python3 -c "
import yaml, os
with open(os.environ['AO_CONFIG_ARG']) as f:
    c = yaml.safe_load(f)
proj = c.get('projects', {}).get(os.environ['PROJECT_ID_ARG'], {})
print(os.path.expanduser(proj.get('path', '')))
" 2>/dev/null || echo "")
    if [[ -n "$REPO_PATH" && (-d "$REPO_PATH/.git" || -f "$REPO_PATH/.git") ]]; then
        git -C "$REPO_PATH" worktree prune 2>/dev/null || true
    fi
done <<< "$MAPPINGS"

# Get current AO session list (claimed PR numbers per session)
AO_SESSIONS=$("$AO_BIN" session ls 2>/dev/null || echo "")

# Build per-repo PR list once (avoid repeated gh calls for repos with multiple projects)
# Key: "repo:all_prs_flag" to support same repo with different filter settings
declare -A REPO_PRS
while IFS=$'\t' read -r REPO PROJECT_ID DEFAULT_BRANCH ALL_PRS; do
    REPO_KEY="${REPO}:${ALL_PRS}"
    if [[ -z "${REPO_PRS[$REPO_KEY]+x}" ]]; then
        if [[ "$ALL_PRS" == "true" ]]; then
            # Handle all open PRs for this project (not just [agento]-tagged)
            REPO_PRS[$REPO_KEY]=$(gh pr list \
                --repo "$REPO" \
                --state open \
                --limit 1000 \
                --json number,title,headRefName \
                --jq '.[] | "\(.number) \(.headRefName)"' \
                2>/dev/null || echo "")
        else
            # Default: only [agento]-tagged PRs
            REPO_PRS[$REPO_KEY]=$(gh pr list \
                --repo "$REPO" \
                --state open \
                --limit 1000 \
                --json number,title,headRefName \
                --jq '[.[] | select(.title | startswith("[agento]"))] | .[] | "\(.number) \(.headRefName)"' \
                2>/dev/null || echo "")
        fi
    fi
done <<< "$MAPPINGS"

# Process: spawn sessions for PRs in each project with no active session
while IFS=$'\t' read -r REPO PROJECT_ID DEFAULT_BRANCH ALL_PRS; do
    REPO_KEY="${REPO}:${ALL_PRS}"
    PRS="${REPO_PRS[$REPO_KEY]:-}"
    [[ -z "$PRS" ]] && continue

    while IFS=' ' read -r PR_NUM PR_BRANCH; do
        [[ -z "$PR_NUM" ]] && continue

        PR_URL="$REPO/pull/$PR_NUM"
        if echo "$AO_SESSIONS" | grep -q "$PR_URL"; then
            log "ao-backfill: PR #$PR_NUM ($PR_BRANCH) already has a session — skipping"
            continue
        fi

        log "ao-backfill: spawning for PR #$PR_NUM branch=$PR_BRANCH project=$PROJECT_ID"
        PR_TASK_MSG="You own PR #$PR_NUM (branch: $PR_BRANCH) in $REPO. Goal: make this PR green (CI passing + all review comments resolved + CodeRabbit APPROVE). NEVER run 'gh pr merge' — do NOT merge the PR yourself. Steps: 1) Run: gh pr view $PR_NUM --repo $REPO --comments to read all PR comments. 2) Run: gh api repos/$REPO/pulls/$PR_NUM/reviews to see all bot reviews including CodeRabbit actionable items. 3) Run: gh api repos/$REPO/pulls/$PR_NUM/comments to see inline review comments. 4) Fix EVERY actionable comment and CI failure — edit code, run tests locally if possible, push. 5) Only AFTER all comments are fixed, post: @coderabbitai all good? 6) If there are merge conflicts, rebase on the default branch first. 7) When all 4 green conditions pass (CI green, MERGEABLE, no unresolved comments, CR APPROVE), post a PR comment: 'PR is green — awaiting human merge review.' Then stop — the human merges."

        SPAWN_OUTPUT=$("$AO_BIN" spawn "$PROJECT_ID" --claim-pr "$PR_NUM" 2>&1 | tee -a "$LOG_FILE") || true
        # Extract session id whether --claim-pr succeeded or failed with "Session X was created"
        SESSION=$(echo "$SPAWN_OUTPUT" | grep -oE 'SESSION=[a-z]+-[0-9]+' | head -1 | cut -d= -f2 || echo "")
        if [[ -z "$SESSION" ]]; then
            # Fallback: parse "Session wa-N was created" from error output
            SESSION=$(echo "$SPAWN_OUTPUT" | grep -oE 'Session [a-z]+-[0-9]+ was created' | grep -oE '[a-z]+-[0-9]+' | head -1 || echo "")
        fi
        if [[ -n "$SESSION" ]]; then
            "$AO_BIN" send "$SESSION" "$PR_TASK_MSG" 2>&1 | tee -a "$LOG_FILE" || true
            log "ao-backfill: spawned $SESSION for PR #$PR_NUM and sent task"
        else
            log "ao-backfill: spawn fully failed for PR #$PR_NUM, no session created"
        fi
    done <<< "$PRS"
done <<< "$MAPPINGS"

# --- PASS 1: Merged PR cleanup (orch-ryk) ---
# Stop sessions for projects whose default branch PR has been merged
log "ao-backfill: checking for merged PRs to cleanup"
while IFS=$'\t' read -r REPO PROJECT_ID DEFAULT_BRANCH ALL_PRS; do
    # Check if there's a PR from default branch (merged/closed) using list --head (view takes number/URL)
    PR_STATE=$(gh pr list --repo "$REPO" --head "$DEFAULT_BRANCH" --state all \
        --json state,merged --jq '.[0] | .state + " " + (.merged | tostring)' 2>/dev/null || echo "unknown false")
    if [[ "$PR_STATE" == "MERGED true" ]]; then
        log "ao-backfill: PR for $PROJECT_ID merged — stopping session"
        "$AO_BIN" stop "$PROJECT_ID" 2>/dev/null || true
    elif [[ "$PR_STATE" == "CLOSED"* ]]; then
        log "ao-backfill: PR for $PROJECT_ID closed — stopping session"
        "$AO_BIN" stop "$PROJECT_ID" 2>/dev/null || true
    fi
done <<< "$MAPPINGS"

# --- PASS 2: Liveness respawn (orch-455) ---
# Find stuck/killed sessions older than 30min and respawn
AO_BASE="${HOME}/.agent-orchestrator"
STUCK_AGE_MINUTES=30

log "ao-backfill: checking for stuck/killed sessions to respawn"
while IFS=$'\t' read -r REPO PROJECT_ID DEFAULT_BRANCH ALL_PRS; do
    # Find machine prefix for this project
    MACHINE_PREFIX=$(ls "$AO_BASE" 2>/dev/null | (grep -E "^[0-9a-f]{12}-${PROJECT_ID}$" || true) | cut -d- -f1 | head -1)
    if [[ -z "$MACHINE_PREFIX" ]]; then
        continue
    fi

    SESSIONS_DIR="$AO_BASE/${MACHINE_PREFIX}-${PROJECT_ID}/sessions"
    if [[ ! -d "$SESSIONS_DIR" ]]; then
        continue
    fi

    # Sessions are plain key=value files (not subdirs) at $SESSIONS_DIR/<session-id>
    for SESSION_FILE in "$SESSIONS_DIR"/*; do
        [[ -f "$SESSION_FILE" ]] || continue
        SESSION_ID=$(basename "$SESSION_FILE")
        # Skip archive dir and orchestrator sessions
        [[ "$SESSION_ID" == "archive" ]] && continue
        ROLE=$(grep -E "^role=" "$SESSION_FILE" 2>/dev/null | cut -d= -f2 || echo "")
        [[ "$ROLE" == "orchestrator" ]] && continue

        # Read session metadata fields
        STATUS=$(grep -E "^status=" "$SESSION_FILE" 2>/dev/null | cut -d= -f2 || echo "")
        CREATED=$(grep -E "^createdAt=" "$SESSION_FILE" 2>/dev/null | cut -d= -f2 || echo "")
        PR_CLAIM=$(grep -E "^pr=" "$SESSION_FILE" 2>/dev/null | cut -d= -f2 || echo "")

        # Check if stuck/killed and older than threshold
        if [[ "$STATUS" == "stuck" || "$STATUS" == "killed" ]]; then
            if [[ -n "$CREATED" ]]; then
                SESSION_EPOCH=$(date -j -f "%Y-%m-%dT%H:%M:%S" "${CREATED%%.*}" +%s 2>/dev/null \
                  || date -d "${CREATED%%.*}" +%s 2>/dev/null \
                  || echo "0")
                NOW_EPOCH=$(date +%s)
                AGE_MINUTES=$(( (NOW_EPOCH - SESSION_EPOCH) / 60 ))
                if [[ "$AGE_MINUTES" -ge "$STUCK_AGE_MINUTES" ]]; then
                    log "ao-backfill: found $STATUS session $SESSION_ID (${AGE_MINUTES}min old) for $PROJECT_ID"

                    # Clear pr field — stuck/killed not in STALE_PR_OWNERSHIP_STATUSES so AO won't auto-demote
                    if [[ -n "$PR_CLAIM" ]]; then
                        sed -i '' 's/^pr=.*/pr=/' "$SESSION_FILE" 2>/dev/null || true
                        log "ao-backfill: cleared pr field from $SESSION_ID"
                    fi
                    # Mark as respawned so this session is not picked up again on the next cron run
                    sed -i '' 's/^status=.*/status=respawned/' "$SESSION_FILE" 2>/dev/null || true

                    # Only respawn if the session had a PR claim with [agento] in title (or backfillAllPRs is enabled)
                    if [[ "$PR_CLAIM" =~ /([0-9]+)$ ]]; then
                        PR_NUM="${BASH_REMATCH[1]}"
                        PR_TITLE=$(gh api "repos/${REPO}/pulls/${PR_NUM}" --jq '.title' 2>/dev/null || echo "")
                        if [[ "$ALL_PRS" == "true" || "$PR_TITLE" == \[agento\]* ]]; then
                            REASON='[agento] confirmed'
                            [[ "$ALL_PRS" == "true" ]] && REASON='backfillAllPRs=true'
                            log "ao-backfill: respawning $PROJECT_ID with --claim-pr $PR_NUM ($REASON)"
                            "$AO_BIN" spawn "$PROJECT_ID" --claim-pr "$PR_NUM" 2>&1 | tee -a "$LOG_FILE" || true
                        else
                            log "ao-backfill: skipping respawn of $SESSION_ID — PR #$PR_NUM has no [agento] prefix"
                        fi
                    else
                        log "ao-backfill: skipping respawn of $SESSION_ID — no PR claim (no-op sessions not respawned)"
                    fi
                fi
            fi
        fi
    done
done <<< "$MAPPINGS"

# --- PASS 3: Idle-session poke ---
# Find AO sessions that are running (tmux alive) but idle at a shell prompt,
# where the PR still has open review comments. Send a poke to restart work.
IDLE_AGE_MINUTES=15

log "ao-backfill: pass 3 — checking for idle sessions with open PR comments"
while IFS=$'\t' read -r REPO PROJECT_ID DEFAULT_BRANCH ALL_PRS; do
    MACHINE_PREFIX=$(ls "$AO_BASE" 2>/dev/null | (grep -E "^[0-9a-f]{12}-${PROJECT_ID}$" || true) | cut -d- -f1 | head -1)
    [[ -z "$MACHINE_PREFIX" ]] && continue

    SESSIONS_DIR="$AO_BASE/${MACHINE_PREFIX}-${PROJECT_ID}/sessions"
    [[ -d "$SESSIONS_DIR" ]] || continue

    for SESSION_FILE in "$SESSIONS_DIR"/*; do
        [[ -f "$SESSION_FILE" ]] || continue
        SESSION_ID=$(basename "$SESSION_FILE")
        [[ "$SESSION_ID" == "archive" ]] && continue
        ROLE=$(grep -E "^role=" "$SESSION_FILE" 2>/dev/null | cut -d= -f2 || echo "")
        [[ "$ROLE" == "orchestrator" ]] && continue

        STATUS=$(grep -E "^status=" "$SESSION_FILE" 2>/dev/null | cut -d= -f2 || echo "")
        PR_CLAIM=$(grep -E "^pr=" "$SESSION_FILE" 2>/dev/null | cut -d= -f2 || echo "")
        CREATED=$(grep -E "^createdAt=" "$SESSION_FILE" 2>/dev/null | cut -d= -f2 || echo "")
        UPDATED=$(grep -E "^updatedAt=" "$SESSION_FILE" 2>/dev/null | cut -d= -f2 || echo "")

        # Only poke running sessions with a PR claim
        [[ "$STATUS" != "running" ]] && continue
        [[ -z "$PR_CLAIM" ]] && continue
        [[ "$PR_CLAIM" =~ /([0-9]+)$ ]] || continue
        PR_NUM="${BASH_REMATCH[1]}"

        # Check session age since last update — skip recently-active sessions
        # Use UPDATED if available, otherwise fall back to CREATED (for new sessions without updatedAt yet)
        TIMESTAMP="${UPDATED:-$CREATED}"
        if [[ -n "$TIMESTAMP" ]]; then
            TIMESTAMP_EPOCH=$(date -j -f "%Y-%m-%dT%H:%M:%S" "${TIMESTAMP%%.*}" +%s 2>/dev/null \
              || date -d "${TIMESTAMP%%.*}" +%s 2>/dev/null \
              || echo "0")
            NOW_EPOCH=$(date +%s)
            IDLE_MINUTES=$(( (NOW_EPOCH - TIMESTAMP_EPOCH) / 60 ))
            [[ "$IDLE_MINUTES" -lt "$IDLE_AGE_MINUTES" ]] && continue
        fi

        # Check if PR still open with unresolved inline comments
        # Use GraphQL to filter to only unresolved threads (REST API includes resolved comments)
        PR_STATE=$(gh api "repos/${REPO}/pulls/${PR_NUM}" --jq '.state' 2>/dev/null || echo "")
        [[ "$PR_STATE" != "open" ]] && continue
        UNRESOLVED_THREADS=$(gh api graphql -F owner="${REPO%%/*}" -F name="${REPO##*/}" -F pr="$PR_NUM" -f query='
          query($owner: String!, $name: String!, $pr: Int!) {
            repository(owner: $owner, name: $name) {
              pullRequest(number: $pr) {
                reviewThreads(first: 50) {
                  nodes { isResolved }
                }
              }
            }
          }
        ' --jq '[.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved == false) | .] | length' 2>/dev/null || echo "0")
        [[ "$UNRESOLVED_THREADS" -eq 0 ]] && continue

        # Rate-limit pokes: skip if already poked within the last 60 minutes
        # Use flock to serialize concurrent ao-backfill runs and prevent TOCTOU race
        # Note: use flag variable instead of continue inside subshell (continue only works in loops, not subshells)
        SKIP_POKE=0
        if [[ "$USE_FLOCK" == "true" ]]; then
            (
            flock -n 9 || { log "ao-backfill: could not acquire lock for $SESSION_FILE — skipping"; exit 1; }
            
            LAST_POKED=$(grep -E "^pokedAt=" "$SESSION_FILE" 2>/dev/null | cut -d= -f2 || echo "")
            if [[ -n "$LAST_POKED" ]]; then
                POKED_EPOCH=$(date -j -f "%Y-%m-%dT%H:%M:%S" "${LAST_POKED%%.*}" +%s 2>/dev/null \
                  || date -d "${LAST_POKED%%.*}" +%s 2>/dev/null \
                  || echo "0")
                NOW_EPOCH=$(date +%s)
                SINCE_POKE=$(( (NOW_EPOCH - POKED_EPOCH) / 60 ))
                if [[ "$SINCE_POKE" -lt 60 ]]; then
                    log "ao-backfill: skipping poke for $SESSION_ID — poked ${SINCE_POKE}min ago (rate limit: 60min)"
                    exit 1
                fi
            fi

            # Check if tmux session is alive and idle (pane ends with shell prompt)
            TMUX_SESSION="${MACHINE_PREFIX}-${SESSION_ID}"
            if tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
                PANE_TEXT=$(tmux capture-pane -t "$TMUX_SESSION" -p 2>/dev/null | tail -3)
                if echo "$PANE_TEXT" | grep -qE '[❯$#] *$'; then
                    log "ao-backfill: poking idle session $SESSION_ID for PR #$PR_NUM ($UNRESOLVED_THREADS unresolved threads)"
                    tmux send-keys -t "$TMUX_SESSION" "You appear to be stuck. Review your current task: check PR status (gh pr view), list unresolved review comments, check CI failures, and decide your next concrete action. If everything is done, post a PR comment summarizing what you completed." Enter
                    if grep -q "^pokedAt=" "$SESSION_FILE" 2>/dev/null; then
                        # Use portable sed: write to temp file and mv (works on BSD and GNU)
                        sed -e "s/^pokedAt=.*/pokedAt=$(date -u +%Y-%m-%dT%H:%M:%S)/" "$SESSION_FILE" > "$SESSION_FILE.tmp" && mv "$SESSION_FILE.tmp" "$SESSION_FILE"
                    else
                        echo "pokedAt=$(date -u +%Y-%m-%dT%H:%M:%S)" >> "$SESSION_FILE"
                    fi
                fi
            fi
            ) 9>"$SESSION_FILE.lock"
            POKE_STATUS=$?
            if [[ $POKE_STATUS -ne 0 ]]; then
                continue
            fi
        fi  # USE_FLOCK
    done
done <<< "$MAPPINGS"

# --- PASS 3.5: Merge gate enforcement (orch-hryp) ---
# Scan all open PRs, check 6 green criteria, post BLOCKED comments for non-compliant PRs
# Also post CHANGES_REQUESTED review to make branch protection actually block

log "ao-backfill: pass 3.5 — merge gate enforcement"

# Get authenticated user for review posting/dismissal
GH_USER=$(gh api user --jq '.login' 2>/dev/null || echo "openclaw[bot]")

while IFS=$'\t' read -r REPO PROJECT_ID DEFAULT_BRANCH ALL_PRS; do
    # Get all open PRs — on failure, log and skip this repo (don't silently proceed with empty list)
    _GH_ERR_FILE=$(mktemp /tmp/ao-backfill-gh-err.XXXXXX)
    PR_LIST=$(gh pr list --repo "$REPO" --state open --limit 100 --json number --jq '.[] | .number' 2>"$_GH_ERR_FILE") || {
        log "ao-backfill: WARN: gh pr list failed for $REPO (skipping): $(cat "$_GH_ERR_FILE")"
        rm -f "$_GH_ERR_FILE"
        continue
    }
    rm -f "$_GH_ERR_FILE"
    [[ -z "$PR_LIST" ]] && continue
    
    for PR_NUM in $PR_LIST; do
        # Skip if already merged
        PR_STATE=$(gh api "repos/${REPO}/pulls/${PR_NUM}" --jq '.state' 2>/dev/null || echo "")
        [[ "$PR_STATE" != "open" ]] && continue
        
        # Collect failing criteria
        FAILING_CRITERIA=""
        
        # Check 1: CI green
        CI_STATUS=$(gh pr checks "$PR_NUM" --repo "$REPO" \
            --json status,conclusion \
            --jq '[.[] | select(.status == "COMPLETED")] | if length == 0 then "pending" else (if all(.conclusion == "SUCCESS" or .conclusion == "NEUTRAL" or .conclusion == "SKIPPED") then "pass" else "fail" end) end' \
            2>/dev/null || echo "unknown")
        [[ "$CI_STATUS" != "pass" ]] && FAILING_CRITERIA="${FAILING_CRITERIA}
1. CI not passing (${CI_STATUS})"
        
        # Check 2: MERGEABLE
        MERGEABLE=$(gh api "repos/${REPO}/pulls/${PR_NUM}" --jq '.mergeable' 2>/dev/null || echo "UNKNOWN")
        [[ "$MERGEABLE" != "true" ]] && FAILING_CRITERIA="${FAILING_CRITERIA}
2. PR has merge conflicts"
        
        # Check 3: unresolved threads = 0
        UNRESOLVED=$(gh api graphql -F owner="${REPO%%/*}" -F name="${REPO##*/}" -F pr="$PR_NUM" -f query='
          query($owner: String!, $name: String!, $pr: Int!) {
            repository(owner: $owner, name: $name) {
              pullRequest(number: $pr) {
                reviewThreads(first: 50) { nodes { isResolved } }
              }
            }
          }
        ' --jq '[.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved == false) | .] | length' 2>/dev/null || echo "1")
        [[ "$UNRESOLVED" != "0" ]] && FAILING_CRITERIA="${FAILING_CRITERIA}
3. ${UNRESOLVED} unresolved review thread(s)"
        
        # Check 4: CR approved/not blocking (fail-closed: NONE means CR hasn't reviewed yet)
        CR_STATE=$(gh api "repos/${REPO}/pulls/${PR_NUM}/reviews" \
            --jq '[.[] | select(.user.login == "coderabbitai[bot]")] | last | .state' 2>/dev/null || echo "NONE")
        # Fail-closed for missing/empty CR states to prevent accidental merges.
        # Allow COMMENTED while still respecting CHANGES_REQUESTED and missing CR.
        if [[ "$CR_STATE" == "NONE" || "$CR_STATE" == "CHANGES_REQUESTED" ]]; then
            FAILING_CRITERIA="${FAILING_CRITERIA}
4. CodeRabbit review is blocking or missing (state: ${CR_STATE})"
        fi
        
        # Check 5: Evidence PASS comment (if evidence files exist)
        HAS_EVIDENCE=$(gh api "repos/${REPO}/pulls/${PR_NUM}/files" --jq '[.[] | select(.filename | test("^evidence|testing_"; "i"))] | length' 2>/dev/null || echo "0")
        if [[ "$HAS_EVIDENCE" -gt 0 ]]; then
            EVIDENCE_PASS=$(gh api "repos/${REPO}/issues/${PR_NUM}/comments" --jq '[.[] | .body] | map(select(test("(?:evidence|/er).*(\\*\\*PASS\\*\\*|✅)"; "i"))) | length' 2>/dev/null || echo "0")
            [[ "${EVIDENCE_PASS:-0}" -lt 1 ]] && FAILING_CRITERIA="${FAILING_CRITERIA}
5. No evidence PASS comment"
        fi
        
        # Check 6: OpenClaw LLM review approved
        OPENCLAW_REVIEW=$(gh api "repos/${REPO}/pulls/${PR_NUM}/reviews" \
            --jq '[.[] | select(.user.login == "openclaw[bot]")] | last | .state' 2>/dev/null || echo "NONE")
        [[ "$OPENCLAW_REVIEW" != "APPROVED" ]] && FAILING_CRITERIA="${FAILING_CRITERIA}
6. OpenClaw LLM review not approved (state: ${OPENCLAW_REVIEW})"
        
        # If any criteria failing, post BLOCKED comment and CHANGES_REQUESTED review
        if [[ -n "$FAILING_CRITERIA" ]]; then
            # Check if we already posted this recently (within last 2 hours)
            LAST_BLOCKED=$(gh api "repos/${REPO}/issues/${PR_NUM}/comments" \
                --jq '[.[] | select(.body | startswith("**MERGE BLOCKED**"))] | last | .created_at' 2>/dev/null || echo "")
            BLOCK_NOW=true
            if [[ -n "$LAST_BLOCKED" ]]; then
                LAST_TIME=$(date -j -f "%Y-%m-%dT%H:%M:%SZ" "$LAST_BLOCKED" +%s 2>/dev/null \
                  || date -d "$LAST_BLOCKED" +%s 2>/dev/null \
                  || echo "0")
                NOW_TIME=$(date +%s)
                [[ $((NOW_TIME - LAST_TIME)) -lt 7200 ]] && BLOCK_NOW=false  # 2 hours
            fi
            
            if [[ "$BLOCK_NOW" == "true" ]]; then
                # Post BLOCKED comment
                gh pr comment "$PR_NUM" --repo "$REPO" --body "**MERGE BLOCKED** — The following criteria are not met:

${FAILING_CRITERIA}Please address these issues before the PR can be merged." 2>/dev/null || true
                
                # Post CHANGES_REQUESTED review to trigger branch protection
                gh pr review "$PR_NUM" --repo "$REPO" --body "Merge blocked: criteria not met" --request-changes 2>/dev/null || true
                
                log "ao-backfill: posted MERGE BLOCKED for PR #$PR_NUM in $REPO"
            fi
        else
            # All criteria pass - dismiss any blocking review
            CURRENT_REVIEW=$(gh api "repos/${REPO}/pulls/${PR_NUM}/reviews" \
                --jq "[.[] | select(.user.login == \"$GH_USER\")] | last | .state" 2>/dev/null || echo "NONE")
            if [[ "$CURRENT_REVIEW" == "CHANGES_REQUESTED" ]]; then
                # Dismiss the blocking review
                REVIEW_ID=$(gh api "repos/${REPO}/pulls/${PR_NUM}/reviews" \
                    --jq "[.[] | select(.user.login == \"$GH_USER\")] | last | .id" 2>/dev/null || echo "")
                if [[ -n "$REVIEW_ID" ]]; then
                    gh api "repos/${REPO}/pulls/${PR_NUM}/reviews/${REVIEW_ID}/dismissals" \
                        --method PUT -f message="All criteria met" 2>/dev/null || true
                    log "ao-backfill: dismissed blocking review for PR #$PR_NUM in $REPO"
                fi
            fi
        fi
    done
done <<< "$MAPPINGS"

# --- PASS 4: Auto-merge execution (orch-xa12, orch-q6ni.1) ---
# Uses unified Python merge gate (check_merge_ready) for all 7 conditions:
#   1. CI green  2. MERGEABLE  3. CR approved  4. No blocking comments
#   5. Inline threads resolved  6. Evidence PASS  7. OpenClaw LLM review
# Then merge using gh pr merge --squash --auto

log "ao-backfill: pass 4 — auto-merge execution"
while IFS=$'\t' read -r REPO PROJECT_ID DEFAULT_BRANCH ALL_PRS; do
    # Find open PRs that may be ready for auto-merge
    # Only process PRs with [agento] prefix (or all if backfillAllPRs is enabled)
    if [[ "$ALL_PRS" == "true" ]]; then
        PR_LIST=$(gh pr list \
            --repo "$REPO" \
            --state open \
            --limit 500 \
            --json number,title,mergeStateStatus \
            --jq '.[] | select(.mergeStateStatus == "CLEAN") | .number' \
            2>/dev/null || echo "")
    else
        PR_LIST=$(gh pr list \
            --repo "$REPO" \
            --state open \
            --limit 500 \
            --json number,title,mergeStateStatus \
            --jq '.[] | select(.title | startswith("[agento]")) | select(.mergeStateStatus == "CLEAN") | .number' \
            2>/dev/null || echo "")
    fi

    [[ -z "$PR_LIST" ]] && continue

    for PR_NUM in $PR_LIST; do
        # Skip if already merged
        PR_STATE=$(gh api "repos/${REPO}/pulls/${PR_NUM}" --jq '.state' 2>/dev/null || echo "")
        [[ "$PR_STATE" != "open" ]] && continue

        # Run unified Python merge gate — checks all 7 conditions in one call
        # Run from current directory to support worktrees
        GATE_OUTPUT=$( \
          OWNER="${REPO%%/*}" REPONAME="${REPO##*/}" PRNUM="$PR_NUM" \
          PYTHONPATH=src python3 -c "
import os, json
from orchestration.merge_gate import check_merge_ready
v = check_merge_ready(os.environ['OWNER'], os.environ['REPONAME'], int(os.environ['PRNUM']))
print(json.dumps({'can_merge': v.can_merge, 'reasons': v.blocked_reasons}))
" 2>/dev/null || echo '{"can_merge": false, "reasons": ["Python gate error"]}')

        CAN_MERGE=$(echo "$GATE_OUTPUT" | jq -r '.can_merge' 2>/dev/null || echo "false")
        if [[ "$CAN_MERGE" != "true" ]]; then
            REASONS=$(echo "$GATE_OUTPUT" | jq -r '.reasons | join("; ")' 2>/dev/null || echo "unknown")
            log "ao-backfill: BLOCKED PR #$PR_NUM — $REASONS"
            continue
        fi

        log "ao-backfill: all 7 conditions pass for PR #$PR_NUM in $REPO — executing merge"

        # Add 'auto-merged' label before merging (create if doesn't exist)
        gh label create "auto-merged" --repo "$REPO" --color 0075ca --description "Merged automatically by orchestrator" 2>/dev/null || true
        gh pr edit "$PR_NUM" --repo "$REPO" --add-label "auto-merged" 2>/dev/null || true
        
        if gh pr merge "$PR_NUM" --repo "$REPO" --squash --auto 2>&1; then
            log "ao-backfill: successfully merged PR #$PR_NUM in $REPO"
        else
            log "ao-backfill: merge failed for PR #$PR_NUM in $REPO"
        fi
    done
done <<< "$MAPPINGS"

log "ao-backfill: done"
