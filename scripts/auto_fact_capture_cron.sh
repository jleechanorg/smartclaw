#!/bin/bash
# Run auto_fact_capture.py on new sessions since last run
# State tracked in ~/.openclaw/memory/auto_fact_capture_state.json
# Called by launchd (ai.openclaw.auto-fact-capture) every 15 minutes

STATE_FILE="$HOME/.openclaw/memory/auto_fact_capture_state.json"
SESSION_DIR="$HOME/.openclaw/agents/main/sessions"
SCRIPT="$HOME/.openclaw/scripts/auto_fact_capture.py"

mkdir -p "$(dirname "$STATE_FILE")"

# Find session files modified since state file (or last hour if no state)
if [ -f "$STATE_FILE" ]; then
    NEWER_THAN="$STATE_FILE"
else
    # No state yet — touch a tmp file 1 hour old as reference
    NEWER_THAN=$(mktemp)
    touch -t "$(date -v-1H '+%Y%m%d%H%M.%S' 2>/dev/null || date -d '1 hour ago' '+%Y%m%d%H%M.%S')" "$NEWER_THAN"
fi

# Find session files modified since last run (cap at 5 per run to stay within 60s launchd budget)
MAX_PER_RUN=5
NEW_SESSIONS=$(find "$SESSION_DIR" -name "*.jsonl" -not -name "*.lock" -newer "$NEWER_THAN" 2>/dev/null | head -"$MAX_PER_RUN")

COUNT=0
for session in $NEW_SESSIONS; do
    size=$(wc -c < "$session" 2>/dev/null || echo 0)
    if [ "$size" -gt 1000 ]; then
        python3 "$SCRIPT" --session-file "$session" && COUNT=$((COUNT+1))
    fi
done

# Clean up tmp reference file if we created one
[ "$NEWER_THAN" != "$STATE_FILE" ] && rm -f "$NEWER_THAN"

# Update state (pure bash — avoids multi-line python -c quoting issues)
NOW=$(date +%s)
echo "{\"last_run\":$NOW,\"last_count\":$COUNT}" > "$STATE_FILE"

[ "$COUNT" -gt 0 ] && echo "auto_fact_capture_cron: processed $COUNT sessions"
exit 0
