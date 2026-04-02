#!/usr/bin/env bash
set -euo pipefail

REPOS=(
  "$HOME/projects/worldarchitect.ai"
  "$HOME/project_smartclaw/smartclaw"
  "$HOME/project_worldaiclaw/worldai_claw"
)

echo "=== Genesis Memory L0 validation (dry-run pipeline) ==="
echo
echo "[1/3] build_memory collect+synthesize dry-run over rolling 8-day window"
python3 scripts/build_memory.py \
  --days 8 \
  --repo "worldarchitect.ai:${REPOS[0]}" \
  --repo "smartclaw:${REPOS[1]}" \
  --repo "worldai_claw:${REPOS[2]}" \
  --dry-run

echo
echo "[2/3] build_memory write dry-run (pattern extraction, last 7 days)"
python3 scripts/build_memory.py \
  --days 7 \
  --stage write \
  --repo "worldarchitect.ai:${REPOS[0]}" \
  --repo "smartclaw:${REPOS[1]}" \
  --repo "worldai_claw:${REPOS[2]}" \
  --dry-run

echo
echo "[3/3] OpenClaw retrieval smoke check"
if ! command -v openclaw >/dev/null; then
  echo "SKIP: openclaw CLI not installed in PATH."
  exit 0
fi

if openclaw memory search "what did Jeffrey work on in week 38 of 2025?" >/tmp/openclaw-genesis-l0-check.log 2>/tmp/openclaw-genesis-l0-check.err; then
  echo "OK: openclaw memory search executed."
  if [[ -s /tmp/openclaw-genesis-l0-check.log ]]; then
    echo "--- sample output ---"
    head -n 20 /tmp/openclaw-genesis-l0-check.log
  else
    echo "WARN: command returned no output; manual follow-up may be needed."
  fi
else
  echo "WARN: openclaw memory search command failed. Check logs:"
  cat /tmp/openclaw-genesis-l0-check.err
fi

echo
echo "Validation script complete. Use output above to confirm L0 answer quality."
