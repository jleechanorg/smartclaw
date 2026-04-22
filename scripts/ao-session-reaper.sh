#!/bin/bash
# ao-session-reaper.sh - Nightly reaper for stale tmux sessions
# Runs as cron job (via openclaw cron) at 03:00 daily
# Also callable manually

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="${HOME}/.smartclaw/logs/session-reaper.log"
MAX_KILLS=5
REPO="jleechanorg/smartclaw"

# Ensure log directory exists
mkdir -p "$(dirname "$LOG_FILE")"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

# Get GitHub token
GH_TOKEN="${GITHUB_TOKEN:-$(gh auth token 2>/dev/null)}"

# Parse tmux session info
parse_session_info() {
    local session_line="$1"
    
    # Extract session name (first field)
    local session_name
    session_name=$(echo "$session_line" | awk '{print $1}')
    
    # Skip if not jc-* session
    if [[ ! "$session_name" =~ ^jc-[0-9]+$ ]]; then
        return 1
    fi
    
    # Extract worktree path (second field, or check for "detached")
    local worktree_path
    worktree_path=$(echo "$session_line" | awk '{print $2}')
    
    local branch=""
    if [[ "$worktree_path" == "detached" ]]; then
        worktree_path=""
    else
        # Try to extract branch from session info
        branch=$(echo "$session_line" | grep -oP '\[branch:\s*\K[^\]]+' || echo "")
    fi
    
    echo "$session_name|$worktree_path|$branch"
}

# Get session age in seconds
get_session_age() {
    local session_name="$1"
    
    # Get session creation time from tmux
    local created_at
    created_at=$(tmux display-message -t "$session_name" -F '#{session_created}' 2>/dev/null || echo "0")
    
    if [[ -z "$created_at" || "$created_at" == "0" ]]; then
        echo "0"
        return
    fi
    
    local now
    now=$(date +%s)
    echo $((now - created_at))
}

# Get PR state from GitHub
get_pr_state() {
    local branch="$1"
    
    if [[ -z "$branch" ]]; then
        echo "none"
        return
    fi
    
    # Query GitHub API for PRs with this branch as head
    local response
    response=$(curl -s -H "Authorization:Bearer $GH_TOKEN" \
        "https://api.github.com/repos/$REPO/pulls?head=$branch&state=all" 2>/dev/null)
    
    if [[ -z "$response" || "$response" == "[]" ]]; then
        echo "none"
        return
    fi
    
    # Check first PR result
    local state
    state=$(echo "$response" | jq -r '.[0].state // "none"')
    local merged
    merged=$(echo "$response" | jq -r '.[0].merged // false')
    
    if [[ "$state" == "closed" && "$merged" == "true" ]]; then
        echo "merged"
    elif [[ "$state" == "closed" ]]; then
        echo "closed"
    elif [[ "$state" == "open" ]]; then
        echo "open"
    else
        echo "none"
    fi
}

# Check if session is safe to kill
# Returns 0 if safe to kill, 1 if not safe
is_safe_to_kill() {
    local session_name="$1"
    local worktree_path="$2"
    local branch="$3"
    
    local age
    age=$(get_session_age "$session_name")
    
    # Case 1: No worktree (orphaned session)
    if [[ -z "$worktree_path" || ! -d "$worktree_path" ]]; then
        if [[ $age -gt 7200 ]]; then  # 2 hours
            return 0
        else
            return 1
        fi
    fi
    
    # Case 2: Branch with no associated PR
    if [[ -z "$branch" ]]; then
        if [[ $age -gt 14400 ]]; then  # 4 hours
            return 0
        else
            return 1
        fi
    fi
    
    # Case 3: Check PR state
    local pr_state
    pr_state=$(get_pr_state "$branch")
    
    case "$pr_state" in
        merged|closed)
            return 0
            ;;
        open)
            return 1
            ;;
        none)
            # No PR found - use age threshold
            if [[ $age -gt 14400 ]]; then  # 4 hours
                return 0
            else
                return 1
            fi
            ;;
        *)
            return 1
            ;;
    esac
}

# Kill a tmux session
kill_session() {
    local session_name="$1"
    
    tmux kill-session -t "$session_name" 2>/dev/null
}

# Remove a worktree
remove_worktree() {
    local worktree_path="$1"
    
    if [[ -z "$worktree_path" || ! -d "$worktree_path" ]]; then
        return 1
    fi
    
    # Use git worktree remove
    cd "$(dirname "$worktree_path")" 2>/dev/null && \
        git worktree remove "$worktree_path" 2>/dev/null
}

# Main reaper logic
main() {
    log "=== Starting session reaper ==="
    
    local killed_count=0
    
    # List all tmux sessions
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        
        # Parse session info
        local session_info
        session_info=$(parse_session_info "$line") || continue
        
        local session_name worktree_path branch
        session_name=$(echo "$session_info" | cut -d'|' -f1)
        worktree_path=$(echo "$session_info" | cut -d'|' -f2)
        branch=$(echo "$session_info" | cut -d'|' -f3)
        
        # Check kill cap
        if [[ $killed_count -ge $MAX_KILLS ]]; then
            log "Reached kill cap ($MAX_KILLS), stopping"
            break
        fi
        
        # Check if safe to kill
        if is_safe_to_kill "$session_name" "$worktree_path" "$branch"; then
            local age pr_state reason
            age=$(get_session_age "$session_name")
            pr_state=$(get_pr_state "$branch")
            
            if [[ -z "$worktree_path" ]]; then
                reason="orphaned (${age}s)"
            elif [[ "$pr_state" == "merged" || "$pr_state" == "closed" ]]; then
                reason="PR $pr_state"
            elif [[ "$pr_state" == "none" ]]; then
                reason="no PR (${age}s)"
            else
                reason="PR $pr_state"
            fi
            
            log "KILLING $session_name: $reason"
            
            # Kill the session
            if kill_session "$session_name"; then
                ((killed_count++))
                
                # Remove worktree if exists
                if [[ -n "$worktree_path" && -d "$worktree_path" ]]; then
                    remove_worktree "$worktree_path"
                    log "Removed worktree: $worktree_path"
                fi
            else
                log "Failed to kill session: $session_name"
            fi
        fi
        
    done < <(tmux list-sessions 2>/dev/null | grep '^jc-')
    
    log "=== Session reaper complete: $killed_count sessions killed ==="
}

# Run main
main "$@"
