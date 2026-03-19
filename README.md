# SmartClaw

**⚠️ WORK IN PROGRESS - Prototype**

This is an experimental reference implementation of the OpenClaw autonomous orchestrator system. It is not yet fully functional.

## What is SmartClaw?

SmartClaw is a lightweight autonomous agent orchestrator that handles day-to-day development tasks across jleechanorg projects. It integrates with Claude Code, GitHub, and Slack to provide:

- Automated PR creation and management
- Session-based agent orchestration
- Health monitoring and alerting
- Backup automation

## SmartClaw vs Agent-Orchestrator

| Feature | SmartClaw (this repo) | Agent-Orchestrator (jleechanclaw) |
|---------|----------------------|-----------------------------------|
| Status | Prototype / WIP | Production |
| Scope | Reference implementation | Full production system |
| Complexity | Minimal | Complete |
| Stability | Experimental | Hardened |

**SmartClaw is a stripped-down reference** for understanding the OpenClaw architecture. For production use, refer to [jleechanclaw](https://github.com/jleechanorg/jleechanclaw).

## Dependencies

### Required

- **Python 3.11+** - Runtime environment
- **Git** - Version control
- **rsync** - Backup operations

### Optional

- **Slack** - Notifications and alerts (requires tokens)
- **Claude Code** - AI coding assistant
- **GitHub CLI (gh)** - PR operations

## Installation

```bash
# Clone the repository
git clone https://github.com/jleechanorg/smartclaw.git
cd smartclaw

# Run the installation script
./install.sh
```

## Setup

### Environment Variables

Create a `.env` file or set these in your shell:

```bash
# Slack Configuration
export OPENCLAW_SLACK_BOT_TOKEN="xoxb-..."
export OPENCLAW_SLACK_APP_TOKEN="xapp-..."
export SLACK_USER_TOKEN="xoxp-..."

# Slack Identity
export JLEECHAN_SLACK_USER_ID="U09GH5BR3QU"
export OPENCLAW_BOT_USER_ID="U0AEZC7RX1Q"

# Slack Channels
export AGENTO_CHANNEL="C0AJQ5M0A0Y"
export SLACK_TEST_CHANNEL="C0AKALZ4CKW"
export JLEECHAN_DM_CHANNEL="D0AFTLEJGJU"

# Gateway
export OPENCLAW_URL="http://127.0.0.1:18789"
export OPENCLAW_GATEWAY_TOKEN="<token>"

# Agent Orchestrator
export OPENCLAW_AO_HOOK_TOKEN="<token>"
```

### Finding Slack IDs

To find channel/user IDs in Slack:
1. Open the channel or conversation
2. Right-click → "Copy link"
3. The ID is the last segment (starts with `C` for channels, `D` for DMs, `U` for users)

## Safety & Security

### ⚠️ Important Warnings

1. **Never commit secrets** - Use environment variables, never hardcode tokens in code
2. **Audit tokens regularly** - Rotate Slack and API tokens periodically
3. **Restrict permissions** - Use minimal required scopes for Slack/GitHub tokens
4. **Monitor activity** - Check logs for unexpected behavior

### Token Security

| Token Type | Risk Level | Recommendation |
|------------|------------|----------------|
| Slack Bot Token (`xoxb-...`) | High | Keep private, rotate monthly |
| Slack User Token (`xoxp-...`) | High | Keep private, never commit |
| GitHub Token | High | Use fine-grained tokens with minimal scope |
| Gateway Token | Medium | Internal only, not exposed externally |

### Backup Security

Backups automatically redact sensitive data:
- API keys and tokens
- Environment variables
- Credential files

Always verify backup integrity before restoring.

## Development

```bash
# Run tests
./scripts/test.sh

# Check code quality
./scripts/lint.sh
```

## License

MIT License - See [LICENSE](./LICENSE) for details.

## Related Projects

- [jleechanclaw](https://github.com/jleechanorg/jleechanclaw) - Production Agent Orchestrator
- [worldarchitect.ai](https://github.com/jleechanorg/worldarchitect.ai) - D&D 5e AI Platform
