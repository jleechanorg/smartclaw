# OpenClaw Staging Setup Guide

For machines that already have OpenClaw running (main gateway on port 18789).

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Onboard Staging Profile](#2-onboard-staging-profile)
3. [Create Separate Slack App](#3-create-separate-slack-app)
4. [Patch openclaw.json](#4-patch-openclawingjson)
5. [Configure Loop Prevention](#5-configure-loop-prevention)
6. [Install as separate launchd service](#6-install-as-separate-launchd-service)
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

Add tokens to `~/.bashrc` (alongside the production tokens):

```bash
# OpenClaw STAGING Slack tokens (app openclaw_staging, keep secure, never commit)
export OPENCLAW_STAGING_SLACK_BOT_TOKEN="xoxb-..."
export OPENCLAW_STAGING_SLACK_APP_TOKEN="xapp-..."
```

Also add to `~/.profile` for scripts that source it.

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

# Staging responds in all channels but only when @mentioned
d['channels']['slack']['channels'] = {
    '*': {'allow': True, 'requireMention': True}
}

with open(path, 'w') as f:
    json.dump(d, f, indent=2)
```

This prevents loops because:
- Production won't `@mention` staging in its replies
- Staging won't `@mention` production in its replies
- Each gateway self-ignores its own bot user ID automatically

> **Do NOT use `ignoredUsers`** — it is not a recognized config key and will crash the gateway on startup.

---

## 6. Install as separate launchd service

```bash
openclaw --profile staging gateway install
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.openclaw.staging.plist
```

**Why not `gateway run`?** `XPC_SERVICE_NAME=0` is always set in macOS terminals, making openclaw think it's supervised by launchd. Lock conflicts → infinite retry loop. `gateway install` + launchd avoids this.

For one-off testing only:
```bash
OPENCLAW_ALLOW_MULTI_GATEWAY=1 openclaw --profile staging gateway run
```

---

## 7. Verify both gateways

```bash
curl http://127.0.0.1:18789/health   # main  → {"ok":true,"status":"live"}
curl http://127.0.0.1:18810/health   # staging → {"ok":true,"status":"live"}

launchctl list | grep ai.openclaw    # shows PIDs for both
```

Manage staging:

```bash
launchctl stop gui/$(id -u)/ai.openclaw.staging
launchctl start gui/$(id -u)/ai.openclaw.staging
tail -f ~/.openclaw-staging/logs/gateway.log
```

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
| `~/Library/LaunchAgents/ai.openclaw.staging.plist` | Staging launchd service |
| `~/.openclaw-staging/logs/gateway.log` | Staging gateway logs |
| `~/.openclaw/` | Main config (unchanged) |
| `~/Library/LaunchAgents/ai.openclaw.gateway.plist` | Main launchd service |
