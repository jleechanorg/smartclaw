# OpenClaw Setup Guide

## Table of Contents

### First-Time Setup
1. [Install OpenClaw](#install)
2. [Onboard & configure openclaw.json](#openclawingjson-required-patches)
3. [Install as launchd service](#install) _(included in install step)_
4. [Set up gog (Google OAuth CLI)](#gog-google-oauth-cli)
5. [Git track ~/.openclaw/](#git-tracking)
6. [Launchd backup job](#launchd-backup)

### Staging Setup _(already have OpenClaw running)_
1. [Onboard staging profile](#staging-alongside-main)
2. [Patch ~/.openclaw-staging/openclaw.json](#openclawingjson-required-patches)
3. [Install as separate launchd service](#staging-alongside-main) _(included in staging step)_
4. [Verify both gateways](#staging-alongside-main)
5. [Git track ~/.openclaw-staging/](#git-tracking)
6. [Launchd backup for staging](#launchd-backup)

### Reference
- [Key paths](#key-paths)
- [Full step-by-step Google Doc](https://docs.google.com/document/d/1VIGahkFRQgfSq2dBZSp5HU6LZOWCEHyPtXrBcGasFwI/edit)

---

Full setup instructions are maintained as a living Google Doc with two tabs:

**→ https://docs.google.com/document/d/1VIGahkFRQgfSq2dBZSp5HU6LZOWCEHyPtXrBcGasFwI/edit**

| Tab | Audience | Contents |
|-----|----------|----------|
| **First-Time Setup** | Fresh machine, no OpenClaw | Install, onboard, launchd, gog auth, git tracking, backup job |
| **Staging Setup** | Already running OpenClaw | Isolated `~/.openclaw-staging/`, separate launchd service, port spacing, testing |

---

## Quick Reference

### Install

```bash
npm install -g openclaw
openclaw onboard \
  --accept-risk \
  --auth-choice openai-codex \
  --gateway-port 18789 \
  --gateway-bind loopback \
  --gateway-auth token \
  --gateway-token "$(openssl rand -hex 32)" \
  --workspace ~/.openclaw/workspace \
  --flow quickstart
openclaw gateway install
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.openclaw.gateway.plist
curl http://127.0.0.1:18789/health   # → {"ok":true,"status":"live"}
```

### Staging (alongside main)

Port must be ≥20 away from main (each gateway spawns derived ports up to base+108).

```bash
openclaw --profile staging onboard \
  --accept-risk --auth-choice openai-codex \
  --gateway-port 18810 --gateway-bind loopback \
  --gateway-auth token --gateway-token "$(openssl rand -hex 32)" \
  --workspace ~/.openclaw-staging/workspace --flow quickstart
openclaw --profile staging gateway install
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.openclaw.staging.plist
curl http://127.0.0.1:18810/health   # → {"ok":true,"status":"live"}
```

**Why not `gateway run`?** `XPC_SERVICE_NAME=0` is always set in macOS terminals, making openclaw think it's supervised by launchd. Lock conflicts → infinite retry loop. `gateway install` + launchd avoids this. For one-off testing: `OPENCLAW_ALLOW_MULTI_GATEWAY=1 openclaw --profile staging gateway run`.

### openclaw.json required patches

```python
import json, os

path = os.path.expanduser('~/.openclaw/openclaw.json')
with open(path) as f: d = json.load(f)

token = d['gateway']['auth']['token']
d.setdefault('gateway', {}).setdefault('remote', {})
d['gateway']['remote']['url'] = 'ws://127.0.0.1:18789'
d['gateway']['remote']['token'] = token          # must match auth.token
d.setdefault('env', {})['GOG_KEYRING_PASSWORD'] = 'YOUR_PASSWORD'  # must be under "env", NOT root

with open(path, 'w') as f: json.dump(d, f, indent=2)
```

`GOG_KEYRING_PASSWORD` at root (not under `"env"`) causes "Config invalid — Unrecognized key" on startup.

### gog (Google OAuth CLI)

```bash
bun install -g gogcli
gog auth add you@gmail.com --services drive,gmail,calendar,docs,contacts,tasks
gog auth status
```

Write to a specific Google Docs tab (gog's `write` command only targets Tab 1):

```bash
~/.openclaw/scripts/gdoc-write-tab.sh <docId> <tabId> <file>
gog docs list-tabs <docId>   # get tab IDs
```

### Git tracking

```bash
cd ~/.openclaw
git init && git remote add origin https://github.com/YOUR_ORG/openclaw-config.git
echo -e "openclaw.json\ncredentials/\nagents/*/sessions/\nlogs/\nworkspace/" >> .gitignore
git add . && git commit -m "chore: initial openclaw config" && git push -u origin main
```

### Launchd backup

```bash
# Daily git commit backup for staging
cat > ~/Library/LaunchAgents/com.openclaw.staging.backup.plist << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.openclaw.staging.backup</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>-c</string>
  <string>cd ~/.openclaw-staging &amp;&amp; git add -A &amp;&amp; git commit -m "backup: $(date +%Y%m%d-%H%M%S)" || true</string></array>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>4</integer><key>Minute</key><integer>0</integer></dict>
</dict></plist>
EOF
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.openclaw.staging.backup.plist
```

---

## Key paths

| Path | Purpose |
|------|---------|
| `~/.openclaw/` | Main config (git repo) |
| `~/.openclaw-staging/` | Staging config (isolated) |
| `~/Library/LaunchAgents/ai.openclaw.gateway.plist` | Main launchd service |
| `~/Library/LaunchAgents/ai.openclaw.staging.plist` | Staging launchd service |
| `~/.openclaw/scripts/gdoc-write-tab.sh` | Write to Google Docs tab via API |
| `~/.openclaw/TOOLS.md` | Full gog + tool reference |
