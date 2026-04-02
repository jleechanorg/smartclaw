#!/usr/bin/env bash
# Local validation for skeptic-gate / skeptic-cron jq (no network).
# Run from repo root: bash scripts/validate-skeptic-gate.sh

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "== CI external_runs filter (must exclude Skeptic + Staging Canary) =="
# Fixture: one page of check-runs API shape
FIXTURE='{"check_runs":[
  {"name":"Skeptic Gate","conclusion":null,"status":"in_progress","app":{"slug":"github-actions"}},
  {"name":"Staging Canary Gate","conclusion":"success","status":"completed","app":{"slug":"github-actions"}},
  {"name":"pytest","conclusion":"success","status":"completed","app":{"slug":"github-actions"}}
]}'

RESULT=$(echo "$FIXTURE" | jq -rs --arg now "$(date +%s)" '
  def stale_threshold: 900;
  def skip_stale: (.app.slug == "cursor" and .status == "in_progress" and (.started_at != null) and (($now | tonumber) - (.started_at | fromdateiso8601) > stale_threshold));
  (map(.check_runs // []) | add) as $runs |
  ($runs | map(select(.name != "Skeptic Gate" and .name != "Staging Canary Gate"))) as $external_runs |
  if ($external_runs | length) == 0 then "neutral"
  elif ($external_runs | map(select(.conclusion == null and .status == "in_progress" and (skip_stale | not))) | length) > 0 then "in_progress"
  elif ($external_runs | map(select(.conclusion != "success" and .conclusion != "skipped" and .conclusion != "neutral" and .conclusion != "cancelled")) | length) > 0 then "failure"
  else "success"
  end
')

if [[ "$RESULT" != "success" ]]; then
  echo "FAIL: expected CI_STATUS success when only external checks pass, got: $RESULT"
  exit 1
fi
echo "OK: CI_STATUS=$RESULT (Skeptic in_progress ignored for gate 1)"

echo "== Verdict regex (Node, matches skeptic-gate.mjs patterns) =="
node -e '
const o = `VERDICT: FAIL - something\nmore`;
const p = `VERDICT: PASS — ok\n`;
if (!/^VERDICT[:\s]+FAIL\b/im.test(o)) process.exit(2);
if (!/^VERDICT[:\s]+PASS\b/im.test(p)) process.exit(3);
console.log("OK: FAIL and PASS patterns");
'

if command -v actionlint >/dev/null 2>&1; then
  echo "== actionlint (non-fatal) =="
  actionlint .github/workflows/skeptic-gate.yml .github/workflows/skeptic-cron.yml 2>&1 || true
else
  echo "(skip actionlint — not installed)"
fi

echo "All local skeptic-gate checks passed."
