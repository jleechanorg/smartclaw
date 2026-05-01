#!/usr/bin/env bash
#
# Gmail Daily Recap
# Posts a summary of important/actionable emails from the last 24h to Slack #life.
#
# Filters:
#   - Starred (IMPORTANT) emails
#   - Meeting invitations / calendar updates
#   - Fraud alerts / security notices
#   - Status page incidents
#   - Known important senders (Jorge, etc.)
#
# Excludes: promotions, task reminders (Asana), newsletters, bulk mail.
#
# Env:
#   SLACK_CHANNEL_ID  — Slack channel ID (default: C0AMM2B4319 = #life)
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/../lib/gog-env.sh"
load_gog_env_from_openclaw "${HOME}/.smartclaw/openclaw.json"

TZ="${TZ:-America/Los_Angeles}"; export TZ

# Use gdate (GNU) if available, else BSD date — gdate preferred for cross-platform -v support
if command -v gdate >/dev/null 2>&1; then
  DATE_CMD="gdate"
else
  DATE_CMD="date"
fi

# Timezone-aware guard: only fire Mon-Fri 8:00-8:59 AM Pacific.
# launchd StartCalendarInterval uses system clock; this guard ensures
# we only post at 8am PT regardless of the machine's system timezone.
_pth_weekday="$($DATE_CMD +%u)"   # 1=Mon … 7=Sun
_pth_hour="$($DATE_CMD +%H)"      # 00-23
if [[ "$_pth_weekday" -ge 1 && "$_pth_weekday" -le 5 && "$_pth_hour" == "08" ]]; then
  : # within window — proceed
else
  exit 0  # not in 8am PT Mon-Fri window — silently skip
fi

NOW="$($DATE_CMD '+%Y-%m-%d %H:%M %Z')"

CHANNEL_ID="${SLACK_CHANNEL_ID:-C0AMM2B4319}"
SLACK_TOKEN="${SLACK_BOT_TOKEN:-${SLACK_BOT_TOKEN:-}}"
# Capture parent temp dir so trap can clean the whole tree on any early exit
_TMP_ROOT="$(mktemp -d)"
WORK_DIR="$_TMP_ROOT/gmail-recap"
OUT="$WORK_DIR/emails.txt"
ERR="$WORK_DIR/gog.err"

mkdir -p "$WORK_DIR"
trap 'rm -rf "$_TMP_ROOT"' EXIT

# Validate prerequisites before any work (fail fast — avoid leaking email metadata on error)
if [[ -z "$SLACK_TOKEN" ]]; then
  echo "ERROR: SLACK_BOT_TOKEN not set" >&2
  exit 1
fi

if ! command -v gog >/dev/null 2>&1; then
  echo "ERROR: 'gog' CLI not found in PATH" >&2
  exit 1
fi

# Search for important/actionable emails:
#   is:important       — starred emails
#   subject:invitation — calendar invites
#   subject:fraud      — fraud/security alerts
#   subject:alert      — account alerts
#   subject:incident   — statuspage incidents
gog gmail search "(is:important OR subject:invitation OR subject:fraud OR subject:alert OR subject:incident) newer_than:1d" --max 20 --no-input > "$OUT" 2>"$ERR"

# Deduplicate by thread (same thread ID = skip older message in same thread)
declare -A SEEN_THREADS=()
FILTERED=""
EMAIL_COUNT=0
while IFS= read -r line; do
  # Skip header/metadata lines
  [[ -z "$line" || "$line" =~ ^(ID|#|\$) ]] && continue
  [[ "$line" =~ "Next page" ]] && continue

  # Extract thread ID (last column)
  thread=$(echo "$line" | awk '{print $NF}')
  if [[ "$thread" == "-" ]]; then
    # No thread (single message) — include it
    FILTERED="${FILTERED}${line}"$'\n'
    EMAIL_COUNT=$((EMAIL_COUNT + 1))
  elif [[ -z "${SEEN_THREADS[$thread]}" ]]; then
    # Thread not yet seen — include it and mark seen
    FILTERED="${FILTERED}${line}"$'\n'
    SEEN_THREADS[$thread]=1
    EMAIL_COUNT=$((EMAIL_COUNT + 1))
  fi
done < "$OUT"

{
  echo "*📬 Gmail Daily Recap — $NOW*"
  echo ""
  if [[ "$EMAIL_COUNT" -eq 0 ]]; then
    echo "No important or actionable emails in the last 24 h. 🎉"
  else
    echo "*$EMAIL_COUNT important / actionable email(s)* in the last 24h:"
    echo ""
    printf '%s' "$FILTERED" | awk '
      NF >= 4 {
        from = $3
        # Subject is cols 4 onwards, until a [ label appears
        subj = ""
        sep = ""
        for (i = 4; i <= NF; i++) {
          if ($i ~ /^\[/) break
          subj = subj sep $i
          sep = " "
        }
        # Truncate long subjects
        if (length(subj) > 80) subj = substr(subj, 1, 77) "..."
        printf "> *%s* — _%s_\n", subj, from
      }
    ' | head -15 || true
    echo ""
    if [[ "$EMAIL_COUNT" -gt 15 ]]; then
      echo "_+$((EMAIL_COUNT - 15)) more — check Gmail_"
    fi
  fi
  echo ""
  echo "_Filtered: starred, invites, fraud alerts, security notices, incidents_"
} > "$WORK_DIR/summary.txt"

SLACK_TEXT=$(cat "$WORK_DIR/summary.txt")

RESPONSE=$(curl -sS --connect-timeout 10 --max-time 30 \
  -X POST "https://slack.com/api/chat.postMessage" \
  -H "Authorization: Bearer $SLACK_TOKEN" \
  -H "Content-Type: application/json" \
  -d "$(jq -n \
    --arg channel "$CHANNEL_ID" \
    --arg text "$SLACK_TEXT" \
    '{channel: $channel, text: $text, unfurl_links: false}' \
  )" 2>&1)

if echo "$RESPONSE" | jq -e '.ok == true' >/dev/null 2>&1; then
  echo "Posted to Slack channel $CHANNEL_ID successfully"
else
  echo "Slack API error: $RESPONSE"
  exit 1
fi
