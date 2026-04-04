#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_ROOT="${SYMPHONY_DAEMON_RUNTIME:-$HOME/Library/Application Support/jleechanclaw/symphony_daemon}"
METADATA="$RUNTIME_ROOT/daemon_metadata.json"

if [[ ! -f "$METADATA" ]]; then
  echo "Missing metadata: $METADATA" >&2
  echo "Run scripts/setup-symphony-daemon.py first." >&2
  exit 1
fi

NODE_NAME="$(jq -r '.node_name' "$METADATA")"
COOKIE="$(jq -r '.cookie' "$METADATA")"
SYMPHONY_ELIXIR_DIR="$(jq -r '.symphony_elixir_dir' "$METADATA")"
MISE_BIN="${MISE_BIN:-$(jq -r '.mise_bin // empty' "$METADATA")}"
if [[ -z "$MISE_BIN" ]]; then
  MISE_BIN="/opt/homebrew/bin/mise"
fi
if [[ ! -x "$MISE_BIN" ]]; then
  if command -v mise >/dev/null 2>&1; then
    MISE_BIN="$(command -v mise)"
  else
    echo "Missing mise binary: $MISE_BIN" >&2
    exit 1
  fi
fi

PLUGIN_NAME="${SYMPHONY_TASK_PLUGIN:-generic_tasks}"
PLUGIN_INPUT="${SYMPHONY_TASK_PLUGIN_INPUT:-}"
if [[ -z "$PLUGIN_INPUT" ]]; then
  echo "SYMPHONY_TASK_PLUGIN_INPUT is required" >&2
  exit 1
fi
ISSUES_JSON="${SYMPHONY_TASK_ISSUES_JSON:-$RUNTIME_ROOT/issues.$(date +%s).json}"
SYMPHONY_MEMORY_QUEUE_MODE="${SYMPHONY_MEMORY_QUEUE_MODE:-benchmark-only}"

SYMPHONY_TASK_PLUGIN="$PLUGIN_NAME" \
SYMPHONY_TASK_PLUGIN_INPUT="$PLUGIN_INPUT" \
SYMPHONY_TASK_ISSUES_JSON="$ISSUES_JSON" \
python3 "$ROOT_DIR/scripts/prepare-symphony-payload.py"

enqueue_to_memory_queue=0
if [[ "$SYMPHONY_MEMORY_QUEUE_MODE" == "benchmark-only" ]]; then
  if [[ "$PLUGIN_NAME" == "leetcode_hard" || "$PLUGIN_NAME" == "swe_bench_verified" ]]; then
    enqueue_to_memory_queue=1
  fi
elif [[ "$SYMPHONY_MEMORY_QUEUE_MODE" == "always" ]]; then
  enqueue_to_memory_queue=1
elif [[ "$SYMPHONY_MEMORY_QUEUE_MODE" == "never" ]]; then
  enqueue_to_memory_queue=0
else
  echo "Invalid SYMPHONY_MEMORY_QUEUE_MODE: $SYMPHONY_MEMORY_QUEUE_MODE (expected: benchmark-only|always|never)" >&2
  exit 1
fi

if [[ "$enqueue_to_memory_queue" -eq 1 ]]; then
  cd "$SYMPHONY_ELIXIR_DIR"
  "$MISE_BIN" exec -- epmd -daemon || true
  SYMPHONY_DAEMON_NODE="$NODE_NAME" \
  SYMPHONY_DAEMON_COOKIE="$COOKIE" \
  TASK_ISSUES_JSON="$ISSUES_JSON" \
  "$MISE_BIN" exec -- mix run "$ROOT_DIR/scripts/enqueue-symphony-memory-issues.exs"
else
  echo "Skipping memory_tracker_issues RPC enqueue for plugin=$PLUGIN_NAME mode=$SYMPHONY_MEMORY_QUEUE_MODE"
  echo "prepared_issues_json=$ISSUES_JSON"
fi
