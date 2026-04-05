#!/bin/bash
# sync-check.sh - Check and sync OpenClaw config between jleechanclaw, openclaw_config, and ~/.openclaw
#
# Usage: ./sync-check.sh [--fix]
#   --fix: Apply fixes to ~/.openclaw/workspace/ (openclaw_config canonical location)
#
# Key policy files tracked in openclaw_config:
#   Root: AGENTS.md, SOUL.md, TOOLS.md, USER.md, IDENTITY.md, HEARTBEAT.md, CLAUDE.md
#   Agents: main, memqa, monitor (auth-profiles.json, models.json)

set -e

WORKSPACE_DIR="$HOME/.openclaw/workspace"
CLAUDE_REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

POLICY_FILES="AGENTS.md SOUL.md TOOLS.md USER.md IDENTITY.md HEARTBEAT.md CLAUDE.md"
AGENTS="main memqa monitor"
AGENT_FILES="auth-profiles.json models.json"

echo -e "${BLUE}=== OpenClaw Sync Check ===${NC}"
echo "Workspace:   $WORKSPACE_DIR"
echo "CLAUDE repo: $CLAUDE_REPO_DIR"
echo ""

# Check if we're in the right directory
if [[ ! -d "$WORKSPACE_DIR/.git" ]]; then
    echo -e "${RED}Error: Workspace directory is not a git repo: $WORKSPACE_DIR${NC}"
    exit 1
fi

# Get commit info from workspace
cd "$WORKSPACE_DIR"
WORKSPACE_COMMIT=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
WORKSPACE_BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
WORKSPACE_URL=$(git remote get-url origin 2>/dev/null || echo "unknown")

echo "Workspace git: $WORKSPACE_URL (@ $WORKSPACE_COMMIT, branch: $WORKSPACE_BRANCH)"
echo ""

# Check OpenClaw status
echo "=== OpenClaw Status ==="
if openclaw gateway status > /dev/null 2>&1; then
    echo -e "${GREEN}✓ OpenClaw gateway is running${NC}"
else
    echo -e "${RED}✗ OpenClaw gateway is NOT running${NC}"
fi
echo ""

# Compare policy files
echo "=== Root Policy Files ==="
echo ""

SYNC_ISSUES=0

for file in $POLICY_FILES; do
    workspace_file="$WORKSPACE_DIR/$file"
    home_file="$HOME/.openclaw/$file"

    if [[ ! -f "$workspace_file" ]]; then
        echo -e "${RED}MISSING in workspace: $file${NC}"
        SYNC_ISSUES=$((SYNC_ISSUES + 1))
        continue
    fi

    # Check if home differs from workspace
    if [[ -f "$home_file" ]] && ! diff -q "$workspace_file" "$home_file" > /dev/null 2>&1; then
        echo -e "${YELLOW}DIFF: ~/.openclaw/$file vs workspace/$file${NC}"

        if [[ "$1" == "--fix" ]]; then
            cp "$workspace_file" "$home_file"
            echo -e "  ${GREEN}Fixed: copied workspace -> ~/.openclaw/${NC}"
        else
            echo -e "  Run with --fix to sync"
        fi
    else
        echo -e "${GREEN}OK: $file${NC}"
    fi
done

echo ""

# Compare agent files
echo "=== Agent Files ==="
echo ""

for agent in $AGENTS; do
    echo "--- $agent ---"

    workspace_agent_dir="$WORKSPACE_DIR/openclaw-config/agents/$agent/agent"
    home_agent_dir="$HOME/.openclaw/agents/$agent/agent"

    if [[ ! -d "$home_agent_dir" ]]; then
        echo -e "${RED}  Agent dir not found: $home_agent_dir${NC}"
        continue
    fi

    for file in $AGENT_FILES; do
        workspace_file="$workspace_agent_dir/$file"
        home_file="$home_agent_dir/$file"

        # Skip if workspace doesn't have this agent
        if [[ ! -d "$workspace_agent_dir" ]]; then
            echo -e "${YELLOW}  $agent: not in workspace (live only)${NC}"
            break
        fi

        if [[ ! -f "$workspace_file" ]]; then
            continue
        fi

        if ! diff -q "$workspace_file" "$home_file" > /dev/null 2>&1; then
            echo -e "${YELLOW}  DIFF: $agent/$file${NC}"

            if [[ "$1" == "--fix" ]]; then
                cp "$workspace_file" "$home_file"
                echo -e "    ${GREEN}Fixed: copied workspace -> live${NC}"
            else
                echo -e "    Run with --fix to sync"
            fi
        else
            echo -e "${GREEN}  OK: $agent/$file${NC}"
        fi
    done
done

echo ""

# Summary
echo "=== Summary ==="
cd "$WORKSPACE_DIR"
echo "Workspace commit: $(git log -1 --oneline)"
echo ""

if [[ $SYNC_ISSUES -gt 0 ]]; then
    echo -e "${RED}$SYNC_ISSUES files missing in workspace${NC}"
    exit 1
else
    echo -e "${GREEN}All policy files in sync${NC}"
fi

# Note about jleechanclaw
if [[ -d "$CLAUDE_REPO_DIR/src" ]]; then
    echo ""
    echo "Note: jleechanclaw contains orchestration code only."
    echo "Policy files are canonical in openclaw_config (workspace)."
fi
