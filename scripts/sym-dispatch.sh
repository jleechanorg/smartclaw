#!/usr/bin/env bash
# sym-dispatch.sh - Dispatch tasks to the Symphony daemon
#
# Usage:
#   sym-dispatch.sh "<task text>"
#   sym-dispatch.sh --plugin <plugin_name> <input_json>

set -euo pipefail

SYMPHONY_SOCKET="${SYMPHONY_SOCKET:-$HOME/.openclaw/symphony/daemon.sock}"
SYMPHONY_MEMORY_QUEUE_MODE="${SYMPHONY_MEMORY_QUEUE_MODE:-always}"

if [ ! -S "$SYMPHONY_SOCKET" ]; then
  echo "ERROR: Symphony daemon socket not found at $SYMPHONY_SOCKET" >&2
  echo "Run scripts/install-symphony-daemon.sh to install the daemon." >&2
  exit 1
fi

if [ "${1:-}" = "--plugin" ]; then
  plugin_name="${2:?plugin name required}"
  input_json="${3:?input JSON required}"
  payload="{\"type\":\"plugin\",\"plugin\":\"$plugin_name\",\"input\":$input_json,\"memory_queue_mode\":\"$SYMPHONY_MEMORY_QUEUE_MODE\"}"
else
  task_text="${1:?task text required}"
  payload="{\"type\":\"task\",\"task\":$(printf '%s' "$task_text" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))'),\"memory_queue_mode\":\"$SYMPHONY_MEMORY_QUEUE_MODE\"}"
fi

echo "$payload" | nc -U "$SYMPHONY_SOCKET"
