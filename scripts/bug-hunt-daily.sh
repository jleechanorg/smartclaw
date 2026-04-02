#!/bin/bash
# Daily bug hunt job - runs at 9am
# Spawns multiple agents to find bugs in PRs merged in the last 2 days
# Creates bug reports, beads, and posts to #bug-hunt channel

set -euo pipefail

# Configuration - store bug reports in repo for version control
# Note: ~/.smartclaw IS the git repo (jleechanorg/smartclaw), so this works
# for both direct checkout usage AND installed copy (which lives at ~/.smartclaw/scripts/)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BUG_REPORTS_DIR="${REPO_ROOT}/bug_reports"
REPOS=("jleechanorg/smartclaw" "jleechanorg/worldarchitect.ai" "jleechanorg/ai_universe" "jleechanorg/beads")
DAYS_LOOKBACK=2
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
REPORT_FILE="${BUG_REPORTS_DIR}/bug-hunt-${TIMESTAMP}.md"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Ensure bug_reports directory exists
mkdir -p "$BUG_REPORTS_DIR"

log_info "Starting daily bug hunt..."

# Get PRs merged in the last N days
get_merged_prs() {
    local repo="$1"
    local since_date=$(date -v-${DAYS_LOOKBACK}d '+%Y-%m-%d' 2>/dev/null || date -d "${DAYS_LOOKBACK} days ago" '+%Y-%m-%d')
    
    # Note: gh pr list --merged-at doesn't support comparison operators
    # Use --json and pipe to jq to filter by date; --arg passes value as $since (not $since_date)
    gh pr list --repo "$repo" --state merged --limit 100 --json number,title,url,mergedAt | \
        jq --arg since "$since_date" --arg repo "$repo" '[.[] | select(.mergedAt >= $since) | . + {repo: $repo}]'
}

# Initialize report file
cat > "$REPORT_FILE" << EOF
# Bug Hunt Report - ${TIMESTAMP}

**Generated:** $(date)
**Period:** Last ${DAYS_LOOKBACK} days
**Agents:** claude, codex, cursor, minimax, gemini

---

EOF

# Track all findings
TOTAL_PRS=0
FINDINGS=""
PRS_JSON="[]"

# Process each repo
for REPO in "${REPOS[@]}"; do
    log_info "Checking $REPO for merged PRs..."
    
    REPO_PRS=$(get_merged_prs "$REPO" 2>/dev/null || echo "[]")
    PRS_JSON=$(echo "$PRS_JSON" | jq --argjson new "$REPO_PRS" '. + $new')
    PR_COUNT=$(echo "$REPO_PRS" | jq 'length' 2>/dev/null || echo "0")
    
    if [ "$PR_COUNT" -eq 0 ]; then
        log_info "No PRs merged in $REPO in the last $DAYS_LOOKBACK days"
        continue
    fi
    
    log_info "Found $PR_COUNT merged PRs in $REPO"
    
    # For each PR, prepare bug-hunting task (tab-separated to handle titles with '|')
    while IFS=$'\t' read -r pr_num pr_title pr_url; do
        log_info "Preparing bug hunt for $REPO PR #$pr_num: $pr_title"

        # Add to findings
        FINDINGS+="- [$REPO PR #$pr_num]($pr_url): $pr_title\n"
        TOTAL_PRS=$((TOTAL_PRS + 1))
    done < <(echo "$REPO_PRS" | jq -r '.[] | "\(.number)\t\(.title)\t\(.url)"')
done

# Agent configurations for parallel execution
AGENTS=("claude" "codex" "cursor" "minimax" "gemini")
AGENT_PIDS=()

# Spawn agents in parallel (direct CLI invocation — ao spawn has no --task flag)
log_info "Spawning bug hunt agents..."

for AGENT in "${AGENTS[@]}"; do
    log_info "Starting $AGENT agent for bug hunt..."
    
    # Create a task prompt for the agent
    TASK_PROMPT="Bug Hunt Task for $AGENT:

Analyze these merged PRs for bugs:
$PRS_JSON

