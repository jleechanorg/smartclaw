#!/usr/bin/env bash
# Raise ao doctor's lifecycle-worker budget from hardcoded 3 to a configurable max.
#
# Upstream jleechanorg/agent-orchestrator warns when total_count > 3. Multi-project
# harnesses (smartclaw + agent-orchestrator + several product repos) legitimately
# run more workers — one per project. This patch replaces that check with:
#   AO_DOCTOR_MAX_LIFECYCLE_WORKERS (default 8)
#
# Usage:
#   bash scripts/patch-ao-doctor-lifecycle-max.sh
#   AO_REPO_ROOT=/path/to/agent-orchestrator bash scripts/patch-ao-doctor-lifecycle-max.sh
#
# Idempotent: skips if the file already contains AO_DOCTOR_MAX_LIFECYCLE_WORKERS.
set -euo pipefail

AO_REPO="${AO_REPO_ROOT:-$HOME/project_agento/agent-orchestrator}"
TARGET="$AO_REPO/scripts/ao-doctor.sh"

if [[ ! -f "$TARGET" ]]; then
  echo "SKIP: ao-doctor.sh not found at $TARGET (set AO_REPO_ROOT to your agent-orchestrator clone)" >&2
  exit 0
fi

if grep -q 'AO_DOCTOR_MAX_LIFECYCLE_WORKERS' "$TARGET"; then
  echo "OK: $TARGET already uses AO_DOCTOR_MAX_LIFECYCLE_WORKERS — no patch needed"
  exit 0
fi

python3 - "$TARGET" <<'PY'
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")

# Match Check 2 block (jleechanorg ao-doctor.sh uses 2 spaces for if/elif/fi, 4 for warn/pass bodies).
pat = re.compile(
    r"  # --- Check 2: total worker count sanity \(warn if > 3 regardless of binary\) ---\s*\n"
    r'  if \[ "\$total_count" -gt 3 \]; then\s*\n'
    r'    warn "unusually high lifecycle-worker count: \$total_count \(expected ≤3\)\. This drains GraphQL quota rapidly\."\s*\n'
    r'  elif \[ "\$total_count" -gt 0 \]; then\s*\n'
    r'    pass "total lifecycle-worker count is \$total_count \(within normal range\)"\s*\n'
    r"  fi",
)

def repl(_m: re.Match[str]) -> str:
    return (
        "  # --- Check 2: total worker count sanity ---\n"
        "  # Default max 8: multi-project setups commonly run one worker per active project.\n"
        '  max_workers="${AO_DOCTOR_MAX_LIFECYCLE_WORKERS:-8}"\n'
        '  if ! [[ "$max_workers" =~ ^[0-9]+$ ]]; then max_workers=8; fi\n'
        '  if [ "$total_count" -gt "$max_workers" ]; then\n'
        '    warn "unusually high lifecycle-worker count: $total_count (expected ≤$max_workers). '
        'Set AO_DOCTOR_MAX_LIFECYCLE_WORKERS to raise this budget. High counts drain GraphQL quota rapidly."\n'
        '  elif [ "$total_count" -gt 0 ]; then\n'
        '    pass "total lifecycle-worker count is $total_count (within normal range ≤$max_workers)"\n'
        "  fi"
    )

new_text, n = pat.subn(repl, text, count=1)
if n != 1:
    print(f"ERROR: Check 2 block not found in {path} (ao-doctor.sh layout changed?)", file=sys.stderr)
    sys.exit(1)
path.write_text(new_text, encoding="utf-8")
print(f"Patched: {path}")
PY

echo "Done. Re-run: ao doctor"
