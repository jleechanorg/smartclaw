#!/usr/bin/env bash
# sym-send-5-leetcode-hard.sh - Enqueue 5 LeetCode hard problems to Symphony
#
# Sets SYMPHONY_MEMORY_QUEUE_MODE=benchmark-only for benchmark isolation.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export SYMPHONY_MEMORY_QUEUE_MODE="${SYMPHONY_MEMORY_QUEUE_MODE:-benchmark-only}"

tasks=(
  "Solve LeetCode hard: Median of Two Sorted Arrays"
  "Solve LeetCode hard: Regular Expression Matching"
  "Solve LeetCode hard: Merge k Sorted Lists"
  "Solve LeetCode hard: Trapping Rain Water"
  "Solve LeetCode hard: N-Queens"
)

for task in "${tasks[@]}"; do
  echo "Dispatching: $task"
  "$SCRIPT_DIR/sym-dispatch.sh" "$task"
done
