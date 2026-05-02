# Staging → Production Pipeline Skill

**Skill**: `staging-prod-pipeline`
**Purpose**: Install, verify, operate, and troubleshoot the OpenClaw staging→production deployment pipeline
**Scope**: `~/.smartclaw/` (staging, port 18810) ↔ `~/.smartclaw_prod/` (production, port 18789)

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  ~/.smartclaw/  (STAGING — the git repo)                      │
│  ├── openclaw.json         ← staging config                 │
│  ├── staging-canary.sh     ← pre-deploy validation          │
│  ├── gateway-preflight.sh  ← pre-upgrade checks              │
│  ├── deploy.sh             ← promotes staging → prod         │
│  ├── launchd/                                               │
│  │   ├── ai.smartclaw.staging.plist  → ai.smartclaw.staging   │
│  │   └── com.smartclaw.gateway.plist → com.smartclaw.gateway  │
│  └── logs/staging-gateway.log                               │
│  Port: 18810  |  Label: ai.smartclaw.staging                  │
└──────────────────────────────────────────────────────────────┘
                           ↓ deploy.sh
┌──────────────────────────────────────────────────────────────┐
│  ~/.smartclaw_prod/  (PRODUCTION — separate dir)             │
│  ├── openclaw.json         ← synced from staging            │
│  ├── cron/                  ← synced from staging            │
│  ├── scripts/               ← synced from staging            │
│  ├── workspace/             ← synced from staging            │
│  └── logs/gateway.log                                       │
│  Port: 18789  |  Label: com.smartclaw.gateway                 │
└──────────────────────────────────────────────────────────────┘
```

### Key Constraints
- `~/.smartclaw/` = staging. `~/.smartclaw_prod/` = production. They are **separate directories**.
- Tokens live in `openclaw.json` — never in plist files.
- Node binary for production plist is **nvm Node 22** (`~/.nvm/versions/node/v22.22.0/bin/node`), NOT Homebrew Node.
- `ThrottleInterval` must be ≥ 10 (preferably 30) — values < 10 cause restart storms.
- Gateway plist **must be XML** (not binary) — binary plists cause launchd to misread `RunAtLoad`/`KeepAlive`/`PATH`.

---

## Prerequisites

Before the pipeline works, the following must be true:

| Requirement | How to verify |
|---|---|
| macOS (launchd) | `uname -s` → `Darwin` |
| openclaw CLI installed | `openclaw --version` |
| Node.js (nvm v22.22.0) | `~/.nvm/versions/node/v22.22.0/bin/node --version` |
| `~/.smartclaw/` cloned | `ls ~/.smartclaw/openclaw.json` |
| `~/.smartclaw_prod/` created | `ls ~/.smartclaw_prod/` |
| Git remote points to jleechanorg/smartclaw | `git remote get-url origin` (must contain `smartclaw`) |

---

## Installation (Fresh Machine)

Run in order. Each step is idempotent — safe to re-run.

### Step 1: Clone the repo

```bash
git clone https://github.com/jleechanorg/smartclaw.git ~/.smartclaw
```

### Step 2: Bootstrap

```bash
bash ~/.smartclaw/scripts/bootstrap.sh
```

This installs:
- `~/agent-orchestrator.yaml` → symlink to `~/.smartclaw/agent-orchestrator.yaml`
- LaunchAgents (core + scheduled jobs)
- openclaw CLI via npm
- webhook secret → `~/.smartclaw/webhook.json`
- gog auth check

### Step 3: Create `~/.smartclaw_prod/`

```bash
mkdir -p ~/.smartclaw_prod/logs
# Seed config from staging (if staging exists)
[[ -f ~/.smartclaw/openclaw.json ]] && cp ~/.smartclaw/openclaw.json ~/.smartclaw_prod/openclaw.json
# Symlink shared resources
for target in SOUL.md TOOLS.md HEARTBEAT.md extensions agents credentials lcm.db; do
  [[ -e ~/.smartclaw/$target ]] && ln -sf ~/.smartclaw/$target ~/.smartclaw_prod/$target
