#!/bin/bash
# Daily bug hunt job - runs at 9am
# Spawns multiple agents to find bugs in PRs merged in the last 2 days
# Creates bug reports, beads, and posts to #bug-hunt channel

set -euo pipefail
set -m  # enable job control so background workers get their own process groups

# Configuration - output bug reports to /tmp to avoid polluting the repo
# with large agent outputs; script itself stays in scripts/
BUG_REPORTS_DIR="/tmp/openclaw/bug_reports"
REPOS=("jleechanorg/smartclaw" "jleechanorg/worldarchitect.ai" "jleechanorg/ai_universe" "jleechanorg/beads")
DAYS_LOOKBACK=2
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
REPORT_FILE="${BUG_REPORTS_DIR}/bug-hunt-${TIMESTAMP}.md"

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

write_empty_findings() {
    local output_file="$1"
    printf '[]\n' > "$output_file"
}

configure_openclaw_agent() {
    local err_file="$1"
    local help_output

    if ! command -v openclaw >/dev/null 2>&1; then
        echo "ERROR: openclaw CLI not found" >> "$err_file"
        return 1
    fi

    if ! help_output=$(run_openclaw_agent_help 2>>"$err_file"); then
        echo "ERROR: openclaw agent subcommand not found" >> "$err_file"
        return 1
    fi

    if printf '%s\n' "$help_output" | grep -q -- '--message'; then
        OPENCLAW_MESSAGE_FLAG="--message"
    elif printf '%s\n' "$help_output" | grep -Eq '(^|[[:space:],])-m([,[:space:]]|$)'; then
        OPENCLAW_MESSAGE_FLAG="-m"
    else
        OPENCLAW_MESSAGE_FLAG="--message"
    fi
}

run_openclaw_agent_help() {
    local timeout_bin
    timeout_bin=$(command -v timeout || command -v gtimeout || true)

    if [ -n "$timeout_bin" ]; then
        "$timeout_bin" "${OPENCLAW_HELP_TIMEOUT_SECONDS:-10}" openclaw agent --help
    else
        openclaw agent --help
    fi
}

terminate_process_tree() {
    local pid="$1"
    local pgid current_pgid

    pgid=$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d '[:space:]' || true)
    current_pgid=$(ps -o pgid= -p "$$" 2>/dev/null | tr -d '[:space:]' || true)

    if [ -n "$pgid" ] && [ "$pgid" != "$current_pgid" ]; then
        kill -TERM "-$pgid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    else
        kill -TERM "$pid" 2>/dev/null || true
    fi
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
# Agents run through OpenClaw one-shot prompts so this job waits for real output.
AGENTS=("claude" "codex" "minimax")
AGENT_PIDS=()
OPENCLAW_MESSAGE_FLAG="--message"
OPENCLAW_AGENT_AVAILABLE=1
OPENCLAW_PREFLIGHT_ERR="${BUG_REPORTS_DIR}/bug-hunt-openclaw-preflight-${TIMESTAMP}.err"

if ! configure_openclaw_agent "$OPENCLAW_PREFLIGHT_ERR"; then
    OPENCLAW_AGENT_AVAILABLE=0
fi

# Spawn agents in parallel via OpenClaw one-shot calls.
log_info "Spawning bug hunt agents via OpenClaw..."

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

