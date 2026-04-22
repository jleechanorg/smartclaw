#!/bin/bash
#
# Harness Engineering Analyzer
# Daily 9am job that analyzes smartclaw for harness engineering violations
# and creates PRs to fix issues + comments on open PRs with suggestions
#

set -e

REPO="jleechanorg/smartclaw"
WORK_DIR="/tmp/harness-analyzer-$$"
LOG_FILE="$HOME/.smartclaw/logs/harness-analyzer.log"
GITHUB_TOKEN_SOURCE="${GITHUB_TOKEN:-$HOME/.github_token}"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

error() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: $1" | tee -a "$LOG_FILE" >&2
}

# Ensure log directory exists
mkdir -p "$(dirname "$LOG_FILE")"

log "=== Starting Harness Engineering Analysis ==="

resolve_gh_token() {
    # Priority:
    # 1) Existing GH_TOKEN env var (token literal)
    # 2) GITHUB_TOKEN env var literal (if not a readable file path)
    # 3) GITHUB_TOKEN env var as file path
    # 4) Default token file path (~/.github_token)
    if [ -n "${GH_TOKEN:-}" ]; then
        printf '%s' "$GH_TOKEN"
        return 0
    fi

    # If GITHUB_TOKEN is set to a non-readable-file path, treat it as a literal token
    # (but not if it's a readable regular file - those should be read as token files)
    if [ -n "${GITHUB_TOKEN:-}" ]; then
        if [ ! -f "$GITHUB_TOKEN" ]; then
            # Not a file path - treat as literal token
            printf '%s' "$GITHUB_TOKEN"
            return 0
        elif [ -f "$GITHUB_TOKEN" ] && [ -r "$GITHUB_TOKEN" ]; then
            # It's a readable regular file - fall through to read it below
            local token_path="$GITHUB_TOKEN"
        else
            # File exists but not readable - either fall back to default or error
            local token_path="$GITHUB_TOKEN_SOURCE"
        fi
    else
        # GITHUB_TOKEN not set - use default
        local token_path="$GITHUB_TOKEN_SOURCE"
    fi

    # Read token from resolved path (must be a regular readable file)
    if [ -f "$token_path" ] && [ -r "$token_path" ] && [ ! -d "$token_path" ]; then
        tr -d '\r\n' < "$token_path"
        return 0
    fi

    return 1
}

# Check for GitHub token
if ! GH_TOKEN_VALUE="$(resolve_gh_token)" || [ -z "$GH_TOKEN_VALUE" ]; then
    error "GitHub token not found. Set GH_TOKEN, set GITHUB_TOKEN, or create $GITHUB_TOKEN_SOURCE"
    exit 1
fi

export GH_TOKEN="$GH_TOKEN_VALUE"

# Clone repo
log "Cloning $REPO..."
rm -rf "$WORK_DIR"
gh repo clone "$REPO" "$WORK_DIR" -- --depth 1
cd "$WORK_DIR"

ISSUES_FOUND=()

# Function to check for missing harness files
check_harness_files() {
    log "Checking for required harness files..."
    
    local required_files=(
        "CLAUDE.md"
        "AGENTS.md"
        "SOUL.md"
        "TOOLS.md"
        "USER.md"
        "openclaw.json"
        "agent-orchestrator.yaml"
    )
    
    for file in "${required_files[@]}"; do
        if [ ! -f "$file" ]; then
            ISSUES_FOUND+=("Missing required harness file: $file")
        fi
    done
    
    # Check for skills directory
    if [ ! -d "skills" ]; then
        ISSUES_FOUND+=("Missing skills directory")
    fi
    
    # Check for agents directory
    if [ ! -d "agents" ]; then
        ISSUES_FOUND+=("Missing agents directory")
    fi
    
    # Check for launchd jobs
    if [ ! -d "launchd" ]; then
        ISSUES_FOUND+=("Missing launchd directory")
    fi
}

# Function to check documentation quality
check_documentation() {
    log "Checking documentation quality..."
    
    # Check CLAUDE.md for key sections
    if [ -f "CLAUDE.md" ]; then
        if ! grep -q "## Coding Task Routing" CLAUDE.md; then
            ISSUES_FOUND+=("CLAUDE.md missing 'Coding Task Routing' section - should route to agento")
        fi
        if ! grep -q "## Red Lines" CLAUDE.md; then
            ISSUES_FOUND+=("CLAUDE.md missing 'Red Lines' section")
        fi
        if ! grep -q "## Tools" CLAUDE.md; then
            ISSUES_FOUND+=("CLAUDE.md missing 'Tools' section")
        fi
    fi
    
    # Check AGENTS.md for PR criteria
    if [ -f "AGENTS.md" ]; then
        if ! grep -q "PR Green Criteria" AGENTS.md; then
            ISSUES_FOUND+=("AGENTS.md missing 'PR Green Criteria' - should define merge requirements")
        fi
        if ! grep -q "## Red Lines" AGENTS.md; then
            ISSUES_FOUND+=("AGENTS.md missing '## Red Lines' section")
        fi
    fi
    
    # Check SOUL.md for key sections
    if [ -f "SOUL.md" ]; then
        if ! grep -q "## Core Truths" SOUL.md; then
            ISSUES_FOUND+=("SOUL.md missing 'Core Truths' section")
        fi
        if ! grep -q "## Coding Task Routing" SOUL.md; then
            ISSUES_FOUND+=("SOUL.md missing 'Coding Task Routing' - should use agento by default")
        fi
    fi
}

