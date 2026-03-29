# OpenClaw Staging Setup Guide

For machines that already have OpenClaw running (main gateway on port 18789).

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Onboard Staging Profile](#2-onboard-staging-profile)
3. [Patch openclaw.json](#3-patch-openclawingjson)
4. [Install as separate launchd service](#4-install-as-separate-launchd-service)
5. [Verify both gateways](#5-verify-both-gateways)
6. [Git track ~/.openclaw-staging/](#6-git-track-openclaw-staging)
7. [Launchd backup job](#7-launchd-backup-job)

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

## 3. Patch openclaw.json

Same two fixes as main — but targeting `~/.openclaw-staging/openclaw.json`:

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

> **Never rewrite the entire file** — always load → patch → save.

---

## 4. Install as separate launchd service

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

## 5. Verify both gateways

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

## 6. Git track ~/.openclaw-staging/

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

## 7. Launchd backup job

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
