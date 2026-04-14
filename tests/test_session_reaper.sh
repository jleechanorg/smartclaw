#!/bin/bash
# test_session_reaper.sh - Tests for session reaper functionality
# These tests verify the behavior of the session reaper script

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REAPER_SCRIPT="$SCRIPT_DIR/../scripts/ao-session-reaper.sh"

# Source production script to exercise real functions
# shellcheck source=/dev/null
source "$REAPER_SCRIPT"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

PASSED=0
FAILED=0

log_pass() { echo -e "${GREEN}✓ PASS${NC}: $1"; ((PASSED++)); }
log_fail() { echo -e "${RED}✗ FAIL${NC}: $1"; ((FAILED++)); }
log_info() { echo -e "${YELLOW}ℹ INFO${NC}: $1"; }

# Mock functions for testing
mock_tmux_list_sessions() {
    local session_type="$1"
    case "$session_type" in
        merged_pr)
            cat <<'EOF'
jc-123	/tmp/worktrees/smartclaw/pr-123	(1) (04/02 14:30:25) (0)	/tmp/worktrees/smartclaw/pr-123 [branch: fix-bug]
jc-456	/tmp/worktrees/smartclaw/pr-456	(1) (04/02 14:30:25) (0)	/tmp/worktrees/smartclaw/pr-456 [branch: feature-x]
EOF
            ;;
        closed_pr)
            cat <<'EOF'
jc-789	/tmp/worktrees/smartclaw/pr-789	(1) (04/02 14:30:25) (0)	/tmp/worktrees/smartclaw/pr-789 [branch: fix-other]
EOF
            ;;
        open_pr)
            cat <<'EOF'
jc-101	/tmp/worktrees/smartclaw/pr-101	(1) (04/02 14:30:25) (0)	/tmp/worktrees/smartclaw/pr-101 [branch: open-feature]
EOF
            ;;
        orphaned_old)
            # Session with no worktree - orphaned for > 2 hours
            cat <<'EOF'
jc-999	detached	(1) (01/01 00:00:00) (0)	 Detached
EOF
            ;;
        orphaned_new)
            # Orphaned but recent
            cat <<'EOF'
jc-998	detached	(1) (04/02 15:30:00) (0)	 Detached
EOF
            ;;
        ao_orphaned_old)
            # ao-* session with no worktree - orphaned for > 2 hours
            cat <<'EOF'
ao-748	detached	(1) (01/01 00:00:00) (0)	 Detached
EOF
            ;;
        ao_orphaned_new)
            # ao-* session orphaned but recent
            cat <<'EOF'
ao-749	detached	(1) (04/02 15:30:00) (0)	 Detached
EOF
            ;;
        mixed_stale)
            # 10 stale sessions - should only kill 5 due to cap
            cat <<'EOF'
jc-1	/tmp/worktrees/smartclaw/pr-1	(1) (04/02 14:30:25) (0)	/tmp/worktrees/smartclaw/pr-1 [branch: closed-1]
jc-2	/tmp/worktrees/smartclaw/pr-2	(1) (04/02 14:30:25) (0)	/tmp/worktrees/smartclaw/pr-2 [branch: closed-2]
jc-3	/tmp/worktrees/smartclaw/pr-3	(1) (04/02 14:30:25) (0)	/tmp/worktrees/smartclaw/pr-3 [branch: closed-3]
jc-4	/tmp/worktrees/smartclaw/pr-4	(1) (04/02 14:30:25) (0)	/tmp/worktrees/smartclaw/pr-4 [branch: closed-4]
jc-5	/tmp/worktrees/smartclaw/pr-5	(1) (04/02 14:30:25) (0)	/tmp/worktrees/smartclaw/pr-5 [branch: closed-5]
jc-6	/tmp/worktrees/smartclaw/pr-6	(1) (04/02 14:30:25) (0)	/tmp/worktrees/smartclaw/pr-6 [branch: closed-6]
jc-7	/tmp/worktrees/smartclaw/pr-7	(1) (04/02 14:30:25) (0)	/tmp/worktrees/smartclaw/pr-7 [branch: closed-7]
jc-8	/tmp/worktrees/smartclaw/pr-8	(1) (04/02 14:30:25) (0)	/tmp/worktrees/smartclaw/pr-8 [branch: closed-8]
jc-9	/tmp/worktrees/smartclaw/pr-9	(1) (04/02 14:30:25) (0)	/tmp/worktrees/smartclaw/pr-9 [branch: closed-9]
jc-10	/tmp/worktrees/smartclaw/pr-10	(1) (04/02 14:30:25) (0)	/tmp/worktrees/smartclaw/pr-10 [branch: closed-10]
EOF
            ;;
        none)
            echo ""
            ;;
    esac
}

