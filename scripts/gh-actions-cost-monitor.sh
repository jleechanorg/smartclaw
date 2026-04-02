#!/bin/bash
#
# GitHub Actions Cost Monitor
# Runs daily at 9pm to monitor GH Actions costs across jleechanorg private repos.
# Alerts via Slack when daily cost exceeds $5.00 threshold.
#
set -euo pipefail

# Cost constants (Linux self-hosted platform fee per minute)
COST_PER_MINUTE=0.002
DAILY_THRESHOLD=5.00

# Repos to monitor
REPOS=(
    "jleechanorg/smartclaw"
    "jleechanorg/worldai_claw"
    "jleechanorg/worldarchitect.ai"
)

# Slack channel for alerts
SLACK_CHANNEL="${SLACK_CHANNEL:-${SLACK_CHANNEL_ID}}"

# Log file
LOG_DIR="${LOG_DIR:-$HOME/.smartclaw/logs}"
LOG_FILE="${LOG_DIR}/gh-actions-cost-monitor.log"

# TODAY must match gh api .created_at timezone (always UTC ISO-8601 with Z suffix)
if [[ "$(uname)" == "Darwin" ]]; then
    TODAY=$(date -u '+%Y-%m-%d')
else
    TODAY=$(date -u '+%Y-%m-%d')
fi

# Ensure log directory exists
mkdir -p "$(dirname "$LOG_FILE")"

log() {
    echo "[$(date '+%Y-%m-%dT%H:%M:%S%z')] $1" | tee -a "$LOG_FILE"
}

log_warn() {
    echo "[$(date '+%Y-%m-%dT%H:%M:%S%z')] WARN: $1" | tee -a "$LOG_FILE"
}

log_error() {
    echo "[$(date '+%Y-%m-%dT%H:%M:%S%z')] ERROR: $1" | tee -a "$LOG_FILE"
}

# Resolve Slack bot token - check env, then source ~/.bashrc once
resolve_slack_token() {
    if [[ -n "${SLACK_BOT_TOKEN:-}" ]]; then
        printf '%s' "$SLACK_BOT_TOKEN"
        return 0
    fi
    # launchd jobs don't inherit env vars from shell config; source it once
    if [[ -f "$HOME/.bashrc" ]]; then
        local token
        token=$(
            set +e
            bash -c 'source "$HOME/.bashrc" 2>/dev/null; printf "%s" "${SLACK_BOT_TOKEN:-}"'
        )
        if [[ -n "$token" ]]; then
            printf '%s' "$token"
            return 0
        fi
    fi
    return 1
}

# Send Slack alert
send_slack_alert() {
    local message="$1"
    local slack_token

    slack_token=$(resolve_slack_token) || {
        log_warn "Cannot send Slack alert: SLACK_BOT_TOKEN not available"
        return 0  # don't abort under set -e; continue to GH issue
    }

    local payload
    payload=$(jq -n --arg channel "$SLACK_CHANNEL" --arg text "$message" \
        '{channel: $channel, text: $text}')

    local response
    response=$(curl -s -X POST "https://slack.com/api/chat.postMessage" \
        -H "Authorization: Bearer $slack_token" \
        -H "Content-Type: application/json" \
        -d "$payload")

    echo "$response" | jq -e '.ok == true' >/dev/null 2>&1 || {
        local err
        err=$(echo "$response" | jq -r '.error // "unknown"')
        log_warn "Slack API error: $err"
    }
}

# Create GitHub issue for cost alert
create_gh_issue() {
    local title="$1"
    local body="$2"

    gh api repos/jleechanorg/smartclaw/issues --method POST \
        -f title="$title" \
        -f body="$body" \
        -f labels='["cost-alert","automated"]' >> "$LOG_FILE" 2>&1 || {
            log_error "Failed to create GitHub issue"
            return 1
        }
    log "GitHub issue created: $title"
}

# Calculate duration in minutes between two ISO timestamps
calc_duration_minutes() {
    local started="$1"
    local updated="$2"

    if [[ "$(uname)" == "Darwin" ]]; then
        # macOS BSD date parsing
        local started_epoch updated_epoch
        started_epoch=$(date -j -f '%Y-%m-%dT%H:%M:%SZ' "$started" '+%s' 2>/dev/null || \
                       gdate -d "$started" '+%s' 2>/dev/null || echo 0)
        updated_epoch=$(date -j -f '%Y-%m-%dT%H:%M:%SZ' "$updated" '+%s' 2>/dev/null || \
                        gdate -d "$updated" '+%s' 2>/dev/null || echo 0)
    else
        # GNU date
        local started_epoch updated_epoch
        started_epoch=$(date -d "$started" '+%s' 2>/dev/null || echo 0)
        updated_epoch=$(date -d "$updated" '+%s' 2>/dev/null || echo 0)
    fi

    local diff=$((updated_epoch - started_epoch))
    echo $((diff / 60))
}

