#!/bin/bash
# Daily bug hunt job - runs at 9am
# Spawns multiple agents to find bugs in PRs merged in the last 2 days
# Creates bug reports, beads, and posts to #bug-hunt channel

set -euo pipefail

# Configuration
BUG_REPORTS_DIR="${HOME}/.openclaw/logs/bug_reports"
REPOS=("jleechanorg/jleechanclaw" "jleechanorg/worldarchitect.ai" "jleechanorg/ai_universe" "jleechanorg/beads")
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

# Spawn agents in parallel (using ao spawn)
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
    
    # Spawn agent in background (non-blocking)
    (
        # Use ao spawn if available, otherwise fall back to manual clone
        if command -v ao &> /dev/null; then
            # Redirect stderr separately so JSON stdout stays clean
            ao spawn "bug-hunt-${AGENT}" --agent "$AGENT" --task "$TASK_PROMPT" > "$OUTPUT_FILE" 2>>"${BUG_REPORTS_DIR}/bug-hunt-${AGENT}-${TIMESTAMP}.err" || true
        else
            log_warn "ao CLI not found, skipping $AGENT agent spawn"
        fi
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
        # Parse JSON array length (count of bugs found by this agent)
        AGENT_BUGS=$(jq 'if type == "array" then length else 0 end' "$OUTPUT_FILE" 2>/dev/null || echo "0")
        ACTUAL_BUGS=$((ACTUAL_BUGS + AGENT_BUGS))
    fi
done

# Append findings and totals to report file
cat >> "$REPORT_FILE" << EOF
## PR Findings

$(echo -e "$FINDINGS")

## Results

- PRs reviewed: $TOTAL_PRS
- Bugs found: $ACTUAL_BUGS
EOF

# Create Slack message
SLACK_MESSAGE="*Daily Bug Hunt Report - ${TIMESTAMP}*

*Repos scanned:* ${REPOS[*]}
*Period:* Last ${DAYS_LOOKBACK} days
*Agents deployed:* ${AGENTS[*]}

*PRs reviewed (${TOTAL_PRS}):*
$(echo -e "$FINDINGS")

*Results:*
- PRs reviewed: $TOTAL_PRS
- Bugs found: $ACTUAL_BUGS

*Reports:* $REPORT_FILE
$([ "$ACTUAL_BUGS" -gt 0 ] && echo "" && echo "@openclaw Please fix these bugs using agento" || true)"

# Post to Slack using user token (so OpenClaw gateway will react to @openclaw mentions)
SLACK_POSTED=0
if [ -f "$HOME/.profile" ]; then
    source "$HOME/.profile" 2>/dev/null || true
fi
if [ -n "${SLACK_USER_TOKEN:-}" ]; then
    SLACK_CHANNEL_ID="${BUG_HUNT_SLACK_CHANNEL_ID:-C09GRLXF9GR}"  # default to #bug-hunt
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
