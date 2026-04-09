#!/bin/bash
# Check orch-7b7 (evidence-capture guidance) and post status to Slack thread
set -euo pipefail

WORKTREE="/Users/jleechan/.worktrees/agent-orchestrator/ao-2313"
THREAD_TS="1775276595.567159"
CHANNEL_ID="C09GRLXF9GR"
TOKEN="${OPENCLAW_SLACK_BOT_TOKEN}"
GH="${GH:-$(command -v gh 2>/dev/null || echo "/usr/local/bin/gh")}"

# Ensure we're in the right directory
cd "$WORKTREE" 2>/dev/null || { echo "Worktree not found"; exit 0; }

# Get branch and sync
BRANCH=$(git branch --show-current 2>/dev/null || echo "unknown")
# Use || true so transient network errors don't kill the launchd job
git fetch origin 2>/dev/null || true
AHEAD=$(git log origin/main..HEAD --oneline 2>/dev/null | wc -l | tr -d ' ')

# Check PR using full gh path
PR_NUM=""

if [ -x "$GH" ]; then
  PR_JSON=$("$GH" pr list --repo jleechanorg/agent-orchestrator --head "$BRANCH" --state open --json number 2>/dev/null || echo "[]")
  if [ -n "$PR_JSON" ] && [ "$PR_JSON" != "[]" ]; then
    PR_NUM=$(echo "$PR_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d[0]['number'] if d else '')" 2>/dev/null || echo "")
  fi
fi

STATUS_MSG=""
if [ -z "$PR_NUM" ]; then
  if [ "$AHEAD" -gt 0 ]; then
    STATUS_MSG="ao-2313: $AHEAD commit(s) pushed but no PR opened yet (branch=$BRANCH)"
  else
    STATUS_MSG="ao-2313: no commits pushed, no PR — checking..."
  fi
else
  # CI check — resolve the actual PR head SHA to avoid querying the default branch
  PR_HEAD_SHA=$("$GH" api repos/jleechanorg/agent-orchestrator/pulls/"$PR_NUM" --jq '.head.sha' 2>/dev/null || echo "")
  CI_RESULT="checking..."
  if [ -n "$PR_HEAD_SHA" ]; then
    CI_JSON=$("$GH" api repos/jleechanorg/agent-orchestrator/commits/"$PR_HEAD_SHA"/check-runs --paginate 2>/dev/null || echo '{"check_runs":[]}')
  else
    CI_JSON='{"check_runs":[]}'
  fi
  SKEPTIC_RESULT=$(echo "$CI_JSON" | python3 -c "
import sys,json
data=json.load(sys.stdin)
runs=data.get('check_runs',[])
sk=[r for r in runs if 'skeptic' in r.get('name','').lower()]
ev=[r for r in runs if 'evidence' in r.get('name','').lower()]
for r in sk+ev:
    print(f'{r.get(\"name\",\"?\"):30s} {r.get(\"conclusion\",\"?\")}')
if not sk+ev: print('No skeptic/evidence checks yet')
" 2>/dev/null || echo "CI check failed")

  # Check failure FIRST (prevents false green when one check passes and another fails)
  if echo "$SKEPTIC_RESULT" | grep -qE "failure|FAIL|cancelled|timed_out"; then
    FAIL_LINE=$(echo "$SKEPTIC_RESULT" | grep -m1 -E "failure|FAIL|cancelled|timed_out" | cut -c1-80)
    STATUS_MSG="ao-2313 / PR #$PR_NUM: 🔴 CI failing — $FAIL_LINE"
  elif echo "$SKEPTIC_RESULT" | grep -qE "success|PASS"; then
    STATUS_MSG="ao-2313 / PR #$PR_NUM: ✅ Evidence guidance PR — skeptic/evidence CI passing"
  else
    STATUS_MSG="ao-2313 / PR #$PR_NUM: 🟡 CI running/pending — $SKEPTIC_RESULT"
  fi
fi

# Always post to Slack when run via launchd
if [ -n "$STATUS_MSG" ] && [ -n "$TOKEN" ]; then
  # Use python json.dumps to safely escape the message content
  SLACK_PAYLOAD=$(python3 -c "
import json,sys
msg = '''$STATUS_MSG'''
print(json.dumps({'channel': '$CHANNEL_ID', 'thread_ts': '$THREAD_TS', 'text': msg}))
" 2>/dev/null || echo '{}')
  curl -s -X POST "https://slack.com/api/chat.postMessage" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d "$SLACK_PAYLOAD" \
    2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print('Slack:', 'ok' if d.get('ok') else d.get('error','fail'))" 2>/dev/null || echo "Slack post failed"
elif [ -n "$STATUS_MSG" ]; then
  echo "WARNING: OPENCLAW_SLACK_BOT_TOKEN not set — skipping Slack notification" >&2
fi

echo "$(date '+%Y-%m-%d %H:%M:%S'): $STATUS_MSG"