# Accumulate today's completed runs from paginated gh api response into a temp JSON array.
# gh --paginate returns multiple JSON objects concatenated; we process each page's
# .workflow_runs[] array in isolation and append selected runs to a temp file.
accumulate_today_runs() {
    local repo="$1"
    local tmpfile="$2"

    # gh api --paginate streams each page as a separate JSON document (NDJSON).
    # Each page is {"total_count":..., "workflow_runs":[...]}.  Extract matching runs
    # from .workflow_runs[] and write one JSON object per line to $tmpfile.
    gh api repos/"$repo"/actions/runs \
        --paginate \
        -q '[.workflow_runs[] | select(.created_at | startswith("'"$TODAY"'"))] | .[] | @json' \
        >> "$tmpfile" 2>/dev/null || true
}

# Main monitoring logic
main() {
    log "=== GitHub Actions Cost Monitor Started ==="
    log "Monitoring date: $TODAY"
    log "Threshold: \$$DAILY_THRESHOLD/day"

    local total_minutes=0
    local total_runs=0
    local total_cost=0
    local repo_summaries=()

    # Work in a temp dir to safely accumulate paginated JSON
    local work_dir
    work_dir=$(mktemp -d) && trap "rm -rf '$work_dir'" EXIT

    for repo in "${REPOS[@]}"; do
        log "Checking $repo..."

        local runs_tmp="$work_dir/runs_${repo//\//_}.json"
        : > "$runs_tmp"

        accumulate_today_runs "$repo" "$runs_tmp"

        local run_count=0
        local repo_minutes=0

        # Each line is a JSON object (one run), skip empty / non-JSON lines
        while IFS= read -r line; do
            [[ -z "$line" || ! "$line" =~ ^\{ ]] && continue

            local status started_at updated_at name duration
            status=$(echo "$line" | jq -r '.status // empty')
            started_at=$(echo "$line" | jq -r '.run_started_at // empty')
            updated_at=$(echo "$line" | jq -r '.updated_at // empty')
            name=$(echo "$line" | jq -r '.name // "unknown"')

            if [[ "$status" == "completed" && -n "$started_at" && -n "$updated_at" && \
                  "$started_at" != "null" && "$updated_at" != "null" ]]; then
                duration=$(calc_duration_minutes "$started_at" "$updated_at")
                repo_minutes=$((repo_minutes + duration))
                run_count=$((run_count + 1))
                log "  - $name: ${duration}min"
            fi
        done < "$runs_tmp"

        rm -f "$runs_tmp"

        local repo_cost
        repo_cost=$(awk "BEGIN {printf \"%.4f\", $repo_minutes * $COST_PER_MINUTE}")
        total_minutes=$((total_minutes + repo_minutes))
        total_runs=$((total_runs + run_count))
        total_cost=$(awk "BEGIN {printf \"%.4f\", $total_cost + $repo_cost}")

        repo_summaries+=("$repo: ${run_count}runs ${repo_minutes}min \$${repo_cost}")

        log "  $repo summary: ${run_count} completed runs, ${repo_minutes} minutes, \$${repo_cost}"
    done

    # Format total cost to 2 decimal places
    local total_cost_display
    total_cost_display=$(printf "%.2f" "$total_cost")

    log "=== Daily Summary ==="
    log "Total runs: $total_runs"
    log "Total minutes: $total_minutes"
    log "Total cost: \$$total_cost_display"

    # Check if threshold exceeded
    local threshold_exceeded
    threshold_exceeded=$(awk "BEGIN {print ($total_cost > $DAILY_THRESHOLD) ? 1 : 0}")

    if [[ "$threshold_exceeded" == "1" ]]; then
        log "ALERT: Daily cost \$$total_cost_display exceeds threshold \$$DAILY_THRESHOLD"

        local repo_list
        repo_list=$(printf '%s\n' "${repo_summaries[@]}" | paste -sd ',' -)

        # Send Slack alert
        local slack_message
        slack_message="[AI Cost Alert] Daily GH Actions cost: \$$total_cost_display (across $total_runs runs, $total_minutes minutes). Repos: $repo_list"
        send_slack_alert "$slack_message"

        # Create GitHub issue
        local issue_title="[Cost Alert] Daily GH Actions \$$total_cost_display exceeds \$5 threshold"
        local issue_body="## Cost Alert

**Date:** $TODAY
**Total Cost:** \$$total_cost_display
**Threshold:** \$$DAILY_THRESHOLD

### Summary
- Total runs: $total_runs
- Total minutes: $total_minutes

### Per-Repo Breakdown
$(printf '%s\n' "${repo_summaries[@]}" | sed 's/^/- /')

---
*Automated alert from gh-actions-cost-monitor.sh*"

        create_gh_issue "$issue_title" "$issue_body"
    else
        log "Cost within threshold: \$$total_cost_display <= \$$DAILY_THRESHOLD"
    fi

    log "=== GitHub Actions Cost Monitor Finished ==="
    exit 0
}

main "$@"