done
```

Or use the installer (creates prod dir automatically):

```bash
bash ~/.smartclaw/scripts/install.sh
```

### Step 4: Install launchd plists

```bash
bash ~/.smartclaw/scripts/install-launchagents.sh
```

This:
1. Substitutes `@HOME@`, `@NODE_PATH@`, `@OPENCLAW_BIN@`, etc. in plist templates
2. Creates `~/Library/LaunchAgents/ai.smartclaw.staging.plist`
3. Creates `~/Library/LaunchAgents/com.smartclaw.gateway.plist`
4. Bootstraps both via `launchctl bootstrap gui/$(id -u) <plist>`

### Step 5: Verify both gateways start

```bash
# Staging (port 18810)
launchctl start gui/$(id -u)/ai.smartclaw.staging
sleep 20
curl -sf --max-time 8 http://127.0.0.1:18810/health

# Production (port 18789)
launchctl start gui/$(id -u)/com.smartclaw.gateway
sleep 20
curl -sf --max-time 8 http://127.0.0.1:18789/health
```

Expected: HTTP 200 JSON response with `status: "ok"` or similar.

### Step 6: Record NODE_MODULE_VERSION baseline

```bash
GATEWAY_NODE="$HOME/.nvm/versions/node/v22.22.0/bin/node"
MODVER=$("$GATEWAY_NODE" -e "process.stdout.write(String(process.versions.modules))")
echo "$MODVER" > "$HOME/.smartclaw/.gateway-node-version"
echo "$MODVER" > "$HOME/.smartclaw_prod/.gateway-node-version"
```

---

## Deploy Procedure

```bash
cd ~/.smartclaw
bash scripts/deploy.sh
```

**Full flow:**
1. **Preflight** — `gateway-preflight.sh` validates plists, SDK version, config keys; add `--fix` to auto-repair
2. **Staging validation** — start staging gateway, run `staging-canary.sh --port 18810`, run `monitor-agent.sh` against staging
3. **Push to origin/main** — merge current branch, push to `origin/main` (BLOCKED if monitor fails)
4. **Sync config** — `rsync` scripts/, workspace/, memory/, cron/ from staging → prod; `cp openclaw.json`
5. **Symlink shared resources** — SOUL.md, TOOLS.md, HEARTBEAT.md, extensions, agents, credentials
6. **Restart prod gateway** — stop → start → wait 20s → canary + monitor-agent
7. **Success alert** — Slack DM to `C0AP8LRKM9N` (success channel), `${SLACK_CHANNEL_ID}` (failure channel)

**Shortcuts:**
```bash
./scripts/deploy.sh --skip-push    # already pushed; just re-deploy
./scripts/deploy.sh --prod-only    # skip staging checks; deploy directly to prod
```

---

## Canary Verification

```bash
# Staging (port 18810)
bash ~/.smartclaw/scripts/staging-canary.sh --port 18810

# Production (port 18789)
OPENCLAW_STAGING_CONFIG="$HOME/.smartclaw_prod/openclaw.json" \
  bash ~/.smartclaw/scripts/staging-canary.sh --port 18789
```

**7 checks:**
1. Gateway listening on port (HTTP 200 on `/health`)
2. Config schema validation (no `cmuxBotToken`, no `checkCompatibility` at wrong level, JSON valid, critical keys present)
3. Native module ABI (better-sqlite3 loads with the plist's Node binary)
4. Slack app token validity (`apps.connections.open` succeeds)
5. SDK protocol version (major=0, minor≤16 — `0.17` breaks ws-stream)
6. Heartbeat response time (< 5000ms)
7. Stale session lock check (no dead PID in `.lock` files)

**Exit codes:**
- `0` = all 7 pass → safe to deploy to production
- `1` = any fail → do NOT apply to production

---

## Rollback Procedure

### If last deploy is bad

```bash
# 1. Stop prod gateway
launchctl stop gui/$(id -u)/com.smartclaw.gateway

# 2. Restore config from staging (no .bak file created by deploy.sh)
cp ~/.smartclaw/openclaw.json ~/.smartclaw_prod/openclaw.json
# If staging config is also bad, restore from git:
# git -C ~/.smartclaw show HEAD:openclaw.json > ~/.smartclaw_prod/openclaw.json

# 3. Restart prod
launchctl start gui/$(id -u)/com.smartclaw.gateway
sleep 20

