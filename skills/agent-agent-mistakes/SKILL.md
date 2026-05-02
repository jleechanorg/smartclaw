---
name: agent-agent-mistakes
description: "Build agent memory patterns that prevent the same mistakes — using session replay, error tracking, and a self-correcting knowledge base."
when_to_use: "Use when the user wants to stop their AI agent from making the same mistakes repeatedly. Examples: 'make agents learn from mistakes', 'build error memory for agents', 'agent self-correction system', 'stop agents repeating errors', 'agent mistake tracker', 'build a memory that prevents agent errors'"
arguments:
  - agent_name
  - error_sources
argument-hint: "[agent_name] [error_sources]"
context: inline
---

# Agent-Agent Mistakes — Stop Agents Repeating Errors

Build a self-correcting memory system so your agent never makes the same mistake twice. Based on Garry Tan's framework for agent error prevention.

## Goal

Agents accumulate error patterns, learn from session failures, and consult a mistake memory before repeating actions.

## Inputs

- `$agent_name`: Name of the agent to instrument (e.g. "hermes", "ao-primary")
- `$error_sources`: Comma-separated sources to monitor for errors (e.g. "session_replays, cron_outputs, slack_threads")

## Steps

### 1. Set Up Error Capture

Create a memory file at `memory/agent-errors.md` tracking all agent mistakes.

**Success criteria**: `memory/agent-errors.md` exists and is git-tracked.

### 2. Instrument Session Replay

After every agent session, extract error patterns:
- Failed tool calls (timeout, permission denied, not found)
- User corrections (things the agent had to be steered on)
- Context-ceiling spirals (long sessions that produced no output)
- Fabricated responses (confabulated IDs, fake confirmations)

**Success criteria**: Session logs are being reviewed and patterns extracted.

### 3. Build the Error Memory

For each unique error pattern, add an entry to `memory/agent-errors.md`:

```markdown
## Error: [short name]

**First seen**: YYYY-MM-DD
**Occurrences**: N
**Root cause**: What the agent got wrong
**Prevention**: What the agent should do instead
**Trigger phrase**: Phrases that indicate this error is happening again
```

**Success criteria**: At least 3 error patterns documented.

### 4. Create a Pre-Action Check

Add a check at the start of every agent session: before taking major actions, consult `memory/agent-errors.md` to see if this mistake was made before.

**Success criteria**: Agent reads error memory before high-stakes actions.

### 5. Track Error Recurrence

If an error from the memory is repeated, increment its occurrence count and add a new timeline entry.

**Success criteria**: `memory/agent-errors.md` shows decreasing recurrence for documented errors.
