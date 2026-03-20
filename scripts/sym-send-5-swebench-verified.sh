#!/usr/bin/env bash
# sym-send-5-swebench-verified.sh - Enqueue 5 SWE-bench verified tasks to Symphony
#
# Sets SYMPHONY_MEMORY_QUEUE_MODE=benchmark-only for benchmark isolation.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export SYMPHONY_MEMORY_QUEUE_MODE="${SYMPHONY_MEMORY_QUEUE_MODE:-benchmark-only}"

tasks=(
  "SWE-bench: Fix pytest issue with fixture teardown ordering"
  "SWE-bench: Fix requests library SSL verification fallback"
  "SWE-bench: Fix Django ORM related field caching bug"
  "SWE-bench: Fix Flask session cookie handling for subdomains"
  "SWE-bench: Fix numpy broadcasting edge case for scalar arrays"
)

for task in "${tasks[@]}"; do
  echo "Dispatching: $task"
  "$SCRIPT_DIR/sym-dispatch.sh" "$task"
done
