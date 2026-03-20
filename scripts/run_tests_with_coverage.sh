#!/bin/bash

# OpenClaw Test Coverage Script (Adapted for TypeScript/Node.js)
# Runs vitest with coverage reporting
#
# Usage:
#   ./run_tests_with_coverage.sh           # Run all tests with coverage
#   ./run_tests_with_coverage.sh unit      # Unit tests only
#   ./run_tests_with_coverage.sh e2e       # E2E tests only
#   ./run_tests_with_coverage.sh fast      # Fast unit tests
#   ./run_tests_with_coverage.sh docker    # Docker-based tests

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
TEST_MODE="${1:-all}"  # Options: all, unit, e2e, fast, docker
COVERAGE_THRESHOLD="${COVERAGE_THRESHOLD:-80}"

echo -e "${BLUE}🧪 Running OpenClaw Tests with Coverage${NC}"
echo "=================================================="
echo "Test mode: $TEST_MODE"
echo "Coverage threshold: ${COVERAGE_THRESHOLD}%"
echo ""

# Function to run tests with error handling
# Arguments: test_name emoji cmd [args...]
run_test() {
    local test_name="$1"
    local emoji="$2"
    shift 2

    echo -e "\n${BLUE}${emoji} Running ${test_name}...${NC}"
    echo "Command: $*"

    if "$@"; then
        echo -e "${GREEN}✅ ${test_name}: PASSED${NC}"
        return 0
    else
        echo -e "${RED}❌ ${test_name}: FAILED${NC}"
        return 1
    fi
}

# Track overall status
overall_status=0

# Run tests based on mode
case "$TEST_MODE" in
    "fast")
        echo -e "${BLUE}⚡ Running fast tests${NC}"
        if ! run_test "Fast Tests" "⚡" pnpm test:fast; then
            overall_status=1
        fi
        ;;

    "unit")
        echo -e "${BLUE}🧪 Running unit tests${NC}"
        if ! run_test "Unit Tests" "🧪" pnpm test:unit; then
            overall_status=1
        fi
        ;;

    "e2e")
        echo -e "${BLUE}🔄 Running end-to-end tests${NC}"
        if ! run_test "E2E Tests" "🔄" pnpm test:e2e; then
            overall_status=1
        fi
        ;;

    "coverage")
        echo -e "${BLUE}📊 Running tests with coverage${NC}"
        if ! run_test "Coverage Tests" "📊" pnpm test:coverage; then
            overall_status=1
        fi

        # Check coverage thresholds
        if [ -f "coverage/coverage-summary.json" ]; then
            echo -e "\n${BLUE}📈 Coverage Summary:${NC}"
            if command -v jq &>/dev/null; then
                jq '.total' coverage/coverage-summary.json

                # Enforce the threshold — fail the build if lines coverage is below it.
                actual_pct=$(jq '.total.lines.pct // 0' coverage/coverage-summary.json)
                # Use awk for floating-point comparison (bash arithmetic only handles integers).
                if awk -v actual="$actual_pct" -v threshold="$COVERAGE_THRESHOLD" \
                        'BEGIN { exit (actual >= threshold) ? 0 : 1 }'; then
                    echo -e "${GREEN}Coverage ${actual_pct}% meets threshold ${COVERAGE_THRESHOLD}%${NC}"
                else
                    echo -e "${RED}Coverage ${actual_pct}% is below threshold ${COVERAGE_THRESHOLD}%${NC}"
                    overall_status=1
                fi
            else
                echo "Install jq to view summary, or open coverage/index.html"
                echo "WARNING: Cannot enforce coverage threshold without jq."
            fi
        fi
        ;;

    "all")
        echo -e "${BLUE}🚀 Running full test suite${NC}"

        # Build first
        echo -e "\n${BLUE}🔨 Building project${NC}"
        if ! run_test "Build" "🔨" pnpm build; then
            overall_status=1
        fi

        # Run all tests
        if ! run_test "Full Test Suite" "🧪" pnpm test; then
            overall_status=1
        fi

        # Run coverage
        echo -e "\n${BLUE}📊 Generating coverage report${NC}"
        if ! run_test "Coverage Report" "📊" pnpm test:coverage; then
            overall_status=1
        fi
        ;;

    "docker")
        echo -e "${BLUE}🐳 Running Docker-based tests${NC}"
        if ! run_test "Docker Tests" "🐳" pnpm test:docker:all; then
            overall_status=1
        fi
        ;;

    *)
        echo -e "${RED}❌ Unknown test mode: $TEST_MODE${NC}"
        echo "Valid modes: all, unit, e2e, fast, coverage, docker"
        exit 1
        ;;
esac

# Summary
echo -e "\n=================================================="
if [[ $overall_status -eq 0 ]]; then
    echo -e "${GREEN}🎉 ALL TESTS PASSED!${NC}"

    # Show coverage report location if available
    if [ -f "coverage/index.html" ]; then
        echo -e "${BLUE}📊 Coverage report: coverage/index.html${NC}"
        if [[ "$OSTYPE" == "darwin"* ]]; then
            echo -e "${YELLOW}   Open with: open coverage/index.html${NC}"
        else
            echo -e "${YELLOW}   Open with: xdg-open coverage/index.html${NC}"
        fi
    fi
else
    echo -e "${RED}❌ SOME TESTS FAILED${NC}"
fi

echo -e "\n${BLUE}📊 Test Summary:${NC}"
echo "  • Mode: $TEST_MODE"
echo "  • Framework: vitest"
echo "  • Coverage threshold: ${COVERAGE_THRESHOLD}%"

exit $overall_status
