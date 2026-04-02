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

echo "== Verdict regex (Node, first VERDICT line — matches skeptic-gate.mjs) =="
node -e '
function firstVerdictLine(text) {
  for (const line of text.split(/\r?\n/)) {
    const t = line.trim();
    const idx = t.search(/\bVERDICT\s*:/i);
    if (idx !== -1) return t.slice(idx);
  }
  return "";
}
const o = `VERDICT: FAIL - something\nmore`;
const p = `VERDICT: PASS — ok\n`;
const proseFirst = `Let me analyze\nVERDICT: FAIL - x`;
const mdWrap = `**VERDICT: PASS** — ok`;
if (!/^VERDICT[:\s]+FAIL\b/i.test(firstVerdictLine(o))) process.exit(2);
if (!/^VERDICT[:\s]+PASS\b/i.test(firstVerdictLine(p))) process.exit(3);
if (!/^VERDICT[:\s]+FAIL\b/i.test(firstVerdictLine(proseFirst))) process.exit(4);
if (!/^VERDICT[:\s]+PASS\b/i.test(firstVerdictLine(mdWrap))) process.exit(6);
const noVerdict = `intro\nno verdict here`;
if (firstVerdictLine(noVerdict) !== "") process.exit(5);
console.log("OK: first VERDICT line");
'

echo "== Skeptic-cron verdict + SHA jq (matches skeptic-cron gate 7) =="
SAMPLE=$(jq -n --arg b $'## x\n```\nVERDICT: PASS\n```\n<!-- HEAD-SHA: deadbeef1234567890abcdef1234567890abcd -->\n' '{"body":$b}')
PARSED=$(printf '%s\n' "$SAMPLE" | jq -r '
  .body as $b |
  (if ($b | test("<!--[[:space:]]*HEAD-SHA:")) then ($b | capture("<!--[[:space:]]*HEAD-SHA:[[:space:]]*(?<sha>[a-f0-9]+)")).sha else "" end) as $sha |
  (if ($b | test("VERDICT:[[:space:]]*FAIL"; "i")) then "FAIL"
   elif ($b | test("VERDICT:[[:space:]]*PASS"; "i")) then "PASS"
   elif ($b | test("VERDICT:[[:space:]]*SKIPPED"; "i")) then "SKIPPED"
   elif ($b | test("VERDICT:[[:space:]]*ERROR"; "i")) then "ERROR"
   else "UNPARSED"
   end) as $tok |
  [$tok, $sha] | @tsv
')
TOK=$(printf '%s\n' "$PARSED" | cut -f1)
SHA=$(printf '%s\n' "$PARSED" | cut -f2)
if [[ "$TOK" != "PASS" || "$SHA" != "deadbeef1234567890abcdef1234567890abcd" ]]; then
  echo "FAIL: expected PASS + SHA, got TOK=$TOK SHA=$SHA"
  exit 5
fi
echo "OK: cron verdict parse (PASS + HEAD-SHA)"

if command -v actionlint >/dev/null 2>&1; then
  echo "== actionlint (non-fatal) =="
  actionlint .github/workflows/skeptic-gate.yml .github/workflows/skeptic-cron.yml 2>&1 || true
else
  echo "(skip actionlint — not installed)"
fi

echo "All local skeptic-gate checks passed."
