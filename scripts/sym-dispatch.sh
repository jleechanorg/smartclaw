#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_ROOT="${SYMPHONY_DAEMON_RUNTIME:-$HOME/Library/Application Support/jleechanclaw/symphony_daemon}"
METADATA="$RUNTIME_ROOT/daemon_metadata.json"

ensure_daemon() {
  if [[ ! -f "$METADATA" ]]; then
    PYTHONPATH="$ROOT_DIR/src" python3 "$ROOT_DIR/scripts/setup-symphony-daemon.py"
  fi
}

build_generic_input() {
  local task_text="$1"
  local ts
  ts="$(date +%s)"
  local input_path="$RUNTIME_ROOT/generic_task_${ts}.json"

  mkdir -p "$RUNTIME_ROOT"
  python3 - "$input_path" "$task_text" <<'PY'
import json
import sys
import uuid
from pathlib import Path

out = Path(sys.argv[1])
task = sys.argv[2].strip()
if not task:
    raise SystemExit("sym-dispatch requires a non-empty task")

words = task.split()
summary = " ".join(words[:8])
task_id = uuid.uuid4().hex[:12]
out.write_text(
    json.dumps(
        {
            "tasks": [
                {
                    "id": task_id,
                    "title": summary,
                    "description": task,
                    "labels": ["adhoc"],
                }
            ]
        },
        indent=2,
    ),
    encoding="utf-8",
)
print(out)
PY
}

main() {
  ensure_daemon

  if [[ "${1:-}" == "--plugin" ]]; then
    local plugin="${2:-}"
    local input="${3:-}"
    if [[ -z "$plugin" || -z "$input" ]]; then
      echo "Usage: sym-dispatch.sh --plugin <plugin_name> <input_json>" >&2
      exit 1
    fi
    SYMPHONY_TASK_PLUGIN="$plugin" \
    SYMPHONY_TASK_PLUGIN_INPUT="$input" \
    "$ROOT_DIR/scripts/enqueue-symphony-tasks.sh"
    exit 0
  fi

  local task_text="$*"
  if [[ -z "$task_text" ]]; then
    echo "Usage: sym-dispatch.sh <task text>" >&2
    echo "   or: sym-dispatch.sh --plugin <plugin_name> <input_json>" >&2
    exit 1
  fi

  local input_path
  input_path="$(build_generic_input "$task_text")"

  SYMPHONY_TASK_PLUGIN="generic_tasks" \
  SYMPHONY_TASK_PLUGIN_INPUT="$input_path" \
  SYMPHONY_MEMORY_QUEUE_MODE="${SYMPHONY_MEMORY_QUEUE_MODE:-always}" \
  "$ROOT_DIR/scripts/enqueue-symphony-tasks.sh"
}

main "$@"
