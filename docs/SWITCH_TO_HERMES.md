# Hermes Agent — Primary AI Agent

**Hermes Agent** (Nous Research) is the primary AI agent. OpenClaw is **disabled** unless explicitly re-enabled.

## Architecture

| | Hermes Staging | Hermes Prod |
|---|---|---|
| **Directory** | `~/.hermes/` (git repo) | `~/.hermes_prod/` |
| **Launchd** | `ai.hermes-staging` | `ai.hermes.prod` |
| **Slack bot** | Staging app (`xoxb-...roQR...`) | Prod app (`xoxb-...L1ZG...`) |
| **Model** | `minimax-portal/MiniMax-M2.7` | `minimax-portal/MiniMax-M2.7` |
| **HERMES_HOME** | `~/.hermes/` | `~/.hermes_prod/` |
| **Tokens** | Staging Slack | Prod Slack |

**Directory structure:**
```
~/.hermes/          ← git repo root (jleechanclaw), Hermes staging
~/.openclaw/        ← symlink → ~/.hermes/ (backward compat)
~/.hermes_prod/     ← Hermes prod (separate runtime data)
```

**OpenClaw is disabled** — set `OPENCLAW_ENABLED=1` in the monitor to re-enable AO checks.

## Quick Start

### Run Hermes Monitor

```bash
bash ~/.openclaw/scripts/hermes-monitor.sh
```

### Check Status

```bash
hermes status                        # staging
HERMES_HOME=~/.hermes_prod hermes status   # prod
```

### Start/Stop Gateways

```bash
launchctl start gui/$(id -u)/ai.hermes-staging   # start staging
launchctl stop gui/$(id -u)/ai.hermes-staging    # stop staging
launchctl start gui/$(id -u)/ai.hermes.prod     # start prod
launchctl stop gui/$(id -u)/ai.hermes.prod      # stop prod
```

Or manually:
```bash
HERMES_HOME=~/.hermes hermes gateway run        # staging (foreground)
HERMES_HOME=~/.hermes_prod hermes gateway run  # prod (foreground)
```

### Restart a Gateway

```bash
launchctl kickstart -kp gui/$(id -u)/ai.hermes-staging
launchctl kickstart -kp gui/$(id -u)/ai.hermes.prod
```

## Gateway Details

| | Hermes Staging | Hermes Prod |
|---|---|---|
| **Launchd label** | `ai.hermes-staging` | `ai.hermes.prod` |
| **Slack tokens** | Staging | Prod |
| **Memory** | `~/.hermes/memories/` | `~/.hermes_prod/memories/` |
| **Sessions** | `~/.hermes/sessions/` | `~/.hermes_prod/sessions/` |
| **Skills** | `~/.hermes/skills/` | `~/.hermes_prod/skills/` |

## Configuration Files

### Staging `.env` (`~/.hermes/.env`)

```bash
HERMES_ENABLED=true
HERMES_ENV=staging
HERMES_HOME=/Users/jleechan/.hermes

# Slack — STAGING tokens
SLACK_BOT_TOKEN=&lt;SLACK_BOT_TOKEN&gt;
SLACK_APP_TOKEN=&lt;SLACK_APP_TOKEN&gt;

OPENCLAW_STATE_DIR=/Users/jleechan/.openclaw/
OPENCLAW_CONFIG_PATH=/Users/jleechan/.openclaw/openclaw.json
GATEWAY_ALLOW_ALL_USERS=true
```

### Prod `.env` (`~/.hermes_prod/.env`)

```bash
HERMES_ENABLED=true
HERMES_ENV=prod
HERMES_HOME=/Users/jleechan/.hermes_prod

# Slack — PROD tokens
SLACK_BOT_TOKEN=&lt;SLACK_BOT_TOKEN&gt;
SLACK_APP_TOKEN=&lt;SLACK_APP_TOKEN&gt;

OPENCLAW_STATE_DIR=/Users/jleechan/.openclaw_prod/
OPENCLAW_CONFIG_PATH=/Users/jleechan/.openclaw_prod/openclaw.json
GATEWAY_ALLOW_ALL_USERS=true
```

## Known Issues

### Discord/Telegram token conflicts

Both Hermes instances share the same `auth.json` for Discord/Telegram, causing "token already in use" warnings. **Non-critical** — Slack works correctly on both since they use separate Slack apps/tokens.

## Troubleshooting

### Gateway won't start

```bash
hermes gateway status                    # staging
HERMES_HOME=~/.hermes_prod hermes gateway status  # prod
hermes doctor
cat ~/.hermes/logs/gateway.log         # staging
cat ~/.hermes_prod/logs/gateway.log    # prod
```

### Slack not responding

```bash
hermes status                        # check Slack ✓
# Verify tokens:
rg 'SLACK_BOT_TOKEN' ~/.hermes/.env        # staging
rg 'SLACK_BOT_TOKEN' ~/.hermes_prod/.env  # prod
```

### Re-enable OpenClaw (AO path)

OpenClaw AO is currently disabled. To re-enable:

```bash
OPENCLAW_ENABLED=1 bash ~/.openclaw/scripts/hermes-monitor.sh
```

Launchd services to load:
```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.openclaw.gateway.plist
launchctl start gui/$(id -u)/ai.openclaw.gateway
```