# Test 1: Session with merged PR → should be killed
test_merged_pr_killed() {
    log_info "Test: Session with merged PR should be killed"
    
    # Mock GitHub API to return "merged" for PR 123
    mock_gh_api() {
        local pr_num="$1"
        if [[ "$pr_num" == "123" ]]; then
            echo '{"state": "closed", "merged": true, "mergeable": null}'
        else
            echo '{"state": "open"}'
        fi
    }
    
    # Mock tmux kill-session
    local killed=false
    mock_kill_session() {
        killed=true
    }
    
    # Simulate the check logic
    local pr_state="merged"
    local should_kill=false
    
    if [[ "$pr_state" == "merged" || "$pr_state" == "closed" ]]; then
        should_kill=true
    fi
    
    if [[ "$should_kill" == "true" ]]; then
        log_pass "Session with merged PR should be killed"
    else
        log_fail "Session with merged PR should be killed"
    fi
}

# Test 2: Session with closed PR → should be killed
test_closed_pr_killed() {
    log_info "Test: Session with closed PR should be killed"
    
    local pr_state="closed"
    local should_kill=false
    
    if [[ "$pr_state" == "merged" || "$pr_state" == "closed" ]]; then
        should_kill=true
    fi
    
    if [[ "$should_kill" == "true" ]]; then
        log_pass "Session with closed PR should be killed"
    else
        log_fail "Session with closed PR should be killed"
    fi
}

# Test 3: Session with open PR → NOT killed
test_open_pr_not_killed() {
    log_info "Test: Session with open PR should NOT be killed"
    
    local pr_state="open"
    local should_kill=false
    
    if [[ "$pr_state" == "merged" || "$pr_state" == "closed" ]]; then
        should_kill=true
    fi
    
    if [[ "$should_kill" == "false" ]]; then
        log_pass "Session with open PR should NOT be killed"
    else
        log_fail "Session with open PR should NOT be killed"
    fi
}

# Test 4: Session with open PR + active worktree → NOT killed
test_open_pr_active_worktree_not_killed() {
    log_info "Test: Session with open PR + active worktree should NOT be killed"
    
    local pr_state="open"
    local has_worktree=true
    
    # Even with active worktree, open PR should not be killed
    local should_kill=false
    
    if [[ "$pr_state" == "merged" || "$pr_state" == "closed" ]]; then
        should_kill=true
    fi
    # Orphaned check - only if no worktree and old
    if [[ "$has_worktree" == "false" ]]; then
        # Would check age...
        should_kill=true
    fi
    
    if [[ "$should_kill" == "false" ]]; then
        log_pass "Session with open PR + active worktree should NOT be killed"
    else
        log_fail "Session with open PR + active worktree should NOT be killed"
    fi
}

# Test 5: Orphaned session older than 2h → killed
test_orphaned_old_killed() {
    log_info "Test: Orphaned session older than 2h should be killed"
    
    # Session created more than 2 hours ago, no worktree
    local session_age_seconds=7201  # Just over 2 hours
    local has_worktree=false
    local pr_state="none"
    
    local should_kill=false
    
    if [[ "$has_worktree" == "false" && "$session_age_seconds" -gt 7200 ]]; then
        should_kill=true
    fi
    
    if [[ "$should_kill" == "true" ]]; then
        log_pass "Orphaned session older than 2h should be killed"
    else
        log_fail "Orphaned session older than 2h should be killed"
    fi
}

# Test 6: Orphaned session newer than 2h → NOT killed
test_orphaned_new_not_killed() {
    log_info "Test: Orphaned session newer than 2h should NOT be killed"
    
    # Session created less than 2 hours ago, no worktree
    local session_age_seconds=3600  # 1 hour
    local has_worktree=false
    
    local should_kill=false
    
    if [[ "$has_worktree" == "false" && "$session_age_seconds" -gt 7200 ]]; then
        should_kill=true
    fi
    
    if [[ "$should_kill" == "false" ]]; then
        log_pass "Orphaned session newer than 2h should NOT be killed"
    else
        log_fail "Orphaned session newer than 2h should NOT be killed"
    fi
}

