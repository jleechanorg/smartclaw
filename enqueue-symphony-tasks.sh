#!/usr/bin/env bash
# enqueue-symphony-tasks.sh - Enqueue tasks to the Symphony daemon
#
# Usage:
#   enqueue-symphony-tasks.sh [task_file]
#
# Reads tasks from a file (one per line) or stdin, dispatches each via sym-dispatch.sh.
# SYMPHONY_MEMORY_QUEUE_MODE defaults to benchmark-only for direct calls.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export SYMPHONY_MEMORY_QUEUE_MODE="${SYMPHONY_MEMORY_QUEUE_MODE:-benchmark-only}"

task_file="${1:-}"
if [ -n "$task_file" ]; then
  input="$(<"$task_file")"
else
  input="$(cat)"
fi

while IFS= read -r task; do
  [ -z "$task" ] && continue
  echo "Dispatching: $task"
  "$SCRIPT_DIR/scripts/sym-dispatch.sh" "$task"
done <<< "$input"