# 4. Verify
curl -sf --max-time 8 http://127.0.0.1:18789/health
bash ~/.smartclaw/scripts/staging-canary.sh --port 18789
```

### If consensus config version drifted

```bash
bash ~/.smartclaw/scripts/gateway-preflight.sh --fix
```

This auto-corrects `meta.lastTouchedVersion` in `~/.smartclaw-consensus/openclaw.json` to match the running binary version.

---

## Key File Paths

| File | Purpose |
|---|---|
| `~/.smartclaw/openclaw.json` | Staging config (port 18810) |
| `~/.smartclaw_prod/openclaw.json` | Production config (port 18789) |
| `~/.smartclaw/scripts/deploy.sh` | Main deploy script |
| `~/.smartclaw/scripts/staging-canary.sh` | 7-point canary test |
| `~/.smartclaw/scripts/gateway-preflight.sh` | Pre-upgrade validation + SDK compat check |
| `~/.smartclaw/scripts/install.sh` | Full installer (creates prod dir) |
| `~/.smartclaw/scripts/install-launchagents.sh` | Installs plists, creates prod dir |
| `~/.smartclaw/launchd/ai.smartclaw.staging.plist` | Staging plist template (port 18810) |
| `~/.smartclaw/launchd/com.smartclaw.gateway.plist` | Production plist template (port 18789) |
| `~/Library/LaunchAgents/ai.smartclaw.staging.plist` | Deployed staging plist |
| `~/Library/LaunchAgents/com.smartclaw.gateway.plist` | Deployed production plist |
| `~/.smartclaw/logs/staging-gateway.log` | Staging gateway stdout |
| `~/.smartclaw/logs/staging-gateway.err.log` | Staging gateway stderr |
| `~/.smartclaw_prod/logs/gateway.log` | Production gateway stdout |
| `~/.smartclaw_prod/logs/gateway.err.log` | Production gateway stderr |
| `~/.smartclaw/.gateway-node-version` | NODE_MODULE_VERSION baseline for staging |
| `~/.smartclaw_prod/.gateway-node-version` | NODE_MODULE_VERSION baseline for prod |
| `~/.smartclaw/.current-sdk-version` | @agentclientprotocol/sdk baseline |
| `~/.smartclaw-consensus/openclaw.json` | Consensus config (AJV version tracking) |
| `~/.smartclaw_prod/openclaw.json.bak.*` | Rotating backups created by `install-launchagents.sh` (not by deploy.sh) |
| `/tmp/staging-canary.log` | Staging canary output |
| `/tmp/prod-canary.log` | Production canary output |
| `/tmp/staging-monitor.log` | Staging monitor-agent output |
| `/tmp/prod-monitor.log` | Production monitor-agent output |

---

## Troubleshooting

### Gateway not responding on port

```bash
# Check if process is listening
lsof -i :18810 2>/dev/null || lsof -i :18789 2>/dev/null

# Check launchd state
launchctl print gui/$(id -u)/ai.smartclaw.staging
launchctl print gui/$(id -u)/com.smartclaw.gateway

# Check stderr log for errors
tail -50 ~/.smartclaw_prod/logs/gateway.err.log
```

### Session lock silent failure (HTTP 200 but messages dropped)

Check `gateway.err.log` for `session file locked (timeout N ms)`. Fix:

```bash
find ~/.smartclaw/agents/main/sessions/ -name "*.lock" | while read f; do
  raw=$(cat "$f")
  pid=$(echo "$raw" | python3 -c "import sys,json; print(json.load(sys.stdin)['pid'])" 2>/dev/null || echo "$raw" | tr -d '[:space:]')
  [[ "$pid" =~ ^[0-9]+$ ]] && ! kill -0 "$pid" 2>/dev/null && rm -f "$f" && echo "removed: $f"
