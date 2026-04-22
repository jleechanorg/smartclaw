#!/usr/bin/env bash
# Teach ao doctor to accept both the ao wrapper path and its resolved JS entrypoint.
#
# smartclaw launches lifecycle-workers through ~/bin/ao in some places and through
# node-resolved dist/index.js in others. Older ao-doctor builds only recognized tokens
# ending in /ao, so node-launched workers were falsely flagged as non-canonical.
#
# Usage:
#   bash scripts/patch-ao-doctor-canonical-binary.sh
#   AO_REPO_ROOT=/path/to/agent-orchestrator bash scripts/patch-ao-doctor-canonical-binary.sh
#
# Idempotent: skips when the target already contains AO_DOCTOR_ACCEPT_NODE_ENTRYPOINTS.
set -euo pipefail

AO_REPO="${AO_REPO_ROOT:-$HOME/project_agento/agent-orchestrator}"
TARGET="$AO_REPO/scripts/ao-doctor.sh"

if [[ ! -f "$TARGET" ]]; then
  echo "SKIP: ao-doctor.sh not found at $TARGET (set AO_REPO_ROOT to your agent-orchestrator clone)" >&2
  exit 0
fi

if grep -q 'AO_DOCTOR_ACCEPT_NODE_ENTRYPOINTS' "$TARGET"; then
  echo "OK: $TARGET already accepts node entrypoints — no patch needed"
  exit 0
fi

python3 - "$TARGET" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
old = """      local cmd\n      cmd=\"$(printf '%s' \"$line\" | awk '{for(i=1;i<=NF;i++) if($i ~ /\\/ao$/) {print $i; exit}}')\"\n      if [ -z \"$cmd\" ] || { [ \"$cmd\" != \"${canonical_binary}\" ] && [ \"$cmd\" != \"${canonical_real}\" ]; }; then\n        stale_count=$((stale_count + 1))\n        local pid\n        pid=\"$(echo \"$line\" | awk '{print $2}')\"\n        stale_pids=\"$stale_pids $pid\"\n        warn \"non-canonical lifecycle-worker binary detected: PID=$pid binary contains: $(echo \"$line\" | grep -oE '/[^ ]+lifecycle|[^ ]+/ao' | head -1 || echo \"unknown\")\"\n      fi\n"""
new = """      local cmd cmd_real js_entry js_entry_real canonical_ok\n      # AO_DOCTOR_ACCEPT_NODE_ENTRYPOINTS: accept both the ao wrapper path and the\n      # resolved JS entrypoint shown by node-launched lifecycle-workers.\n      cmd=\"$(printf '%s' \"$line\" | awk '{for(i=1;i<=NF;i++) if($i ~ /\\/ao$/) {print $i; exit}}')\"\n      cmd_real=\"\"\n      if [ -n \"$cmd\" ]; then\n        cmd_real=\"$(realpath \"$cmd\" 2>/dev/null || printf '%s' \"$cmd\")\"\n      fi\n      js_entry=\"$(printf '%s' \"$line\" | awk '{for(i=1;i<=NF;i++) if($i ~ /\\/dist\\/index\\.js$/ || $i ~ /\\/bin\\/ao\\.js$/) {print $i; exit}}')\"\n      js_entry_real=\"\"\n      if [ -n \"$js_entry\" ]; then\n        js_entry_real=\"$(realpath \"$js_entry\" 2>/dev/null || printf '%s' \"$js_entry\")\"\n      fi\n      canonical_ok=0\n      if [ -n \"$cmd\" ] && { [ \"$cmd\" = \"${canonical_binary}\" ] || [ \"$cmd_real\" = \"${canonical_real}\" ]; }; then\n        canonical_ok=1\n      elif [ -n \"$js_entry\" ] && [ \"$js_entry_real\" = \"${canonical_real}\" ]; then\n        canonical_ok=1\n      fi\n      if [ \"$canonical_ok\" -ne 1 ]; then\n        stale_count=$((stale_count + 1))\n        local pid\n        pid=\"$(echo \"$line\" | awk '{print $2}')\"\n        stale_pids=\"$stale_pids $pid\"\n        warn \"non-canonical lifecycle-worker binary detected: PID=$pid binary contains: $(echo \"$line\" | grep -oE '/[^ ]+lifecycle|[^ ]+/ao|/[^ ]+/dist/index\\.js|/[^ ]+/bin/ao\\.js' | head -1 || echo \"unknown\")\"\n      fi\n"""
if old not in text:
    print(f"ERROR: expected lifecycle-worker canonical-binary block not found in {path}", file=sys.stderr)
    sys.exit(1)
path.write_text(text.replace(old, new, 1), encoding="utf-8")
print(f"Patched: {path}")
PY

echo "Done. Re-run: ao doctor"