For each PR:
1. Use 'gh pr diff <number> --repo <repo>' to fetch the code changes
2. Examine the diff for:
   - Logic bugs (null checks missing, edge cases)
   - Error handling issues
   - Memory leaks or resource issues
   - Security vulnerabilities
   - Race conditions
   - Type errors

Return findings as structured JSON only (do not create files):
[
  {
    \"repo\": \"...\",
    \"pr\": 123,
    \"file\": \"path/to/file\",
    \"line\": 42,
    \"severity\": 1,
    \"description\": \"...\",
    \"suggested_fix\": \"...\"
  }
]"

    # Output file for this agent's findings
    OUTPUT_FILE="${BUG_REPORTS_DIR}/bug-hunt-${AGENT}-${TIMESTAMP}.json"
    
    # Spawn agent in background (non-blocking) via direct CLI
    (
        ERR_FILE="${BUG_REPORTS_DIR}/bug-hunt-${AGENT}-${TIMESTAMP}.err"
        case "$AGENT" in
            claude)
                claude --dangerously-skip-permissions "$TASK_PROMPT" > "$OUTPUT_FILE" 2>>"$ERR_FILE" || true
                ;;
            codex)
                codex exec --dangerously-bypass-approvals-and-sandbox "$TASK_PROMPT" > "$OUTPUT_FILE" 2>>"$ERR_FILE" || true
                ;;
            gemini)
                gemini --approval-mode yolo "$TASK_PROMPT" > "$OUTPUT_FILE" 2>>"$ERR_FILE" || true
                ;;
            cursor)
                cursor-agent --print "$TASK_PROMPT" > "$OUTPUT_FILE" 2>>"$ERR_FILE" || true
                ;;
            minimax)
                ANTHROPIC_API_KEY="${MINIMAX_API_KEY}" \
                ANTHROPIC_BASE_URL="https://api.minimax.io/anthropic" \
                ANTHROPIC_MODEL="MiniMax-M2.5" \
                claude --dangerously-skip-permissions "$TASK_PROMPT" > "$OUTPUT_FILE" 2>>"$ERR_FILE" || true
                ;;
            *)
                echo "Unknown agent: $AGENT" >> "$ERR_FILE"
                ;;
        esac
    ) &
    
    # Store PIDs for wait
    AGENT_PIDS+=($!)
done

# Wait for all agents to complete (with timeout)
log_info "Waiting for bug hunt agents to complete..."
TIMEOUT_SECONDS=600  # 10 minutes
for PID in "${AGENT_PIDS[@]}"; do
    ( sleep "$TIMEOUT_SECONDS" && kill "$PID" 2>/dev/null ) &
    TIMEOUT_PID=$!
    wait "$PID" 2>/dev/null || true
    kill "$TIMEOUT_PID" 2>/dev/null || true
done

# Count bugs by parsing JSON output files from this run
ACTUAL_BUGS=0
for AGENT in "${AGENTS[@]}"; do
    OUTPUT_FILE="${BUG_REPORTS_DIR}/bug-hunt-${AGENT}-${TIMESTAMP}.json"
    if [ -f "$OUTPUT_FILE" ]; then
        # Validate JSON first — non-JSON output (API errors, plain text) must not be silently zero'd
        if ! jq empty "$OUTPUT_FILE" 2>/dev/null; then
            log_warn "$OUTPUT_FILE is not valid JSON — skipping (possible API error or timeout)"
            continue
        fi
        # Top-level array, or wrapped object with a findings array (common LLM shape).
        # Use `else empty end` (not `else 0 end`) so unrecognized shapes produce no output,
        # making AGENT_BUGS empty and caught by the fail-closed case below.
        AGENT_BUGS=$(
            jq '
                if type == "array" then length
                elif type == "object" and (.findings | type == "array") then (.findings | length)
                else empty end
            ' "$OUTPUT_FILE" 2>/dev/null
        )
        # Empty = unrecognized shape or jq error — fail closed, do not count as zero
        case "${AGENT_BUGS}" in
            '') log_warn "could not parse bug count from $OUTPUT_FILE — skipping"; continue ;;
            *[!0-9]*) log_warn "unexpected bug count '${AGENT_BUGS}' from $OUTPUT_FILE — skipping"; continue ;;
        esac
        ACTUAL_BUGS=$((ACTUAL_BUGS + AGENT_BUGS))
    fi