done
```

Then restart gateway.

### AJV stack overflow (meta.lastTouchedVersion newer than binary)

```bash
bash ~/.smartclaw/scripts/gateway-preflight.sh --fix
launchctl stop gui/$(id -u)/com.smartclaw.gateway && launchctl start gui/$(id -u)/com.smartclaw.gateway
```

### Native module (better-sqlite3) fails to load after Node upgrade

```bash
# Verify NODE_MODULE_VERSION baseline
GATEWAY_NODE="$HOME/.nvm/versions/node/v22.22.0/bin/node"
"$GATEWAY_NODE" -e "require('$HOME/.smartclaw/extensions/openclaw-mem0/node_modules/better-sqlite3')" 2>/dev/null || echo "FAIL"
# If fails: rebuild
cd ~/.smartclaw/extensions/openclaw-mem0
"$GATEWAY_NODE" npm rebuild better-sqlite3
# Verify
"$GATEWAY_NODE" -e "require('$HOME/.smartclaw/extensions/openclaw-mem0/node_modules/better-sqlite3')" && echo "OK"
```

### plist binary encoding issue (RunAtLoad/KeepAlive/PATH read as absent)

```bash
# Detect
file ~/Library/LaunchAgents/com.smartclaw.gateway.plist
# Fix — convert to XML so openclaw CLI / gateway status commands can parse it
plutil -convert xml1 ~/Library/LaunchAgents/com.smartclaw.gateway.plist
launchctl kickstart -k gui/$(id -u)/com.smartclaw.gateway
```

### Duplicate plists causing port conflict

```bash
# Find all gateway plists
ls ~/Library/LaunchAgents/*openclaw*gateway*
# Two gateways run simultaneously on different ports — both are needed:
#   com.smartclaw.gateway  (port 18789, production)
#   ai.smartclaw.staging    (port 18810, staging)
# Remove duplicates that are neither of the above labels
# Fix via preflight
bash ~/.smartclaw/scripts/gateway-preflight.sh --fix
```

### ThrottleInterval too low (< 10)

```bash
defaults write ~/Library/LaunchAgents/com.smartclaw.gateway.plist ThrottleInterval -int 30
launchctl kickstart -k gui/$(id -u)/com.smartclaw.gateway
```

---

## Maintenance

### Upgrading openclaw through the staging gate

```bash
# 1. Validate new version's SDK compatibility
bash ~/.smartclaw/scripts/gateway-preflight.sh
# The script exports validate_sdk_compatibility() — call it:
source ~/.smartclaw/scripts/gateway-preflight.sh
validate_sdk_compatibility <new-openclaw-version>
# If FAIL: major SDK jump — do NOT upgrade

# 2. Upgrade staging
npm install -g openclaw@<new-version>

# 3. Run full staging canary
bash ~/.smartclaw/scripts/staging-canary.sh --port 18810

# 4. If all pass → deploy
bash ~/.smartclaw/scripts/deploy.sh
```

### Recurring health check (every hour via launchd)

```bash
bash ~/.smartclaw/monitor-agent.sh
# Logs: ~/.smartclaw/logs/monitor-agent.log
# Schedule: LaunchAgent ai.smartclaw.monitor-agent runs every 3600s (1 hour)
```

---

## Launchd Labels Reference

| Label | Port | State dir | Purpose |
|---|---|---|---|
| `ai.smartclaw.staging` | 18810 | `~/.smartclaw/` | Staging gateway |
| `com.smartclaw.gateway` | 18789 | `~/.smartclaw_prod/` | Production gateway |
| `ai.smartclaw.monitor-agent` | — | `~/.smartclaw/` | Periodic health check (every 30 min) |
| `ai.smartclaw.startup-check` | — | `~/.smartclaw/` | Startup verification on login |
| `com.smartclaw.mem0-watchdog` | — | `~/.smartclaw/` | Periodic better-sqlite3 ABI check |

---

## TODOs (Gaps Found During Analysis)

| Issue | Status | Notes |
|---|---|---|
| `staging-gateway.sh` referenced in OUTAGE_PREVENTION_ROADMAP.md but no longer needed | RESOLVED | Staging gateway now installed via `install-launchagents.sh` |
| Roadmap doc shows port 18790 for staging, actual is 18810 | FIXED IN SKILL | Staging is port **18810**, not 18790 |
| Install script creates `~/Library/LaunchAgents/ai.smartclaw.staging.plist` (lowercase) | RESOLVED | `install-launchagents.sh` handles both gateways |
| Linux systemd install for staging gateway not yet implemented | TODO | Only production gateway has systemd unit on Linux |

---

## Emergency Contacts

- **Production gateway**: `launchctl print gui/$(id -u)/com.smartclaw.gateway`
- **Staging gateway**: `launchctl print gui/$(id -u)/ai.smartclaw.staging`
- **Slack health channel**: `#C0AJ3SD5C79` (OpenClaw design channel)
- **Logs**: `~/.smartclaw_prod/logs/gateway.err.log` (look for `session file locked`, `cmuxBotToken`, `AJV`, `ws-stream`)