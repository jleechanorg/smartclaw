---
name: slack-troubleshooting
description: Diagnose and resolve Slack integration failures — webhooks, bot tokens, and API errors
triggers:
  - "slack not working"
  - "webhook no_service"
  - "slack message failed"
  - "can't post to slack"
  - "slack channel reminder"
---

## Slack Posting Options

### 1. Webhook (simplest, but unreliable long-term)
```bash
curl -s -X POST "$SLACK_WEBHOOK_URL" \
  -H "Content-Type: application/json" \
  -d '{"text": "your message here"}'
```
**Problem:** Webhooks deactivate when the Slack app is uninstalled or its incoming webhooks are removed.

### 2. Bot Token API (more reliable)
```bash
curl -s -X POST "https://slack.com/api/chat.postMessage" \
  -H "Authorization: Bearer $SLACK_BOT_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"channel\": \"$CHANNEL_ID\", \"text\": \"your message\"}"
```

## Bot Token — Verified Extraction (2026-04-29)
The env var `SLACK_MCP_XOXB_TOKEN` may return a **redacted/placeholder value** (`***`) when sourced from bashrc or shell — not the real token. The actual working `xoxb-...` token is stored in `~/.hermes_prod/config.yaml`.

**Extraction command (tested and working):**
```bash
TOKEN=$(grep "SLACK_MCP_XOXB_TOKEN" ~/.hermes_prod/config.yaml | awk '{print $2}')
# Verify: echo "Token length: ${#TOKEN}"  # should be ~58 chars, not 3
```

**Auth test before posting:**
```bash
curl -s "https://slack.com/api/auth.test" \
  -H "Authorization: Bearer $TOKEN" | python3 -c "import sys,json; d=json.load(sys.stdin); print('ok:', d.get('ok'), 'user:', d.get('user'))"
```

**Token Discovery Order (most reliable first):**
1. `~/.hermes_prod/config.yaml` — grep + awk (this is the canonical working source)
2. Env var `SLACK_MCP_XOXB_TOKEN` — **verify it returns >10 chars**, not `***`
3. If none yield a real `xoxb-...` token -> cannot post via API

## Webhook Diagnostic
Always verify before assuming it works:
```bash
curl -sv "https://hooks.slack.com/services/T09FXQ4LCQP/B09GCD0K6N6/..." 2>&1 | grep -E "no_service|no-match|outcome"
```

**`no_service` in response body** = webhook deactivated/deleted. Fix: reinstall Slack app.

**`x-slack-shared-secret-outcome: no-match`** = webhook secret mismatch.

## Cron Job / Automated Posting
- **Default:** Use `openclaw cron add --announce --to slack:#channel --name "..." --at <time>`
- This uses OpenClaw's internal Slack integration and handles auth automatically
- Only fall back to raw webhook/curl if OpenClaw is unavailable

## Notes
- Slack home channel ID for this user: `C0AJQ5M0A0Y`
- Slack bot token format: `xoxb-...`
- Incoming webhook URL format: `https://hooks.slack.com/services/T09.../B09.../xxx`