# Function to check agent-orchestrator config
check_ao_config() {
    log "Checking agent-orchestrator configuration..."
    
    if [ -f "agent-orchestrator.yaml" ]; then
        # Check for required reaction configs
        if ! grep -q "reactions:" agent-orchestrator.yaml; then
            ISSUES_FOUND+=("agent-orchestrator.yaml missing 'reactions:' section")
        fi
        
        # Check for CI reaction
        if ! grep -q "ci-failed:" agent-orchestrator.yaml; then
            ISSUES_FOUND+=("agent-orchestrator.yaml missing 'ci-failed:' reaction")
        fi
    else
        ISSUES_FOUND+=("Missing agent-orchestrator.yaml")
    fi
}

# Function to check for deprecated patterns
check_deprecated_patterns() {
    log "Checking for deprecated patterns..."
    
    # Check for mctrl references (should use agento)
    if grep -rq "mctrl" --include="*.md" . 2>/dev/null; then
        ISSUES_FOUND+=("Found 'mctrl' references - should use 'agento' instead")
    fi
    
    # Check for hardcoded cron (should use launchd)
    # Use -rh to output matching lines; filter out crontab_backup/legacy context
    if grep -rh "crontab" --include="*.md" . 2>/dev/null | grep -v "crontab_backup" | grep -v "legacy" | grep -q .; then
        ISSUES_FOUND+=("Found 'crontab' references - should use launchd for macOS scheduling")
    fi
    
    # Check for Socket Mode issues documented in MEMORY.md
    if grep -q "Socket Mode" MEMORY.md 2>/dev/null; then
        # Check if there's a plan to migrate to HTTP
        if ! grep -q "HTTP mode" MEMORY.md 2>/dev/null; then
            ISSUES_FOUND+=("MEMORY.md documents Socket Mode issues but no HTTP migration plan found")
        fi
    fi
}

# Function to check for missing skills
check_skills() {
    log "Checking for required skills..."
    
    local required_skills=(
        "agento"
        "github"
        "discord"
        "slack"
        "weather"
    )
    
    if [ -d "skills" ]; then
        for skill in "${required_skills[@]}"; do
            if [ ! -d "skills/$skill" ]; then
                ISSUES_FOUND+=("Missing skill: $skill")
            fi
        done
    fi
}