Return findings as structured JSON wrapped in markdown code fence (do not create files):
\`\`\`json
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
]
\`\`\`
"

    # Output file for this agent's findings
    OUTPUT_FILE="${BUG_REPORTS_DIR}/bug-hunt-${AGENT}-${TIMESTAMP}.json"
    ERR_FILE="${BUG_REPORTS_DIR}/bug-hunt-${AGENT}-${TIMESTAMP}.err"

    # Check OpenClaw before launching a worker; unavailable hosts skip cleanly.
    if [ "$OPENCLAW_AGENT_AVAILABLE" -ne 1 ]; then
        log_warn "openclaw agent unavailable, writing empty findings for $AGENT"
        [ -s "$OPENCLAW_PREFLIGHT_ERR" ] && cat "$OPENCLAW_PREFLIGHT_ERR" >> "$ERR_FILE"
        write_empty_findings "$OUTPUT_FILE"
        continue
    fi

    # Map AGENT names to openclaw agent handles
    case "$AGENT" in
        claude) OCLAW_AGENT="claude" ;;
        codex) OCLAW_AGENT="codex" ;;
        cursor) OCLAW_AGENT="cursor" ;;
        minimax) OCLAW_AGENT="minimax" ;;
        gemini) OCLAW_AGENT="gemini" ;;
        *) OCLAW_AGENT="main" ;;
    esac
    # Run openclaw and extract JSON response from code fence
    # Parent job control gives the background worker its own process group.
    (
        openclaw agent --agent "$OCLAW_AGENT" "$OPENCLAW_MESSAGE_FLAG" "$TASK_PROMPT" 2>>"$ERR_FILE" | \
            perl -0777 -ne 'print $1 if /```json\n?(.*?)\n?```/s' > "$OUTPUT_FILE" || true
    ) 2>>"$ERR_FILE" &
    
    # Store PIDs for wait
    AGENT_PIDS+=($!)
done

# Wait for all agents to complete (with timeout)
log_info "Waiting for bug hunt agents to complete..."
TIMEOUT_SECONDS=600  # 10 minutes
if [ "${#AGENT_PIDS[@]}" -eq 0 ]; then
    log_warn "No bug hunt agent processes were started"
else
    for PID in "${AGENT_PIDS[@]}"; do
        ( sleep "$TIMEOUT_SECONDS" && terminate_process_tree "$PID" ) &
        TIMEOUT_PID=$!
        wait "$PID" 2>/dev/null || true
        kill "$TIMEOUT_PID" 2>/dev/null || true
    done
fi

# Count bugs by parsing JSON output files from this run.
# Track agent failures separately so a clean-sweep is only reported when agents actually ran.
AGENT_FAILURES=0
ACTUAL_BUGS=0
for AGENT in "${AGENTS[@]}"; do
    OUTPUT_FILE="${BUG_REPORTS_DIR}/bug-hunt-${AGENT}-${TIMESTAMP}.json"
    ERR_FILE="${BUG_REPORTS_DIR}/bug-hunt-${AGENT}-${TIMESTAMP}.err"

    if [ ! -f "$OUTPUT_FILE" ]; then
        log_warn "$AGENT produced no output file — agent failed"
        AGENT_FAILURES=$((AGENT_FAILURES + 1))
        continue
    fi

    # Empty file = agent produced nothing (crashed, timeout, connection error)
    if [ ! -s "$OUTPUT_FILE" ]; then
        log_warn "$AGENT output file is empty (0 bytes) — agent failed; see $ERR_FILE"
        AGENT_FAILURES=$((AGENT_FAILURES + 1))
        continue
    fi

    # Validate JSON — non-JSON output (API errors, plain text) must not be silently zero'd
    if ! jq empty "$OUTPUT_FILE" 2>/dev/null; then
        log_warn "$OUTPUT_FILE is not valid JSON — skipping (possible API error)"
        AGENT_FAILURES=$((AGENT_FAILURES + 1))
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
done

# Fail-closed: if ALL agents failed, this is NOT a clean sweep
ALL_AGENTS_FAILED=0
if [ "$AGENT_FAILURES" -eq "${#AGENTS[@]}" ]; then
    log_error "All bug hunt agents failed — 0 bugs recorded is NOT a clean sweep"
    ALL_AGENTS_FAILED=1
fi

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
    UNIQUE_FINDINGS=$(echo "$ALL_FINDINGS" | jq 'unique_by("\(.repo)\(.pr)\(.file)\(.line)\(.description)")' 2>/dev/null)

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
        if [ "$OPENCLAW_AGENT_AVAILABLE" -ne 1 ]; then
            log_warn "openclaw agent unavailable, skipping fix task for $repo PR#$pr"
            [ -s "$OPENCLAW_PREFLIGHT_ERR" ] && cat "$OPENCLAW_PREFLIGHT_ERR" >> "$FIX_LOG"
            continue
        fi

        (
            openclaw agent --agent "${BUG_HUNT_FIX_AGENT:-main}" "$OPENCLAW_MESSAGE_FLAG" "$FIX_TASK" >> "$FIX_LOG" 2>&1
        ) &
        FIX_PID=$!
        (
          sleep "${BUG_HUNT_FIX_TIMEOUT_SECONDS:-300}"
          terminate_process_tree "$FIX_PID"
        ) &
        watchdog_pid=$!
        FIX_SUCCEEDED=0
        if wait "$FIX_PID" 2>/dev/null; then
            FIX_SUCCEEDED=1
        else
            log_warn "fix agent failed for $repo PR#$pr; see $FIX_LOG"
        fi
        kill $watchdog_pid 2>/dev/null || true
        wait $watchdog_pid 2>/dev/null || true
        if [ "$FIX_SUCCEEDED" -eq 1 ]; then
            FIX_PR_COUNT=$((FIX_PR_COUNT + 1))
        fi
    done < <(echo "$UNIQUE_FINDINGS" | jq -r '.[] | @json' 2>/dev/null)
fi

# Append findings and totals to report file
FIX_PR_INFO=""
if [ "${FIX_PR_COUNT:-0}" -gt 0 ]; then
    FIX_PR_INFO="- Fix PRs created: $FIX_PR_COUNT"
fi

# Build failure warning block (included in report and Slack when agents failed)
FAILURE_WARNING=""
if [ "${ALL_AGENTS_FAILED:-0}" -eq 1 ]; then
    FAILURE_WARNING="

:warning: *ALL bug hunt agents failed to run.* 0 bugs recorded — this is NOT a clean sweep.
Check error logs in $BUG_REPORTS_DIR/*.err for details."
elif [ "${AGENT_FAILURES:-0}" -gt 0 ]; then
    FAILURE_WARNING="

:warning: ${AGENT_FAILURES}/${#AGENTS[@]} agents failed — bug count may be incomplete.
Check error logs in $BUG_REPORTS_DIR/*.err for details."
fi

cat >> "$REPORT_FILE" << EOF
## PR Findings

$(echo -e "$FINDINGS")

## Results

- PRs reviewed: $TOTAL_PRS
- Bugs found: $ACTUAL_BUGS
- Agent failures: ${AGENT_FAILURES:-0}/${#AGENTS[@]}${FIX_PR_INFO:+$'\n'$FIX_PR_INFO}
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
- Agent failures: ${AGENT_FAILURES:-0}/${#AGENTS[@]}
${FAILURE_WARNING}
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