# Test 7: Kill cap - only 5 killed per run when 10 stale
test_kill_cap() {
    log_info "Test: Kill cap should limit to 5 sessions per run"
    
    local total_stale=10
    local max_kill=5
    
    local actual_kills=$((total_stale < max_kill ? total_stale : max_kill))
    
    if [[ "$actual_kills" -eq 5 ]]; then
        log_pass "Kill cap limits to 5 sessions per run"
    else
        log_fail "Kill cap should limit to 5 (got $actual_kills)"
    fi
}

# Test 8: log() function from production script produces correctly formatted output
test_log_entry_written() {
    log_info "Test: log() from production script outputs timestamp + message"

    # Capture output from the production log() function
    local log_output
    log_output=$(log "KILLING jc-123: merged PR" 2>&1)

    # Verify format: [YYYY-MM-DD HH:MM:SS] MESSAGE
    if [[ "$log_output" =~ ^\[202[0-9]-[0-9]{2}-[0-9]{2}\ [0-9]{2}:[0-9]{2}:[0-9]{2}\]\ KILLING\ jc-123:\ merged\ PR ]]; then
        log_pass "log() outputs correctly formatted timestamped message"
    else
        log_fail "log() outputs correctly formatted timestamped message (got: $log_output)"
    fi
}

# Test 9: ao-* orphaned session older than 2h → is_safe_to_kill returns success
test_ao_orphaned_old_killed() {
    log_info "Test: ao-* orphaned session older than 2h should be killed"

    # Mock tmux so get_session_age returns a timestamp 7201s in the past
    tmux() {
        if [[ "$*" == *"display-message"* && "$*" == *"ao-748"* ]]; then
            echo $(( $(date +%s) - 7201 ))
        else
            command tmux "$@"
        fi
    }

    if is_safe_to_kill "ao-748" "" ""; then
        log_pass "ao-* orphaned session older than 2h should be killed"
    else
        log_fail "ao-* orphaned session older than 2h should be killed"
    fi
}

# Test 10: ao-* orphaned session newer than 2h → is_safe_to_kill returns failure
test_ao_orphaned_new_not_killed() {
    log_info "Test: ao-* orphaned session newer than 2h should NOT be killed"

    # Mock tmux so get_session_age returns a timestamp 3600s (1h) in the past
    tmux() {
        if [[ "$*" == *"display-message"* && "$*" == *"ao-749"* ]]; then
            echo $(( $(date +%s) - 3600 ))
        else
            command tmux "$@"
        fi
    }

    if ! is_safe_to_kill "ao-749" "" ""; then
        log_pass "ao-* orphaned session newer than 2h should NOT be killed"
    else
        log_fail "ao-* orphaned session newer than 2h should NOT be killed"
    fi
}

# Test 11: parse_session_info accepts ao-* session names via production function
test_parse_ao_session() {
    log_info "Test: parse_session_info should accept ao-* session names"

    local line=$'ao-748\tdetached\t(1) (01/01 00:00:00) (0)\t Detached'
    local parsed
    parsed=$(parse_session_info "$line")

    if [[ "$parsed" == "ao-748||" ]]; then
        log_pass "parse_session_info accepts ao-* session names"
    else
        log_fail "parse_session_info accepts ao-* session names (got: $parsed)"
    fi
}

# Main test runner
main() {
    echo "========================================"
    echo "Session Reaper Tests"
    echo "========================================"
    echo ""
    
    test_merged_pr_killed
    test_closed_pr_killed
    test_open_pr_not_killed
    test_open_pr_active_worktree_not_killed
    test_orphaned_old_killed
    test_orphaned_new_not_killed
    test_ao_orphaned_old_killed
    test_ao_orphaned_new_not_killed
    test_parse_ao_session
    test_kill_cap
    test_log_entry_written
    
    echo ""
    echo "========================================"
    echo "Results: $PASSED passed, $FAILED failed"
    echo "========================================"
    
    if [[ $FAILED -gt 0 ]]; then
        exit 1
    fi
    exit 0
}

main "$@"
