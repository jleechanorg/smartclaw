# Natural Language Task Dispatch

**Goal:** Say something short to openclaw and have the full pipeline execute correctly.

```
"build the discord bot we discussed"
  → jleechanclaw reads DM history, extracts spec
  → dispatches a bead-tracked mctrl task with full description
  → mctrl dispatches to minimax agent
  → agent builds /tmp/discord-eng-bot/openclaw.json
  → result posted back to Slack
```

---

## Current State (what openclaw already has)

openclaw.json already provides everything needed:

| Capability | Config key | Value |
|---|---|---|
| Read DM history | `channels.slack.dmHistoryLimit` | `50` |
| Memory search on every turn | `agents.defaults.memorySearch.enabled` | `true` |
| Session memory | `agents.defaults.memorySearch.experimental.sessionMemory` | `true` |
| Memory flush to MEMORY.md | `agents.defaults.compaction.memoryFlush.enabled` | `true` |

**Nothing new needs to be built.** The pipeline already works. The only missing piece is a SOUL.md instruction telling jleechanclaw *when* and *how* to use this history before dispatching.

---

## The Fix: SOUL.md Instruction (ORCH-7wk)

Add to SOUL.md under the long-running task dispatch section:

```markdown
### Context Expansion Before Dispatch

When a message references prior conversation — keywords: "we discussed",
"as discussed", "from earlier", "the X we talked about", "continue with" —
do NOT dispatch the raw short message. Instead:

1. Read the last 20 DM messages (dmHistoryLimit provides this automatically)
2. Extract the relevant spec: goal, requirements, constraints, output location
3. Write the full spec into the dispatched bead/mctrl task
4. Confirm to Jeffrey: "Queued: [task title]. Spec: [one-line summary]."

The coding agent only gets the expanded description — never the raw "we discussed" stub.
```

That's the entire implementation.

---

## Design Principles (ORCH-svp)

### Config-first, code-last

Before writing Python in `src/`, ask: can this be done in `openclaw-config/`?

| Want to change | Try first |
|---|---|
| jleechanclaw behavior / personality | `SOUL.md` |
| What tools jleechanclaw can use | `TOOLS.md` or `openclaw.json` tool allow/deny |
| How history/memory works | `openclaw.json` memorySearch / historyLimit / compaction |
| Agent identity / user context | `USER.md`, `identity/` |
| Cron / scheduled behavior | `cron/` config |
| New Python orchestration logic | `src/orchestration/` — **only if config cannot express it** |

Python code in `src/` is for capabilities that genuinely don't exist in openclaw's config surface. Everything else is config.

---

## Discord Bot Project (immediate)

Full spec for the minimax agent, ready to paste into a dispatched task:

```
Build an openclaw.json agent config for a public Discord engineering Q&A bot.

Agent profile: eng_qa
Sandbox: non-main, workspaceAccess: none
Tools allowed: web_search, web_fetch only
Tools blocked: read, write, edit, exec, process, browser, canvas, nodes, cron, gateway
Discord: message content + read/send/history only — no admin perms
Guild policy: channels.discord.groupPolicy = allowlist (use placeholder guild/channel IDs)
Slash commands: /docs, /status, /reset only
Anti-spam: mention-required, session TTL 10 minutes

Output: /tmp/discord-eng-bot/openclaw.json
Verify: cat /tmp/discord-eng-bot/openclaw.json | python3 -m json.tool && echo VALID
Post verification output when done.
```

---

## Beads

- `ORCH-w9e` — Feature: natural language task dispatch (parent)
- `ORCH-7wk` — Task: implement SOUL.md context expansion instruction
- `ORCH-svp` — Decision: config-first principle documented in CLAUDE.md + AGENTS.md
