# TOOLS.md - Local Notes

Skills define _how_ tools work. This file is for _your_ specifics — the stuff that's unique to your setup.

## MCP Servers

### Active MCPs
- **mcp_mail** - Agent coordination via mail-like messages
- **chrome-superpower** - Browser automation
- **second-opinion-tool** - Multi-model AI consultation
- **context7** - Documentation lookup
- **beads** - Task tracking (requires bd CLI install)
- **openclaw** - OpenClaw agent control

### Extensions
- `voice-call` - Voice call functionality

## Project Context [CRITICAL]

Projects requiring special handling:
- **beads** (jleechanorg/beads) - Task tracking system, uses beads CLI
- **ai_universe** (jleechanorg/ai_universe) - MCP backend with Firebase + Cerebras
- **worldarchitect.ai** (jleechanorg/worldarchitect.ai) - Main project, AI RPG, 6k+ commits

## Common Commands

| Context | Command | Notes |
|---------|---------|-------|
| openclaw dev | `pnpm openclaw ...` | Use bun or pnpm |
| worldarchitect.ai | `pnpm build`, `pnpm test` | Full test suite |
| Claude Code | `claude --dangerously-skip-permissions` | Use sparingly |

## OpenClaw Scheduling Guardrail

- Forbidden: system `crontab` changes for OpenClaw reminder/scheduling/automation jobs.
- Required: OpenClaw gateway cron subcommands only (`openclaw cron ...`).
- First command to use: `openclaw cron --help`

## Infrastructure

### Exe.dev VMs
- Access via `ssh exe.dev` then `ssh <vm-name>`
- Gateway: `openclaw gateway run --bind loopback --port 18789 --force`

### Credentials
- Stored in: `~/.openclaw/credentials/`
- Re-run `openclaw login` if logged out

## Failure Modes

| Tool | Common Issue | Fix |
|------|--------------|-----|
| openclaw gateway | Won't start | Check port, kill old process |
| MCP servers | Not responding | Restart Claude Code |
| Tests | Flaky | Check for pre-existing failures |

## Restart Catch-up Playbook

- After any gateway recovery/restart, immediately run Slack MCP catch-up for unanswered mentions/threads and post acknowledgements before deeper processing.
- Use `mcporter call slack.conversations_history ...` + `mcporter call slack.conversations_replies ...` to identify and process missed items.

## Why Separate?

Skills are shared. Your setup is yours. Keeping them apart means you can update skills without losing your notes, and share skills without leaking your infrastructure.

---

Add whatever helps you do your job. This is your cheat sheet.
