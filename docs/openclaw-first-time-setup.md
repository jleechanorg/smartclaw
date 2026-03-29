# OpenClaw First-Time Setup

For a fresh machine with no existing OpenClaw installation.

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Install OpenClaw](#2-install-openclaw)
3. [Onboard](#3-onboard)
4. [Patch openclaw.json](#4-patch-openclawingjson)
5. [Create Slack App](#5-create-slack-app)
6. [Install as launchd service](#6-install-as-launchd-service)
7. [Set up gog (Google OAuth CLI)](#7-set-up-gog-google-oauth-cli)
8. [Git track ~/.openclaw/](#8-git-track-openclaw)
9. [Launchd backup job](#9-launchd-backup-job)

---

## 1. Prerequisites

- macOS (Apple Silicon or Intel)
- Node.js 22.12+

```bash
nvm install 22 && nvm use 22
node --version   # v22.x
```

- A Slack workspace where you are an admin (to create a bot)
- gog CLI for Google integrations (optional, Step 7)

---

## 2. Install OpenClaw

```bash
npm install -g openclaw
openclaw --version
```

---

## 3. Onboard

Creates `~/.openclaw/` with config and workspace.

```bash
openclaw onboard \
  --accept-risk \
  --auth-choice openai-codex \
  --gateway-port 18789 \
  --gateway-bind loopback \
  --gateway-auth token \
  --gateway-token "$(openssl rand -hex 32)" \
  --workspace ~/.openclaw/workspace \
  --flow quickstart
```

---

## 4. Patch openclaw.json

Two required fixes the onboard wizard doesn't set:

```python
import json, os

path = os.path.expanduser('~/.openclaw/openclaw.json')
with open(path) as f:
    d = json.load(f)

# Fix 1: remote token must match auth token (CLI can't connect otherwise)
token = d['gateway']['auth']['token']
d.setdefault('gateway', {}).setdefault('remote', {})
d['gateway']['remote']['url'] = 'ws://127.0.0.1:18789'
d['gateway']['remote']['token'] = token

# Fix 2: GOG_KEYRING_PASSWORD must be under "env" — NOT at root
# Root-level unknown keys cause "Config invalid — Unrecognized key" on startup
d.setdefault('env', {})['GOG_KEYRING_PASSWORD'] = 'YOUR_KEYRING_PASSWORD'

with open(path, 'w') as f:
    json.dump(d, f, indent=2)
```

> **Never rewrite the entire file** — always load → patch → save. Full rewrites silently drop config sections not in scope.

---

## 5. Create Slack App

Each OpenClaw instance needs its own Slack app to avoid token conflicts. If two gateways share one bot token, Slack delivers events to both WebSocket connections causing race conditions and dropped messages.

Go to `https://api.slack.com/apps` → **Create New App** → **From an app manifest** → select your workspace → paste:

```json
{
  "display_information": {
    "name": "openclaw",
    "description": "OpenClaw AI agent gateway",
    "background_color": "#2c2d30"
  },
  "features": {
    "bot_user": {
      "display_name": "openclaw",
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
3. **Basic Information** → **App-Level Tokens** → Generate with `connections:write` scope → copy `xapp-...` token

Add tokens to `openclaw.json` (Step 4 python patch or manually under `channels.slack.botToken` / `channels.slack.appToken`).

Optionally export to `~/.bashrc` for scripts that need them:

```bash
# Gateway reads from openclaw.json directly, but env fallback names are:
export SLACK_BOT_TOKEN="xoxb-..."
export SLACK_APP_TOKEN="xapp-..."

# Custom aliases for your own scripts (NOT read by gateway):
export OPENCLAW_SLACK_BOT_TOKEN="$SLACK_BOT_TOKEN"
export OPENCLAW_SLACK_APP_TOKEN="$SLACK_APP_TOKEN"
```

---

## 6. Install as launchd service

```bash
openclaw gateway install
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.openclaw.gateway.plist
```

Verify:

```bash
curl http://127.0.0.1:18789/health        # → {"ok":true,"status":"live"}
launchctl list | grep ai.openclaw.gateway # shows PID
```

Manage:

```bash
launchctl stop gui/$(id -u)/ai.openclaw.gateway
launchctl start gui/$(id -u)/ai.openclaw.gateway
tail -f ~/.openclaw/logs/gateway.log
```

---

## 7. Set up gog (Google OAuth CLI)

gog manages Google OAuth tokens headlessly for Drive, Gmail, Calendar, Docs, etc.

```bash
bun install -g gogcli    # or: npm install -g gogcli
```

Authenticate (opens browser):

```bash
gog auth add you@gmail.com \
  --services drive,gmail,calendar,docs,contacts,tasks
gog auth status
```

Verify services work:

```bash
gog drive ls
gog gmail search 'is:unread newer_than:1d' --max 10 --no-input
gog docs list-tabs <docId>
```

The `GOG_KEYRING_PASSWORD` set in Step 4 unlocks the macOS Keychain headlessly so the gateway can use gog without prompting.

Write to a specific Google Docs tab (gog's `write` only targets Tab 1):

```bash
~/.openclaw/scripts/gdoc-write-tab.sh <docId> <tabId> <file>
gog docs list-tabs <docId>   # get tab IDs
```

---

## 8. Git track ~/.openclaw/

```bash
cd ~/.openclaw
git init
git remote add origin https://github.com/YOUR_ORG/openclaw-config.git

cat >> .gitignore << 'EOF'
openclaw.json
credentials/
agents/*/sessions/
logs/
workspace/
EOF

git add .
git commit -m "chore: initial openclaw config"
git push -u origin main
```

> Never commit `openclaw.json` — it contains live tokens.

---

## 9. Launchd backup job

Runs at 3am daily, archives config (secrets excluded) to `~/.openclaw-backups/`.

Create `~/.openclaw/scripts/backup.sh`:

```bash
#!/bin/bash
DEST="$HOME/.openclaw-backups/backup-$(date +%Y%m%d-%H%M%S).tar.gz"
mkdir -p "$(dirname "$DEST")"
tar czf "$DEST" \
  --exclude='openclaw.json' \
  --exclude='credentials/' \
  --exclude='logs/' \
  --exclude='workspace/' \
  --exclude='agents/*/sessions/' \
  -C "$HOME" .openclaw
find "$HOME/.openclaw-backups" -name '*.tar.gz' -mtime +30 -delete
echo "Backup: $DEST"
```

```bash
chmod +x ~/.openclaw/scripts/backup.sh
```

Create plist (replace `YOUR_USER`):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.openclaw.backup</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>/Users/YOUR_USER/.openclaw/scripts/backup.sh</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>3</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key>
  <string>/Users/YOUR_USER/.openclaw/logs/backup.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/YOUR_USER/.openclaw/logs/backup.err.log</string>
</dict></plist>
```

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.openclaw.backup.plist
```

---

## Key paths

| Path | Purpose |
|------|---------|
| `~/.openclaw/` | Config root (git repo) |
| `~/.openclaw/openclaw.json` | Main config — gitignored, contains tokens |
| `~/Library/LaunchAgents/ai.openclaw.gateway.plist` | launchd service |
| `~/.openclaw/logs/gateway.log` | Gateway logs |
| `~/.openclaw/scripts/gdoc-write-tab.sh` | Write to Google Docs tab via API |
| `~/.openclaw/TOOLS.md` | Full gog + tool reference |
