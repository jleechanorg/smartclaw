---
description: /claw - Send a task to OpenClaw agent via Slack orchestrator loop (P0 fix)
type: orchestration
execution_mode: immediate
---
# /claw - OpenClaw Agent Dispatch via Slack

**Usage**: `/claw <task description>`

**Purpose**: Send a task to the OpenClaw agent via Slack. This is the **recommended** path since the native `openclaw agent` CLI hangs due to gateway WebSocket timeouts when the orchestrator loop is active.

## Why Slack?

The native `openclaw agent` CLI path has known issues:
1. Gateway WS times out while orchestrator loop is active
2. Embedded fallback fails on session file lock held by gateway (e.g., `8a87e127.jsonl.lock`)

Posting to Slack (`#ai-slack-test`) goes through the **orchestrator loop**, which is the working path.

## Execution Instructions

When this command is invoked with `$ARGUMENTS`:

### Step 1: Health Check - Detect Gateway Session Lock

Before dispatching, check if the gateway holds a session lock that would cause the CLI path to fail:

```bash
# Check for stale session lock files (gateway holds lock during active orchestrator loops)
LOCK_FILES=$(find ~/.openclaw/agents/main/sessions -name "*.jsonl.lock" -mmin -5 2>/dev/null | wc -l)
if [ "$LOCK_FILES" -gt 0 ]; then
  echo "⚠️ Gateway session lock detected ($LOCK_FILES lock files < 5 min old)"
  echo "   Falling back to Slack dispatch path..."
  USE_SLACK_FALLBACK=true
else
  echo "✓ No active gateway session locks detected"
  USE_SLACK_FALLBACK=false
fi
```

### Step 2: Dispatch via Slack (Primary Path)

Post the task to `#ai-slack-test` so OpenClaw picks it up through the orchestrator loop:

```bash
TASK_DESCRIPTION="$ARGUMENTS"
SLACK_CHANNEL="C0AKALZ4CKW"  # #ai-slack-test

# Format message with clear task delineation for the orchestrator
MESSAGE="[claw dispatch] $TASK_DESCRIPTION"

# Post to Slack using bot token (orchestrator loop picks up from #ai-slack-test)
RESPONSE=$(curl -s -X POST https://slack.com/api/chat.postMessage \
  -H "Authorization: Bearer $OPENCLAW_SLACK_BOT_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"channel\":\"$SLACK_CHANNEL\",\"text\":\"$MESSAGE\"}")

if echo "$RESPONSE" | grep -q '"ok":true'; then
  echo "✓ Dispatched to OpenClaw via Slack orchestrator loop"
  echo "  Task: $TASK_DESCRIPTION"
  echo "  Channel: #ai-slack-test"
  echo ""
  echo "  The orchestrator loop will pick up this message and spawn an agent."
else
  echo "✗ Slack dispatch failed:"
  echo "$RESPONSE" | jq -r '.error // .'
  exit 1
fi
```

### Step 3: Confirm to User

- Task dispatched via Slack orchestrator loop (not broken `openclaw agent` CLI)
- OpenClaw picks up the message from `#ai-slack-test` and spawns an agent
- Response will appear in the Slack thread

## Alternative: Direct CLI (Only When No Locks)

If you must use the CLI path (no gateway locks detected), you can optionally try it:

```bash
# Only use this path as a last resort - the CLI hangs with active orchestrator
if [ "$USE_SLACK_FALLBACK" = "false" ]; then
  echo ""
  echo "Alternatively, you could try (may hang):"
  echo "  openclaw agent --agent main -m \"$ARGUMENTS\""
fi
```

## Requirements

- OpenClaw gateway running and monitoring `#ai-slack-test`
- `OPENCLAW_SLACK_BOT_TOKEN` environment variable set
- Gateway has `requireMention: false` for `#ai-slack-test` (so it picks up all messages)

## Checking Progress

- Check Slack `#ai-slack-test` for agent responses
- Gateway logs: `tail -f /tmp/openclaw/openclaw-$(date +%F).log`

## Environment Variables Used

| Variable | Source | Purpose |
|----------|--------|---------|
| `OPENCLAW_SLACK_BOT_TOKEN` | Gateway config | Bot token for posting to Slack |
| `SLACK_CHANNEL` | Hardcoded | `#ai-slack-test` (C0AKALZ4CKW) |

## Notes

- **This is the P0 fix** for the broken `openclaw agent` CLI path
- The Slack orchestrator loop is the reliable production path
- Health check prevents wasted dispatch attempts when gateway holds session locks
- Beads: orch-1mdn (epic), orch-cagr, orch-vf9y, orch-sb8y, orch-vcaf