# Function to create PR for issues
create_fix_pr() {
    if [ ${#ISSUES_FOUND[@]} -eq 0 ]; then
        log "No issues found - harness looks good!"
        return 0
    fi
    
    log "Found ${#ISSUES_FOUND[@]} issues to fix"
    
    # Create branch
    local branch_name="fix/harness-engineering-$(date +%Y%m%d)"
    git checkout -b "$branch_name" origin/main 2>/dev/null || git checkout -b "$branch_name"
    
    # Create fixes
    # 1. Update CLAUDE.md if missing sections
    if [ -f "CLAUDE.md" ]; then
        if ! grep -q "## Coding Task Routing" CLAUDE.md; then
            cat >> CLAUDE.md << 'EOF'

## Coding Task Routing (Default)

**For all coding tasks, use the `agento` skill (Agent-Orchestrator) by default.**

- Coding tasks = anything involving code changes, PRs, CI fixes, feature implementation
- `agento` → calls `ao` CLI → spawns agent in isolated worktree
- Only use `mctrl` if explicitly requested
EOF
            log "Added Coding Task Routing section to CLAUDE.md"
        fi
    fi
    
    # 2. Update AGENTS.md if missing PR Green Criteria
    if [ -f "AGENTS.md" ] && ! grep -q "PR Green Criteria" AGENTS.md; then
        cat >> AGENTS.md << 'EOF'

## PR Green Criteria — Mandatory Before Any Merge

A PR is only "green" when ALL conditions hold:

| # | Condition |
|---|-----------|
| 1 | `mergeable == true` (no conflicts) |
| 2 | `mergeable_state` not `dirty` or `unstable` |
| 3 | CodeRabbit has reviewed |
| 4 | Bugbot has reviewed |
| 5 | Bugbot's latest review is NOT `CHANGES_REQUESTED` |
| 6 | Evidence PASS comment from self-review |

**Never run `gh pr merge` yourself.** The orchestrator merges automatically.
EOF
        log "Added PR Green Criteria to AGENTS.md"
    fi
    
    # 3. Update SOUL.md if missing sections
    if [ -f "SOUL.md" ]; then
        if ! grep -q "## Coding Task Routing" SOUL.md; then
            cat >> SOUL.md << 'EOF'

## Coding Task Routing (Default)

**For all coding tasks, use the `agento` skill by default.**

- Coding tasks = anything involving code changes, PRs, CI fixes
- Use `ao spawn` or mention "agento" in message
EOF
            log "Added Coding Task Routing to SOUL.md"
        fi
    fi
    
    # Commit changes
    git add -A
    if git diff --cached --quiet; then
        log "No changes to commit"
        git checkout main
        rm -rf "$WORK_DIR"
        return 0
    fi
    
    git commit -m "fix: harness engineering improvements

$(printf '%s\n' "${ISSUES_FOUND[@]}")
"
    
    # Push
    git push -u origin "$branch_name" 2>/dev/null || {
        error "Failed to push branch - may already exist"
        git checkout main
        rm -rf "$WORK_DIR"
        return 1
    }
    
    # Create PR
    local pr_body="## Harness Engineering Analysis

Daily analysis found the following issues:

$(printf '%s\n' "${ISSUES_FOUND[@]}")

### Fixes Applied
- Added missing documentation sections
- Standardized harness engineering patterns

### Verification
Please review the changes and merge when ready.

---
*Generated by harness-analyzer.sh - daily 9am job*"
    
    gh pr create --title "fix: harness engineering improvements ($(date +%Y-%m-%d))" \
        --body "$pr_body" \
        --repo "$REPO" 2>/dev/null || {
        error "Failed to create PR - may already exist"
        return 1
    }
    
    log "Created PR for harness engineering fixes"
    
    # Cleanup
    git checkout main
}

# Function to comment on open PRs
comment_on_open_prs() {
    log "Checking open PRs for harness engineering suggestions..."
    
    local prs
    prs=$(gh pr list --repo "$REPO" --state open --json number,title --jq '.[].number' 2>/dev/null)
    
    for pr_num in $prs; do
        log "Analyzing PR #$pr_num..."
        
        # Get PR diff/stats
        local pr_info
        pr_info=$(gh pr view "$pr_num" --repo "$REPO" --json title,files --jq '.title')
        
        local suggestions=()
        
        # Check PR title for patterns
        if echo "$pr_info" | grep -qi "fix\|bug\|hotfix"; then
            suggestions+=("- Consider adding 'Closes #<issue>' to PR description for issue tracking")
        fi
        
        # Get file changes
        local files
        files=$(gh api "repos/$REPO/pulls/$pr_num/files" --jq '.[].filename' 2>/dev/null | head -20)
        
        # Check for documentation changes
        if echo "$files" | grep -q "\.md$"; then
            suggestions+=("- Documentation changes detected: ensure CLAUDE.md/AGENTS.md are updated if new patterns are introduced")
        fi
        
        # Check for config changes
        if echo "$files" | grep -qE "(openclaw\.json|agent-orchestrator\.yaml|\.claude)"; then
            suggestions+=("- Config changes: verify new settings follow harness engineering principles (deterministic first, LLM for judgment)")
        fi
        
        # Check for skill changes
        if echo "$files" | grep -q "skills/"; then
            suggestions+=("- Skill changes: ensure skill follows SKILL.md template and has proper error handling")
        fi
        
        # Check for launchd changes
        if echo "$files" | grep -q "launchd/"; then
            suggestions+=("- Launchd changes: verify plist follows OpenClaw conventions and has proper environment variables")
        fi
        
        # Add general suggestions
        suggestions+=("- Run `agento status` to check if PR meets green criteria")
        suggestions+=("- Ensure all CodeRabbit comments are addressed before requesting review")
        
        # Check if we already commented today (use issues API for PR body comments)
        local last_comment
        last_comment=$(gh api "repos/$REPO/issues/$pr_num/comments" \
            --jq '[.[] | select(.body | contains("Harness Engineering Suggestions"))] | last | .body' \
            2>/dev/null)
        
        if [ -n "$last_comment" ]; then
            log "Already commented on PR #$pr_num today, skipping"
            continue
        fi
        
        if [ ${#suggestions[@]} -gt 0 ]; then
            local comment_body="## Harness Engineering Suggestions

Hello! This PR was reviewed by the daily harness analyzer.

### Suggestions

$(printf '%s\n' "${suggestions[@]}")

### Resources
- [Harness Engineering Philosophy](../docs/HARNESS_ENGINEERING.md)
- [PR Green Criteria](../AGENTS.md#pr-green-criteria)

---
*Automated review by harness-analyzer.sh*"
            
            gh pr comment "$pr_num" --body "$comment_body" --repo "$REPO" 2>/dev/null
            log "Added harness suggestions to PR #$pr_num"
        fi
    done
}

# Main execution
check_harness_files
check_documentation
check_ao_config
check_deprecated_patterns
check_skills

# Create fix PR if needed
create_fix_pr

# Comment on open PRs
comment_on_open_prs

log "=== Harness Engineering Analysis Complete ==="

# Cleanup
rm -rf "$WORK_DIR"
