#!/bin/bash
# backup_cron_jobs.sh — exports current cron jobs to docs/context/CRON_JOBS_BACKUP.md
set -euo pipefail

OPENCLAW_BASE="${REPO_DIR:-${OPENCLAW_ROOT:-${OPENCLAW_DIR:-$HOME/.smartclaw}}}"
DOCS="$OPENCLAW_BASE/docs/context"
OUT="$DOCS/CRON_JOBS_BACKUP.md"
TMP_JSON="$OUT.json.$$"
TMP_OUT="$OUT.tmp.$$"
cleanup() { rm -f "$TMP_JSON" "$TMP_OUT"; }
trap cleanup EXIT

mkdir -p "$DOCS"

# Use openclaw cron list --json for cron jobs.
# Verify subcommand exists before calling (openclaw CLI can hang on unknown subcommands).
if ! openclaw cron list --help 2>&1 | head -3 >/dev/null; then
    echo "ERROR: 'openclaw cron list' subcommand not available" >&2
    exit 1
fi

# Capture JSON to temp file; strip non-JSON prefix (plugin/config warnings).
RAW=$(timeout 30 openclaw cron list --json 2>/dev/null || true)
if [[ -z "$RAW" ]]; then
    echo "ERROR: openclaw cron list --json returned no output (may have hung or failed)" >&2
    exit 1
fi
# Strip everything before the first '{' to remove plugin/warning lines
JSON_START=$(echo "$RAW" | awk '/^\{/{found=1} found{print}')
if [[ -z "$JSON_START" ]]; then
    echo "ERROR: No JSON object found in openclaw cron list output" >&2
    exit 1
fi
echo "$JSON_START" > "$TMP_JSON"

python3 - "$TMP_JSON" "$TMP_OUT" <<'PYEOF'
import json, sys
from datetime import datetime

_, json_path, out_path = sys.argv
data = json.load(open(json_path))
jobs = data.get('jobs', [])
ts = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')

lines = [
    "# Cron Jobs Backup",
    "",
    f"Exported: {ts}",
    f"Total jobs: {len(jobs)}",
    "",
]
for j in jobs:
    sched = j.get('schedule', {})
    kind = sched.get('kind', '?')
    if kind == 'cron':
        sched_str = sched.get('expr', '?')
    elif kind == 'every':
        ms = sched.get('everyMs', '?')
        if isinstance(ms, int):
            mins = ms // 60000
            sched_str = f"every {mins}m"
        else:
            sched_str = f"every {ms}"
    else:
        sched_str = f"{kind} {sched.get('everyMs', sched.get('expr', '?'))}"

    name = j.get('name', '?')
    jid = j.get('id', 'no-id')
    desc = j.get('description', '')
    enabled = j.get('enabled', False)
    state = j.get('state', {})
    last_run = state.get('lastRunAtMs')
    last_status = state.get('lastRunStatus', '?')

    lines.append(f"## {name}")
    lines.append(f"- ID: `{jid}`")
    lines.append(f"- Enabled: {enabled}")
    lines.append(f"- Schedule: `{sched_str}`")
    lines.append(f"- Description: {desc}")
    if last_run:
        dt = datetime.utcfromtimestamp(last_run/1000).strftime('%Y-%m-%d %H:%M')
        lines.append(f"- Last run: {dt} ({last_status})")
    lines.append("")

content = '\n'.join(lines) + '\n'
with open(out_path, 'w') as f:
    f.write(content)
print(content, end='')
PYEOF

mv "$TMP_OUT" "$OUT"
echo "Written: $OUT"
