---
name: executive-assistant
description: "Run a comprehensive morning executive assistant sweep for {{OWNER_NAME}}: check today's calendar, scan Gmail for flagged/important emails, review Slack action items, and post a concise briefing to {{OWNER_NAME}}'s DM. Use when a cron or direct request triggers the executive assistant sweep."
---

# Executive Assistant Sweep

Produce a concise morning briefing for {{OWNER_NAME}} covering schedule, email, and Slack, then post it to their DM channel.

## Goal

Give {{OWNER_NAME}} one message that covers everything they need to be aware of right now — without fluff. Actionable items get explicit prompts ("Want me to draft a reply?").

## Workflow

### 1. Calendar — what's happening today

```bash
source ~/.profile && export GOG_KEYRING_PASSWORD="hermes-gog-2026" && gog calendar events --all -a {{ASSISTANT_EMAIL}} --days=1 --max=100 --json --results-only
```

**Critical:** `gog` requires two environment fixes before it will run non-interactively in cron contexts:
1. `source ~/.profile` — loads the gog binary and OAuth session
2. `export GOG_KEYRING_PASSWORD="hermes-gog-2026"` — unlocks the macOS keychain credential store so gog can authenticate without interactive prompt

Both are required together. Without them, gog hangs waiting for keychain access or OAuth confirmation.

- Pull events from all calendars: `{{PERSONAL_EMAIL}}`, `{{PRIMARY_WORK_EMAIL}}`, `{{SECONDARY_CALENDAR_EMAIL_1}}`, `{{SECONDARY_CALENDAR_EMAIL_2}}`, `{{SECONDARY_CALENDAR_EMAIL_3}}`
- Include family/household events as context (not as action items)
- Group into sections: **Now / Today** (imminent), **Tonight**, **Upcoming** (next 2 days if unusual)
- Format: `HH:MM — event name` in local time (America/Los_Angeles)

### 2. Gmail — flagged and important messages

Use the `himalaya` skill or shell email tools to check for:
- Starred / flagged messages
- Messages marked IMPORTANT by Gmail
- Any unread messages in the primary inbox from the last 24h that look high-priority (recruiters, legal, finance, urgent subject lines)

For each flagged email, include: sender, subject, one-line summary, and offer to draft a reply or pull full content.

**Gmail search via gog (non-interactive):**
```bash
source ~/.profile && export GOG_KEYRING_PASSWORD="hermes-gog-2026" && gog gmail search 'is:unread newer_than:24h (is:important OR is:starred)' --max 10 --json --no-input
```
**Always use `--no-input`** — without it, gog gmail search prompts for confirmation even with `--json` flag.

### 3. Slack — action items needing {{OWNER_NAME}}

Check the channels in `openclaw.json` (or the default monitored list). Look for:
- Open threads where {{OWNER_NAME}} asked a question and the bot hasn't answered yet
- Mentions of {{OWNER_NAME}} with no reply
- Anything marked urgent or pinned since the last sweep

Do **not** list every message — only items needing action.

### 4. Deploys / system status

Check `#deploys` or equivalent channel for:
- Failed deploys or errors from the past 12h
- Successful deploys worth noting

### 5. Life / personal reminders

Check `#life` or equivalent personal channel for:
- Reminders posted since last sweep
- Follow-ups that were posted but not actioned

### 6. Find DM channel

Do NOT rely on `$JLEECHAN_DM_CHANNEL` — it is often empty in cron job environments. Instead, look up the DM channel dynamically:

Use the `mcp__slack__users_search` tool (from any session with Slack MCP available) to find the user's `DMChannelID`. Alternatively, the known stable values for jleechan are:
- User ID: `U09GH5BR3QU`
- DM channel: `${SLACK_CHANNEL_ID}`

### 7. Compose and post briefing

Post to {{OWNER_NAME}}'s DM channel using Python `urllib.request` with the bot token from `~/.hermes_prod/.env`. This is the only reliable approach in cron contexts — **not** `curl`, **not** `send_message` (MCP routing does not expose `sendMessage` in cron runtime), **not** the webhook.

**Confirmed working approach (2026-04-24):**
```python
import json, urllib.request
from pathlib import Path

# Read bot token from .env (NOT from env vars — they may be masked in cron)
env_path = Path.home() / ".hermes_prod" / ".env"
with open(env_path) as f:
    env_content = f.read()
bot_token = [line for line in env_content.split("\n") if line.startswith("SLACK_BOT_TOKEN=")][0].split("=", 1)[1].strip()

# DM channel for jleechan: ${SLACK_CHANNEL_ID}
channel_id = "${SLACK_CHANNEL_ID}"

blocks = [
    {"type": "header", "text": {"type": "plain_text", "text": "📋 Executive Briefing — ...", "emoji": True}},
    {"type": "divider"},
    # ... section blocks ...
]

payload = json.dumps({"channel": channel_id, "text": "Executive Briefing", "blocks": blocks}).encode()
req = urllib.request.Request(
    "https://slack.com/api/chat.postMessage",
    data=payload,
    headers={"Authorization": f"Bearer {bot_token}", "Content-Type": "application/json"},
    method="POST"
)
with urllib.request.urlopen(req) as resp:
    result = json.loads(resp.read())
# result["ok"] == True means success; ts is the posted message timestamp
```

**Why this works when curl fails:** The bot token read directly from `~/.hermes_prod/.env` is valid and unmasked. Shell `curl` may still return `no_service` for the webhook URL even when the bot token is healthy — the two are independent.

**Known stable values for jleechan:**
- User ID: `U09GH5BR3QU`
- DM channel: `${SLACK_CHANNEL_ID}`

### 8. Slack posting fallback

If the Python + bot token approach also fails (should be rare):
1. Try reading `SLACK_BOT_TOKEN` from `~/.hermes_prod/.env` using `grep` in shell instead of Python
2. If token is masked at the file level (extremely rare), write to `memory/briefing-YYYY-MM-DD.md` and notify in the thread that Slack delivery failed

**Webhook diagnostics (lower priority):**
- `curl` returning `no_service` means the webhook app is deactivated — this is independent of the bot token and can be ignored if the bot token approach is working
- Do NOT spend time troubleshooting the webhook in cron contexts — the bot token + REST API is the primary path

### 9. gog OAuth re-auth flow

If gog returns `invalid_grant: Token has been expired or revoked`, you can attempt re-auth:

```bash
source ~/.profile && export GOG_KEYRING_PASSWORD="hermes-gog-2026" && gog auth add jleechan@gmail.com --services gmail,calendar --remote --no-input
```

This produces a URL that must be opened in a browser to complete OAuth. The re-auth **cannot complete automatically in a cron context** — it requires a human to visit the URL and authorize. After authorization, run step 2:

```bash
source ~/.profile && export GOG_KEYRING_PASSWORD="hermes-gog-2026" && gog auth add jleechan@gmail.com --services gmail,calendar --remote --step 2 --auth-url "<url from step 1>"
```

**Until re-auth completes**, calendar and Gmail will remain unavailable. Note the failure in the briefing but do not retry gog within the same run.

## Safety rules

- Never post the briefing twice for the same sweep run (check if a briefing was already posted in the last 30 minutes before sending)
- If calendar access fails, still post what's available and note the failure
- If Gmail access fails, skip that section silently unless it was explicitly requested
- Stay silent on errors that don't affect the briefing content
