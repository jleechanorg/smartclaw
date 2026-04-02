#!/bin/bash

# OpenClaw Linting Script (Adapted for TypeScript/Node.js)
# Runs oxlint and formatting checks for code quality analysis

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
FIX_MODE="${1:-false}"  # Pass 'fix' as argument to auto-fix issues

echo -e "${BLUE}🔍 Running OpenClaw linting checks${NC}"
echo "=================================================="

# Function to run a linter with proper error handling
run_linter() {
    local tool_name="$1"
    local command="$2"
    local emoji="$3"

    echo -e "\n${BLUE}${emoji} Running ${tool_name}...${NC}"
    echo "Command: $command"

    if eval "$command"; then
        echo -e "${GREEN}✅ ${tool_name}: PASSED${NC}"
        return 0
    else
        echo -e "${RED}❌ ${tool_name}: FAILED${NC}"
        return 1
    fi
}

# Track overall status
overall_status=0

# 1. oxlint - Type-aware TypeScript/JavaScript linting
echo -e "\n${BLUE}📋 STEP 1: oxlint (Type-aware Linting)${NC}"
if [[ "$FIX_MODE" == "fix" ]]; then
    oxlint_cmd="pnpm lint:fix"
else
    oxlint_cmd="pnpm lint"
fi

if ! run_linter "oxlint" "$oxlint_cmd" "📋"; then
    overall_status=1
fi

# 2. oxfmt - Formatting check
echo -e "\n${BLUE}🎨 STEP 2: oxfmt (Formatting)${NC}"
if [[ "$FIX_MODE" == "fix" ]]; then
    format_cmd="pnpm format"
else
    format_cmd="pnpm format:check"
fi

if ! run_linter "oxfmt" "$format_cmd" "🎨"; then
    overall_status=1
fi

# 3. TypeScript compilation check
echo -e "\n${BLUE}🔬 STEP 3: TypeScript (Type Checking)${NC}"
ts_cmd="pnpm tsgo"

if ! run_linter "TypeScript" "$ts_cmd" "🔬"; then
    overall_status=1
fi

# 4. Swift linting (if applicable)
if command -v swiftlint >/dev/null 2>&1; then
    echo -e "\n${BLUE}🍎 STEP 4: SwiftLint (iOS/macOS code)${NC}"
    swift_cmd="pnpm lint:swift"

    if ! run_linter "SwiftLint" "$swift_cmd" "🍎"; then
        overall_status=1
    fi
fi

# 5. Documentation linting
echo -e "\n${BLUE}📝 STEP 5: Markdown/Docs Linting${NC}"
if [[ "$FIX_MODE" == "fix" ]]; then
    docs_cmd="pnpm lint:docs:fix"
else
    docs_cmd="pnpm lint:docs"
fi

if ! run_linter "Docs Linting" "$docs_cmd" "📝"; then
    overall_status=1
fi

# 6. OpenClaw cron guardrail
echo -e "\n${BLUE}⏱️ STEP 6: OpenClaw Cron Guardrail${NC}"
cron_guardrail_cmd="pnpm check:cron-guardrail"

if ! run_linter "OpenClaw Cron Guardrail" "$cron_guardrail_cmd" "⏱️"; then
    overall_status=1
fi

# Summary
echo -e "\n=================================================="
if [[ $overall_status -eq 0 ]]; then
    echo -e "${GREEN}🎉 ALL LINTING CHECKS PASSED!${NC}"
    echo -e "${GREEN}✅ oxlint, oxfmt, TypeScript, and docs all successful${NC}"
else
    echo -e "${RED}❌ SOME LINTING CHECKS FAILED${NC}"
    echo -e "${YELLOW}💡 Run with 'fix' argument to auto-fix some issues:${NC}"
    echo -e "${YELLOW}   ./scripts/run_lint.sh fix${NC}"
fi

echo -e "\n${BLUE}📊 Linting Summary:${NC}"
echo "  • Mode: $([ "$FIX_MODE" == "fix" ] && echo "Auto-fix enabled" || echo "Check-only")"
echo "  • Tools: oxlint, oxfmt, TypeScript, SwiftLint, markdownlint, OpenClaw cron guardrail"

exit $overall_status
