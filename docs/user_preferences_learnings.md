# User Preferences & Patterns Learnings

> Analysis of conversation history for jleechan

## Overview

This document captures user preferences and patterns extracted from available conversation history.

**Note:** No Claude Code or Cursor conversation history was found in the home directory (~). The analysis is based on OpenClaw session data from `~/.openclaw/backups/` covering February 2026.

---

## Communication Preferences

| Preference | Value |
|------------|-------|
| **Primary Channel** | WhatsApp |
| **Tone** | Straight to business, direct and practical |
| **Response Style** | Terse by default |
| **Signature Emoji** | 🦾 |

### Evidence

From onboarding session (2026-02-10):
- User chose "1. genesis coder 2.claw" for identity setup
- When asked about tone preference, responded: "straight to business"
- When asked about response style: "1. terse"
- When asked about emoji: kept 🦾

---

## User Profile

| Field | Value |
|-------|-------|
| **Name** | Genesis Coder |
| **Timezone** | America/Los_Angeles |
| **Preferred Channel** | WhatsApp (mainly WhatsApp) |

---

## Technical Patterns

### AI Models Used
- **OpenAI Codex** (`gpt-5.3-codex-spark`)
- **MiniMax-M2.5** (via minimax-portal)

### Thinking Level
- Prefers "low" thinking level for efficiency

### Workflow Patterns
- Uses **cron jobs** for recurring reminders (e.g., "review-p-every-10m")
- Active **GitHub PR workflow** - frequently reviews and addresses PR comments
- Uses **Slack** for communication
- Tests **E2E for OpenClaw** (character creation flows)

### GitHub Activity
- Works on `jleechanorg/worldarchitect.ai` and `jleechanorg/jleechanclaw`
- Manages many PRs (5500+ range)
- Recent focus: BYOK (Bring Your Own Key) implementation, MCP smoke tests

---

## Interaction Patterns

### Command Style
- Prefers numbered menu responses (e.g., "1", "2", "3")
- Minimal messages - gets straight to the point
- Uses short confirmations like "terse", "keep"

### Example Interactions
```
Agent: What do you want first: 1) set up daily check-in, 2) create reminder, 3) help with task?
User: 1

Agent: Pick the daily check-in schedule: 1) Time, 2) Days, 3) What to include
User: 2

Agent: Reply like: "9am weekdays; calendar + priorities"
User: 1. terse 2. keep
```

---

## Constraints & Boundaries

Based on OpenClaw SOUL.md guidance:
- No filler words ("Great question!", "I'd be happy to help!")
- Be genuinely helpful, not performatively helpful
- Have opinions
- Earn trust through competence
- Remember being a guest in user's space
- Private things stay private
- When in doubt, ask before acting externally
- Never send half-baked replies to messaging surfaces

---

## Key Takeaways

1. **Efficiency-first**: User values terse, direct communication
2. **Multi-channel**: WhatsApp primary, Slack secondary
3. **Active developer**: Heavy GitHub/PR workflow involvement
4. **Automation lover**: Uses cron jobs for reminders
5. **Minimalist**: Prefers numbered responses over freeform

---

## Data Sources

- OpenClaw session backups: `~/.openclaw/backups/20260219_120541/agents/main/sessions/`
- Date range: February 10-19, 2026
- Session types: Onboarding, cron reminders, GitHub PR reviews, E2E testing