done

# Create fix PRs for bugs found (after agents complete)
FIX_PR_COUNT=0
if [ "${ACTUAL_BUGS:-0}" -gt 0 ] 2>/dev/null; then
    log_info "Creating fix PRs for $ACTUAL_BUGS bugs..."

    # Collect all findings from all agents
    ALL_FINDINGS="[]"
    for AGENT in "${AGENTS[@]}"; do
        OUTPUT_FILE="${BUG_REPORTS_DIR}/bug-hunt-${AGENT}-${TIMESTAMP}.json"
        if [ -f "$OUTPUT_FILE" ] && jq empty "$OUTPUT_FILE" 2>/dev/null; then
            AGENT_FINDINGS=$(
                jq 'if type == "array" then . elif type == "object" and (.findings | type == "array") then .findings else [] end' \
                    "$OUTPUT_FILE" 2>/dev/null
            )
            ALL_FINDINGS=$(echo "$ALL_FINDINGS" | jq --argjson new "$AGENT_FINDINGS" '. + $new' 2>/dev/null)
        fi
    done

    # Deduplicate by repo+file+line+description
    UNIQUE_FINDINGS=$(echo "$ALL_FINDINGS" | jq 'unique_by("\(.repo)\(.\pr)\(.\file)\(.\line)\(.\description)")' 2>/dev/null)

    # For each unique bug, spawn a fix agent
    FIX_BRANCH_NAME="fix/bug-hunt-$(date +%Y%m%d)"

    while IFS= read -r finding; do
        repo=$(echo "$finding" | jq -r '.repo // empty')
        pr=$(echo "$finding" | jq -r '.pr // empty')
        file=$(echo "$finding" | jq -r '.file // empty')
        line=$(echo "$finding" | jq -r '.line // empty')
        severity=$(echo "$finding" | jq -r '.severity // 3')
        description=$(echo "$finding" | jq -r '.description // empty')
        suggested_fix=$(echo "$finding" | jq -r '.suggested_fix // empty')

        # Only fix P1/P2 bugs
        if [ "$severity" -gt 2 ] 2>/dev/null; then
            log_info "Skipping severity-$severity bug (only fixing P1/P2): $repo PR#$pr $file:$line"
            continue
        fi

        FIX_TASK="Fix Bug from Bug Hunt:

Bug found in $repo PR #$pr:
- File: $file
- Line: $line
- Severity: $severity (P1/P2)
- Description: $description
- Suggested Fix: $suggested_fix

Your job:
1. Clone/checkout $repo if not already present
2. Create branch: $FIX_BRANCH_NAME-<short-hash-of-finding>
3. Apply the fix to $file around line $line
4. Write a test that reproduces the bug and verifies the fix
5. Commit with message: 'fix: patch $file:$line - <one-line-description>'
6. Push branch to origin
7. Create PR titled '[bug-hunt] fix: <brief description>' targeting main
8. In the PR body, reference: 'Found in bug hunt — $repo PR #$pr'

Return the PR URL as your final output."

        FIX_LOG="${BUG_REPORTS_DIR}/bug-hunt-fix-$(date +%s)-${RANDOM}.log"
        ANTHROPIC_API_KEY="${MINIMAX_API_KEY}" \
            ANTHROPIC_BASE_URL="https://api.minimax.io/anthropic" \
            ANTHROPIC_MODEL="MiniMax-M2.5" \
            claude --dangerously-skip-permissions "$FIX_TASK" >> "$FIX_LOG" 2>&1 &
        FIX_AGENT_PID=$!
        (
          sleep 300
          kill $FIX_AGENT_PID 2>/dev/null || true
        ) &
        watchdog_pid=$!
        wait $FIX_AGENT_PID 2>/dev/null || true
        kill $watchdog_pid 2>/dev/null || true
        wait $watchdog_pid 2>/dev/null || true
        FIX_PR_COUNT=$((FIX_PR_COUNT + 1))
    done < <(echo "$UNIQUE_FINDINGS" | jq -r '.[] | @json' 2>/dev/null)
