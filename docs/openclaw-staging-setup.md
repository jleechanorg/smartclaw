# OpenClaw Staging Setup Guide

For machines that already have OpenClaw running (main gateway on port 18789).

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Onboard Staging Profile](#2-onboard-staging-profile)
   - [Pre-flight check](#pre-flight-check-required-before-onboarding)
   - [Onboard](#onboard)
   - [Post-check](#post-check-required-before-proceeding-to-step-3)
3. [Create Separate Slack App](#3-create-separate-slack-app)
4. [Patch openclaw.json](#4-patch-openclawingjson)
5. [Configure Loop Prevention](#5-configure-loop-prevention)
6. [Run staging gateway on demand](#6-run-staging-gateway-on-demand)
7. [Verify both gateways](#7-verify-both-gateways)
8. [Git track ~/.openclaw-staging/](#8-git-track-openclaw-staging)
9. [Launchd backup job](#9-launchd-backup-job)

---

## 1. Prerequisites

- OpenClaw already installed and running (main gateway on port 18789)
- Node.js 22.12+

---

## 2. Onboard Staging Profile

Port must be ≥20 away from main (each gateway spawns derived ports up to base+108).
Main is on 18789 → staging on 18810.

### Pre-flight check (required before onboarding)

If the onboard command fails, openclaw falls back to the default profile directory
(`~/.openclaw/`), causing all subsequent steps to silently patch the **main** instance
instead of staging. Run these checks first:

```bash
# 1. Confirm main gateway is healthy (you need it running before staging)
curl -sf http://127.0.0.1:18789/health || { echo "ERROR: main gateway not running — start it first"; exit 1; }

# 2. Confirm staging port is free
lsof -i :18810 | grep LISTEN && { echo "ERROR: port 18810 already in use — stop whatever is using it"; exit 1; } || echo "port 18810 is free"

# 3. Confirm staging home doesn't already exist (re-running onboard over an existing dir can corrupt config)
[ -d ~/.openclaw-staging ] && { echo "ERROR: ~/.openclaw-staging already exists — delete it first if you want a fresh onboard"; exit 1; } || echo "staging home is clear"
```

All three must pass before continuing.

### Onboard

```bash
openclaw --profile staging onboard \
  --accept-risk \
  --auth-choice openai-codex \
  --gateway-port 18810 \
  --gateway-bind loopback \
  --gateway-auth token \
  --gateway-token "$(openssl rand -hex 32)" \
  --workspace ~/.openclaw-staging/workspace \
  --flow quickstart
```

### Version check (before onboarding)

Confirm the installed openclaw version matches what you expect. A version mismatch between
the CLI and an existing `openclaw.json` causes a silent startup failure — the gateway
process starts but never opens its port:

```bash
openclaw --version   # should match the version that wrote ~/.openclaw-staging/openclaw.json
# If out of date:
npm install -g openclaw@latest
openclaw --version   # confirm updated
```

Also regenerate the launchd plist after any openclaw update — the old plist may point to a
stale dist path:

```bash
openclaw --profile staging gateway install --force
```

### Post-check (required before proceeding to Step 3)

Verify the command wrote to the staging directory — **not** the main one:

```bash
[ -f ~/.openclaw-staging/openclaw.json ] \
  || { echo "ERROR: onboard did not create ~/.openclaw-staging/openclaw.json — do NOT continue"; exit 1; }

# Confirm staging config targets port 18810 (catches silent fallback to main)
python3 -c "
import json, os
path = os.path.expanduser('~/.openclaw-staging/openclaw.json')
d = json.load(open(path))
port = d.get('gateway', {}).get('port')
assert port == 18810, f'WRONG PORT {port} — onboard fell back to main profile. Delete ~/.openclaw-staging and retry.'
print(f'OK: staging gateway port = {port}')
"
```

If the post-check fails, delete `~/.openclaw-staging/` and go back to the pre-flight checks.

This creates `~/.openclaw-staging/` with its own config and workspace.

---

## 3. Create Separate Slack App

**Critical: each gateway MUST have its own Slack app.** If two gateways share one bot token, Slack delivers events to both WebSocket connections causing race conditions, dropped messages, and WebSocket 500 errors.

Go to `https://api.slack.com/apps` → **Create New App** → **From an app manifest** → select your workspace → paste:

```json
{
  "display_information": {
    "name": "openclaw_staging",
    "description": "OpenClaw staging instance",
    "background_color": "#2c2d30"
  },
  "features": {
    "bot_user": {
      "display_name": "openclaw_staging",
      "always_online": true
    }
  },
  "oauth_config": {
    "scopes": {
      "bot": [
        "app_mentions:read",
        "channels:history",
        "channels:read",
        "chat:write",
        "files:read",
        "files:write",
        "groups:history",
        "groups:read",
        "im:history",
        "im:read",
        "im:write",
        "reactions:read",
        "reactions:write",
        "team:read",
        "users:read",
        "users:read.email"
      ]
    }
  },
  "settings": {
    "event_subscriptions": {
      "bot_events": [
        "app_mention",
        "message.channels",
        "message.groups",
        "message.im"
      ]
    },
    "interactivity": { "is_enabled": false },
    "org_deploy_enabled": false,
    "socket_mode_enabled": true,
    "token_rotation_enabled": false
  }
}
```

After creating:
1. **Install to Workspace** (left sidebar → Install App → Install to Workspace → Allow)
2. Copy **Bot User OAuth Token** (`xoxb-...`) from OAuth & Permissions
3. **Basic Information** → **App-Level Tokens** → Generate with `connections:write` scope (name: `staging`) → copy `xapp-...` token
4. **Invite the bot** to channels: in Slack, type `/invite @openclaw_staging` in each channel it should monitor

**Prefer [§4 Patch `openclaw.json`](#4-patch-openclawingjson):** put staging bot/app tokens in `~/.openclaw-staging/openclaw.json` under `channels.slack` (staging uses the `staging` profile and that directory; production usually runs under launchd from `~/.openclaw/`).

If you also export tokens for **your** shell scripts, remember OpenClaw’s documented env fallbacks are still `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN` ([configuration reference](https://docs.openclaw.ai/gateway/configuration-reference), [Slack setup](https://www.getopenclaw.ai/en/docs/configuration)) — not `OPENCLAW_STAGING_SLACK_*`. A single shell cannot set two different `SLACK_BOT_TOKEN` values at once, so do not rely on env for two gateways; use per-profile `openclaw.json` for staging vs production.

Optional — only if a script you own expects custom names (OpenClaw does not read these):

```bash
# Example names for your automation only; gateway uses openclaw.json + SLACK_* fallbacks above
export STAGING_SLACK_BOT_TOKEN="xoxb-..."
export STAGING_SLACK_APP_TOKEN="xapp-..."
```

Also add to `~/.profile` if those scripts source it.

---

## 4. Patch openclaw.json

Same two fixes as main — but targeting `~/.openclaw-staging/openclaw.json`.

**Also set the staging Slack tokens** (from Step 3):

```python
import json, os

path = os.path.expanduser('~/.openclaw-staging/openclaw.json')
with open(path) as f:
    d = json.load(f)

# Fix 1: remote token must match auth token
token = d['gateway']['auth']['token']
d.setdefault('gateway', {}).setdefault('remote', {})
d['gateway']['remote']['url'] = 'ws://127.0.0.1:18810'
d['gateway']['remote']['token'] = token

# Fix 2: GOG_KEYRING_PASSWORD must be under "env" — NOT at root
d.setdefault('env', {})['GOG_KEYRING_PASSWORD'] = 'YOUR_KEYRING_PASSWORD'

with open(path, 'w') as f:
    json.dump(d, f, indent=2)
```

Update Slack tokens to use the **staging app** (from Step 3):

```python
import json, os

path = os.path.expanduser('~/.openclaw-staging/openclaw.json')
with open(path) as f:
    d = json.load(f)

d['channels']['slack']['botToken'] = 'xoxb-YOUR_STAGING_BOT_TOKEN'
d['channels']['slack']['appToken'] = 'xapp-YOUR_STAGING_APP_TOKEN'

with open(path, 'w') as f:
    json.dump(d, f, indent=2)
```

> **Never rewrite the entire file** — always load → patch → save.

---

## 5. Configure Loop Prevention

Two bots in the same workspace will respond to each other infinitely unless prevented. The safest approach: staging requires `@openclaw_staging` mention to respond.

```python
import json, os

path = os.path.expanduser('~/.openclaw-staging/openclaw.json')
with open(path) as f:
    d = json.load(f)

# Staging responds in all channels but only when @mentioned.
# Merge into existing per-channel rules — do not replace the whole map.
slack = d.setdefault('channels', {}).setdefault('slack', {})
raw = slack.get('channels')
ch_map = dict(raw) if isinstance(raw, dict) else {}
ch_map['*'] = {'allow': True, 'requireMention': True}
slack['channels'] = ch_map

with open(path, 'w') as f:
    json.dump(d, f, indent=2)
```

This prevents loops because:
- Production won't `@mention` staging in its replies
- Staging won't `@mention` production in its replies
- Each gateway self-ignores its own bot user ID automatically

> **Do NOT use `ignoredUsers`** — it is not a recognized config key and will crash the gateway on startup.

---

## 6. Run staging gateway on demand

Keep **one** always-on gateway (production on 18789 via launchd). Run staging only when you need it so you avoid two launchd-managed OpenClaw processes competing for locks and Slack socket routing.

When you are done testing, stop staging (Ctrl+C in the terminal or end the process) so only production is connected to Slack.

```bash
OPENCLAW_ALLOW_MULTI_GATEWAY=1 openclaw --profile staging gateway run
```

`OPENCLAW_ALLOW_MULTI_GATEWAY=1` is required because two gateways on one machine is otherwise blocked.

**macOS terminal quirk:** `XPC_SERVICE_NAME=0` is often set in interactive shells, which can make OpenClaw assume launchd supervision and hit lock/retry issues. If `gateway run` misbehaves in Terminal.app or iTerm, try the same command from a context where that variable is unset (for example an SSH session to `127.0.0.1`, or a small wrapper script run without a full GUI login environment).

Logs while staging is running:

```bash
tail -f ~/.openclaw-staging/logs/gateway.log
```

Do **not** run `openclaw --profile staging gateway install` for this workflow — that registers a second LaunchAgent. This guide intentionally avoids a staging launchd service.

---

## 7. Verify both gateways

Production (should be up whenever your Mac is on, if you installed main via launchd):

```bash
curl -s http://127.0.0.1:18789/health   # → {"ok":true,"status":"live"}
launchctl list | grep ai.openclaw.gateway
```

Staging (after launchd bootstraps it via §6):

```bash
# Allow ~30s for startup (OAuth refresh + Slack socket connect before port opens)
sleep 30
curl -s http://127.0.0.1:18810/health   # → {"ok":true,"status":"live"}
launchctl list | grep ai.openclaw.staging
```

If the health check fails after 30s, check the logs before retrying — a silent startup
failure (port never opens, no new log entries) usually means an openclaw version mismatch.
See the Version check step in §2.

---

## 8. Git track ~/.openclaw-staging/

```bash
cd ~/.openclaw-staging
git init
git remote add origin https://github.com/YOUR_ORG/openclaw-staging-config.git

cat >> .gitignore << 'EOF'
openclaw.json
credentials/
agents/*/sessions/
logs/
workspace/
EOF

git add .
git commit -m "chore: initial openclaw-staging config"
git push -u origin main
```

> Never commit `openclaw.json` — it contains live tokens.

---

## 9. Launchd backup job

Runs at 4am daily, archives staging config (secrets excluded) to `~/.openclaw-staging-backups/`.

Create `~/.openclaw-staging/scripts/backup.sh`:

```bash
#!/bin/bash
DEST="$HOME/.openclaw-staging-backups/backup-$(date +%Y%m%d-%H%M%S).tar.gz"
mkdir -p "$(dirname "$DEST")"
tar czf "$DEST" \
  --exclude='openclaw.json' \
  --exclude='credentials/' \
  --exclude='logs/' \
  --exclude='workspace/' \
  --exclude='agents/*/sessions/' \
  -C "$HOME" .openclaw-staging
find "$HOME/.openclaw-staging-backups" -name '*.tar.gz' -mtime +30 -delete
echo "Backup: $DEST"
```

```bash
chmod +x ~/.openclaw-staging/scripts/backup.sh
```

Create plist (replace `YOUR_USER`):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.openclaw.staging.backup</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>/Users/YOUR_USER/.openclaw-staging/scripts/backup.sh</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>4</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key>
  <string>/Users/YOUR_USER/.openclaw-staging/logs/backup.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/YOUR_USER/.openclaw-staging/logs/backup.err.log</string>
</dict></plist>
```

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.openclaw.staging.backup.plist
```

---

## Key paths

| Path | Purpose |
|------|---------|
| `~/.openclaw-staging/` | Staging config root (git repo) |
| `~/.openclaw-staging/openclaw.json` | Staging config — gitignored, contains tokens |
| `~/.openclaw-staging/logs/gateway.log` | Staging gateway logs (when running §6) |
| `~/.openclaw/` | Main config (unchanged) |
| `~/Library/LaunchAgents/ai.openclaw.gateway.plist` | Main launchd service |
