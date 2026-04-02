# OpenClaw Node Version Management

## Problem: Dual Node ABI Mismatch

OpenClaw's gateway uses a hardcoded Node binary path in the launchd plist
(`~/Library/LaunchAgents/ai.smartclaw.gateway.plist`). The `openclaw-mem0` extension
includes `better-sqlite3`, a native Node module compiled against a specific
`NODE_MODULE_VERSION` (127 = Node 22, 137 = Node 24).

**Failure scenario**: If the machine has both **nvm Node 22** and **Homebrew Node 24**,
scripts that detect "which Node should I use?" can accidentally pick Homebrew (wrong
version), rebuild `better-sqlite3` for Node 24, and silently break `memory_lookup`
until manually fixed.

This happened repeatedly on this machine until fixed on 2026-03-30.

---

## Root Causes Fixed

### 1. `rebuild-native-modules.sh` bad fallback (commit `e8b2f1a975`)

`detect_gateway_node()` fell back to `/opt/homebrew/bin/node` (Node 24) when
the gateway process was not running. Fixed by reading the launchd plist directly:

```bash
python3 -c "
import plistlib
with open('$HOME/Library/LaunchAgents/ai.smartclaw.gateway.plist', 'rb') as f:
    d = plistlib.load(f)
for a in d.get('ProgramArguments', []):
    if '/bin/node' in a: print(a); break
"
```

Priority order (never use Homebrew as fallback):
1. `OPENCLAW_GATEWAY_NODE` env override
2. Running gateway process (`pgrep` + `lsof`)
3. Gateway launchd plist (ground truth)
4. `~/.nvm/versions/node/v22.22.0/bin/node` (hardcoded nvm fallback)
5. PATH node (with warning log)

### 2. `mem0-native-module-watchdog.sh` missing load test

Watchdog compared `MODULE_VERSION` numbers only — never called `require()`. If
something rebuilt `better-sqlite3` for Node 24 while the stored baseline was still
`127`, the watchdog said "OK" while the module was broken.

Fixed by adding a `require()` load test inside the "versions match" branch:
```bash
if ! "$GATEWAY_NODE" -e "require('$BETTER_SQLITE3_DIR/node_modules/better-sqlite3')" 2>/dev/null; then
  # fall through to rebuild path even if MODULE_VERSION matches
fi
```

### 3. Removed Homebrew Node entirely (2026-03-30)

Eliminated the root cause by removing the second Node installation:

```bash
brew uninstall node
```

Homebrew-only packages (`verdaccio`, `pyright`, `wscat`, `clawhub`, `mcporter`,
`chrome-devtools-mcp`, `grok-mcp`) reinstalled under nvm Node 22 first.

---

## Canonical Node for This Machine

**Single Node: nvm v22.22.0**

```
~/.nvm/versions/node/v22.22.0/bin/node
~/.nvm/versions/node/v22.22.0/bin/npm
```

- Never install a second Node via Homebrew (`brew install node`)
- If an upgrade is needed, update the gateway plist after: `openclaw gateway install --force`
- All agent instruction files (CLAUDE.md, AGENTS.md, GEMINI.md, Cursor rules) document this preference

---

## `/up` Slash Command

Created `~/.claude/commands/up.md` — a slash command to update all agent instruction
files simultaneously:

```
/up <instruction>
```

Targets: `~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`, `~/.gemini/GEMINI.md`,
`~/.cursor/rules/env-preferences.mdc`

---

## Docker Container Setup

`docker/` in this repo contains a container-based openclaw setup:
- `Dockerfile` — `node:22-slim` base, installs openclaw
- `entrypoint.sh` — onboards on first run, patches openclaw.json, starts gateway on `0.0.0.0:18789`
- `patch.py` — surgical openclaw.json patcher (tokens, bind address)
- `docker-compose.yml` — service definition with healthcheck
- `.env.example` — template for `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `GATEWAY_TOKEN`

Key difference from native: `--gateway-bind 0.0.0.0` (Docker needs this; native uses loopback).

---

## Related Files

| File | Purpose |
|------|---------|
| `scripts/rebuild-native-modules.sh` | Detects + fixes native module ABI mismatch on gateway start |
| `scripts/mem0-native-module-watchdog.sh` | 4-hourly check; auto-rebuilds if `require()` fails |
| `scripts/health-check.sh` | Calls rebuild-native-modules on startup |
| `~/.smartclaw/.gateway-node-version` | Baseline MODULE_VERSION for watchdog |
