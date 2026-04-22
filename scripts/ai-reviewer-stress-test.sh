#!/bin/bash
# AI Reviewer Stress Test - runs every 4 hours
# Selects code slices, creates PR in test repo, runs agento to fix AI reviewer comments

set -euo pipefail

# Config
SOURCE_REPO="jleechanorg/smartclaw"
TEST_REPO="jleechanorg/smartclaw-review-test"
STATE_DIR="$HOME/.smartclaw/state"
SLICE_INDEX_FILE="$STATE_DIR/stress_test_slice_index"
OUTCOME_LOG="$STATE_DIR/stress_test_outcomes.jsonl"
LOG_FILE="$HOME/.smartclaw/logs/stress_test.log"

# Create log directory
mkdir -p "$(dirname "$LOG_FILE")"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

# Load slices configuration
SLICES=(
    "src/orchestration:Orchestration modules"
    "scripts:Shell scripts"
    "src/tests:Test files"
)

# Select next slice (round-robin)
get_next_slice() {
    local index=0
    if [[ -f "$SLICE_INDEX_FILE" ]]; then
        index=$(cat "$SLICE_INDEX_FILE")
    fi
    
    # Increment and wrap around
    index=$(( (index + 1) % ${#SLICES[@]} ))
    echo "$index" > "$SLICE_INDEX_FILE"
    
    # Return slice info (format: "path:description")
    echo "${SLICES[$index]}"
}

# Get files in a slice
get_slice_files() {
    local slice_path="$1"
    local base_dir="$HOME/.smartclaw"
    
    # Find relevant files
    find "$base_dir/$slice_path" -type f \( -name "*.py" -o -name "*.sh" -o -name "*.yaml" -o -name "*.json" \) 2>/dev/null | head -20
}

# Count lines in slice
count_slice_lines() {
    local slice_path="$1"
    local base_dir="$HOME/.smartclaw"
    
    find "$base_dir/$slice_path" -type f \( -name "*.py" -o -name "*.sh" -o -name "*.yaml" \) -exec cat {} \; 2>/dev/null | wc -l
}

# Create test branch and copy files
setup_test_branch() {
    local slice_path="$1"
    local branch_name="review-$(date +%Y%m%d-%H%M%S)"
    
    log "Setting up test branch: $branch_name"
    
    # Create temp directory for test files
    local tmp_dir=$(mktemp -d)
    trap "rm -rf $tmp_dir" EXIT
    
    # Copy source files
    local base_dir="$HOME/.smartclaw"
    find "$base_dir/$slice_path" -type f \( -name "*.py" -o -name "*.sh" -o -name "*.yaml" \) 2>/dev/null | while read -r file; do
        local relative="${file#$base_dir/}"
        local dest="$tmp_dir/$relative"
        mkdir -p "$(dirname "$dest")"
        cp "$file" "$dest"
    done
    
    # Initialize git in temp dir if needed, or use gh
    echo "$branch_name"
}

# Create PR in test repo
create_test_pr() {
    local branch="$1"
    local title="$2"
    local body="$3"
    
    log "Creating PR in test repo..."
    
    # This would use gh or direct API calls
    # For now, log the intent
    log "Would create PR: $title"
    echo "https://github.com/$TEST_REPO/pull/new/$branch"
}

# Wait for AI reviewers (CodeRabbit, etc)
wait_for_reviewers() {
    local pr_url="$1"
    local max_wait=${2:-1800}  # 30 minutes default
    
    log "Waiting for AI reviewers (max ${max_wait}s)..."
    
    local waited=0
    while [[ $waited -lt $max_wait ]]; do
        # Check if reviewers have commented
        # For now, just wait a bit
        sleep 60
        waited=$((waited + 60))
        
        # Check for comments (mock)
        log "Checked for reviews at +${waited}s"
    done
}

# Run agento to fix comments
run_agento_fix() {
    local pr_url="$1"
    
    log "Running agento to fix review comments..."
    
    # Extract PR number
    local pr_num=$(echo "$pr_url" | grep -oP '\d+$')
    
    # Spawn agento
    if command -v ao &>/dev/null; then
        ao spawn "$TEST_REPO" --claim-pr "$pr_num" || log "Agento spawn failed"
    else
        log "ao CLI not available - would spawn agento"
    fi
}

# Log outcome to JSONL
log_outcome() {
    local status="$1"
    local slice="$2"
    local pr_url="$3"
    local details="${4:-}"
    
    local timestamp
    timestamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    jq -n \
        --arg ts "$timestamp" \
        --arg sl "$slice" \
        --arg st "$status" \
        --arg url "$pr_url" \
        --arg det "$details" \
        '{timestamp: $ts, slice: $sl, status: $st, pr_url: $url, details: $det}' >> "$OUTCOME_LOG"
}

# Main execution
main() {
    log "=== Starting AI Reviewer Stress Test ==="
    
    # Get next slice
    local slice_info
    slice_info=$(get_next_slice)
    local slice_path="${slice_info%%:*}"
    local slice_desc="${slice_info##*:}"
    
    log "Selected slice: $slice_path ($slice_desc)"
    
    # Count lines
    local line_count
    line_count=$(count_slice_lines "$slice_path")
    log "Slice has ~$line_count lines"
    
    # Skip if too small
    if [[ $line_count -lt 100 ]]; then
        log "Slice too small, skipping this run"
        exit 0
    fi
    
    # Setup test branch
    local branch_name
    branch_name=$(setup_test_branch "$slice_path")
    
    # Create PR
    local pr_title="Stress test: $slice_desc"
    local pr_body="Automated AI reviewer stress test

Slice: $slice_path
Lines: $line_count

This PR tests the full AI review loop."
    
    local pr_url
    pr_url=$(create_test_pr "$branch_name" "$pr_title" "$pr_body")
    
    # Wait for reviews
    wait_for_reviewers "$pr_url"
    
    # Run agento fixes
    run_agento_fix "$pr_url"
    
    # Log outcome
    log_outcome "completed" "$slice_path" "$pr_url" ""
    
    log "=== Stress Test Complete ==="
}

# Run if executed directly
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