fi

# Append findings and totals to report file
FIX_PR_INFO=""
if [ "${FIX_PR_COUNT:-0}" -gt 0 ]; then
    FIX_PR_INFO="- Fix PRs created: $FIX_PR_COUNT"
fi

cat >> "$REPORT_FILE" << EOF
## PR Findings

$(echo -e "$FINDINGS")

## Results

- PRs reviewed: $TOTAL_PRS
- Bugs found: $ACTUAL_BUGS
$FIX_PR_INFO
EOF

# Only ping @openclaw when there is at least one counted finding (see gh #242).
OPENCLAW_BUG_ESCALATION=""
if [ "${ACTUAL_BUGS:-0}" -gt 0 ] 2>/dev/null; then
    OPENCLAW_BUG_ESCALATION="

@openclaw Please fix these bugs using agento"
fi

# Create Slack message
SLACK_MESSAGE="*Daily Bug Hunt Report - ${TIMESTAMP}*

*Repos scanned:* ${REPOS[*]}
*Period:* Last ${DAYS_LOOKBACK} days
*Agents deployed:* ${AGENTS[*]}

*PRs reviewed (${TOTAL_PRS}):*
$(echo -e "$FINDINGS")

*Results:*
- PRs reviewed: $TOTAL_PRS
- Bugs found: $ACTUAL_BUGS${FIX_PR_INFO:+, Fix PRs created: $FIX_PR_COUNT}

*Reports:* $REPORT_FILE${OPENCLAW_BUG_ESCALATION}"

# Post to Slack using user token (so OpenClaw gateway will react to @openclaw mentions)
SLACK_POSTED=0
if [ -f "$HOME/.profile" ]; then
    source "$HOME/.profile" 2>/dev/null || true
fi
if [ -n "${SLACK_USER_TOKEN:-}" ]; then
    SLACK_CHANNEL_ID="${BUG_HUNT_SLACK_CHANNEL_ID:-${SLACK_CHANNEL_ID}}"  # default to #bug-hunt
    SLACK_RESPONSE=""
    if SLACK_RESPONSE=$(curl -s -X POST "https://slack.com/api/chat.postMessage" \
        -H "Authorization: Bearer $SLACK_USER_TOKEN" \
        -H "Content-Type: application/json" \
        -d "{\"channel\":\"$SLACK_CHANNEL_ID\",\"text\":$(echo "$SLACK_MESSAGE" | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read()))')}") \
        && echo "$SLACK_RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); sys.exit(0 if d.get('ok') else 1)" 2>/dev/null; then
        SLACK_POSTED=1
    else
        log_warn "Failed to post to Slack: $(echo "$SLACK_RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('error','unknown'))" 2>/dev/null || echo "${SLACK_RESPONSE:-curl failed}")"
    fi
else
    log_warn "SLACK_USER_TOKEN not set — skipping Slack notification"
fi

# Fallback to GitHub issue only when Slack was not posted
if [ "$TOTAL_PRS" -gt 0 ] && [ "$SLACK_POSTED" -ne 1 ]; then
    gh issue create --title "Bug Hunt Report - $TIMESTAMP" \
        --body "$SLACK_MESSAGE" \
        --repo "${REPOS[0]}" 2>/dev/null || log_warn "Failed to create GitHub issue"
fi

# Final report
log_info "Bug hunt complete!"
log_info "Total PRs reviewed: $TOTAL_PRS"
log_info "Report saved to: $REPORT_FILE"

# Print summary
echo ""
echo "============================================"
echo "         BUG HUNT SUMMARY"
echo "============================================"
echo "Timestamp:    $TIMESTAMP"
echo "PRs Reviewed:  $TOTAL_PRS"
echo "Bugs Found:   $ACTUAL_BUGS"
echo "Report:       $REPORT_FILE"
echo "============================================"

exit 0
