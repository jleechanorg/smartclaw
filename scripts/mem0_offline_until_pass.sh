#!/usr/bin/env bash
set -euo pipefail

TARGET="${TARGET_PASS_RATE:-1.0}"
MAX_ATTEMPTS="${MAX_ATTEMPTS:-20}"
SLEEP_SECONDS="${SLEEP_SECONDS:-2}"

attempt=1
while (( attempt <= MAX_ATTEMPTS )); do
  echo "== mem0 offline attempt $attempt/$MAX_ATTEMPTS =="
  node scripts/mem0_offline_50q.mjs | tee /tmp/openclaw-mem0-offline-last-run.json

  score_file="/tmp/openclaw-mem0-offline/latest/score.json"
  if [[ ! -f "$score_file" ]]; then
    echo "missing score file: $score_file"
    exit 1
  fi

  pass_rate="$(python3 - <<'PY'
import json
print(json.load(open('/tmp/openclaw-mem0-offline/latest/score.json'))['pass_rate'])
PY
)"

  echo "pass_rate=$pass_rate target=$TARGET"
  if python3 - "$pass_rate" "$TARGET" <<'PY'
import sys
rate=float(sys.argv[1]); target=float(sys.argv[2])
sys.exit(0 if rate >= target else 1)
PY
  then
    echo "SUCCESS: mem0 offline suite reached target pass rate."
    exit 0
  fi

  ((attempt++))
  sleep "$SLEEP_SECONDS"
done

echo "FAILED: mem0 offline suite did not reach target pass rate in $MAX_ATTEMPTS attempts."
exit 1
