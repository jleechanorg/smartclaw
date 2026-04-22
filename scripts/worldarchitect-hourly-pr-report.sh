#!/usr/bin/env bash
# Hourly worldarchitect.ai PR status reporter — posts one-line summary to #worldai Slack thread.
# Run every hour via launchd.
set -euo pipefail

# Config
GH_TOKEN="${GH_TOKEN:-$(gh auth token 2>/dev/null)}"
export GH_TOKEN
REPO="jleechanorg/worldarchitect.ai"
CHANNEL_ID="C0AJ3SD5C79"
THREAD_TS="1775460296.300209"
SLACK_BOT_TOKEN="${SLACK_BOT_TOKEN:-${SLACK_BOT_TOKEN:-}}"

log() { echo "[$(date '+%Y-%m-%dT%H:%M:%S')] $*"; }

# Fetch open PRs (last 7 days via search API)
week_ago="$(TZ=UTC date -u -v-7d '+%Y-%m-%d' 2>/dev/null)" || week_ago="$(date -u -d '7 days ago' '+%Y-%m-%d' 2>/dev/null)"
raw="$(gh api "search/issues?q=repo:$REPO+is:pr+is:open+updated:>=$week_ago&per_page=100&sort=updated&order=desc" 2>/dev/null)" || raw=""
total_count="$(jq -r '.total_count // 0' <<<"$raw" 2>/dev/null)" || total_count="0"

if [[ -z "$raw" || "$total_count" == "0" ]]; then
  # Fallback to open PRs only
  raw="$(gh api "repos/$REPO/pulls?state=open&per_page=100" 2>/dev/null)" || raw=""
  total_count="$(jq 'if type == "array" then length else 0 end' <<<"$raw" 2>/dev/null)" || total_count="0"
fi

# Count green statuses (CI + CR APPROVED as proxy)
green=0 needs_attention=0
while IFS= read -r pr_json; do
  [[ -z "$pr_json" || "$pr_json" == "null" ]] && continue
  number="$(jq -r '.number // empty' <<<"$pr_json" 2>/dev/null)" || continue
  [[ -z "$number" ]] && continue
  state="$(jq -r '.state // empty' <<<"$pr_json" 2>/dev/null)" || continue
  draft="$(jq -r '.draft // false' <<<"$pr_json" 2>/dev/null)" || continue
  [[ "$state" == "closed" || "$state" == "merged" || "$draft" == "true" ]] && continue

  sha="$(jq -r '.head.sha // empty' <<<"$pr_json" 2>/dev/null)" || sha=""
  if [[ -z "$sha" && -n "$number" ]]; then
    _pull="$(gh api "repos/$REPO/pulls/$number" 2>/dev/null)" || _pull=""
    sha="$(jq -r '.head.sha // empty' <<<"$_pull" 2>/dev/null)" || sha=""
  fi
  ci_state="error"
  if [[ -n "$sha" ]]; then
    ci_state="$(gh api "repos/$REPO/commits/$sha/status" --jq '.state' 2>/dev/null)" || ci_state="error"
  fi
  cr_review="$(gh api "repos/$REPO/pulls/$number/reviews" \
    --jq '[.[] | select(.user.login == "coderabbitai[bot]")] | sort_by(.submitted_at) | reverse | .[0].state // "none"' 2>/dev/null)" || cr_review="none"

  if [[ "$ci_state" == "success" && "$cr_review" == "APPROVED" ]]; then
    ((green++)) || true
  else
    ((needs_attention++)) || true
  fi
done < <(
  if jq -e '.items' <<<"$raw" >/dev/null 2>&1; then
    jq -c '.items[]' <<<"$raw"
  else
    jq -c '.[]' <<<"$raw"
  fi
)

# Status emoji for Slack (mirrors green / attention / idle)
if (( green > 0 )) && (( needs_attention == 0 )); then
  status_icon=":large_green_circle:"
elif (( needs_attention > 0 )); then
  status_icon=":large_yellow_circle:"
else
  status_icon=":large_blue_circle:"
fi

# Build summary (single assignment path; icon always included)
if (( needs_attention == 0 )) && (( green == 0 )); then
  message="${status_icon} • No open PRs in \`$REPO\` (of ${total_count} tracked)"
else
  message="${status_icon} • \`$REPO\` — 🟢 $green ready, 🔴 $needs_attention needs attention (of $total_count total)"
fi

# Post to Slack thread
if [[ -n "$SLACK_BOT_TOKEN" && "$SLACK_BOT_TOKEN" != "null" ]]; then
  # Build JSON payload safely using jq with proper JSON encoding
  SLACK_JSON="$(jq -n \
    --arg ch "$CHANNEL_ID" \
    --arg msg "$message" \
    --arg ts "$THREAD_TS" \
    '{channel: $ch, text: $msg, thread_ts: $ts}')"
  curl -sS "https://slack.com/api/chat.postMessage" \
    -H "Authorization: Bearer $SLACK_BOT_TOKEN" \
    -H "Content-Type: application/json" \
    -d "$SLACK_JSON" \
    > /dev/null 2>&1 || true
fi

log "Posted to Slack thread $THREAD_TS: $message"
echo "$message"
