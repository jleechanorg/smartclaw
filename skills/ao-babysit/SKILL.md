---
name: ao-babysit
description: "Babysit an active AO worker tmux session — poll it, detect stalls/deaths, steer it back on track, post progress to Slack. Use after dispatching an AO worker to ensure it stays alive and produces a PR."
when_to_use: "After dispatching an AO worker (ao spawn + ao send) when the task is complex or the worker has a history of dying silently. The babysit skill actively monitors the tmux session, reads terminal output, sends corrective nudges when the agent goes off-track, and posts periodic progress to Slack."
arguments:
  - session_id
  - slack_channel
  - slack_thread_ts
  - task_summary
argument-hint: "[session_id] [slack_channel] [slack_thread_ts] [task_summary]"
context: inline
---

# ao-babysit — AO Worker Babysitter

## What it does

After an AO worker is dispatched, this skill polls its tmux session every 5 minutes to:
1. **Detect if it's alive** — tmux session exists + terminal has recent activity
2. **Detect if it's stuck** — same output repeated, looping, or idle too long
3. **Detect if it's dead** — tmux session gone or completely idle
4. **Steer it back** — send corrective messages via `ao send` when off-track
5. **Post progress** — in-thread Slack updates every ~5 min with terminal status
6. **Escalate** — if the worker dies, report what happened and suggest next steps

## Key failure modes this catches

| Failure | Detection | Response |
|---------|-----------|----------|
| Worker dies silently | tmux session gone | Post in Slack: worker died, what it completed, what to do next |
| Context compaction kills session | tmux exists but terminal shows compaction message | Detect and escalate immediately |
| Agent loops forever on one step | repeated same command/output in terminal | Send nudge: "stop searching, just [concrete next action]" |
| Agent waiting for human input | terminal shows prompt waiting | Detect and escalate to human |
| Agent goes off-track | terminal shows unrelated work | Send correction via `ao send` |
| Agent idle too long (>10min no output) | no new terminal output | Send status-check nudge |
| Agent produces PR | gh command or PR URL in terminal | Report PR URL in Slack |

## tmux session naming

AO workers create tmux sessions named by their session ID. The mapping:
- Session `ao-4250` → tmux session `9cc70dbf8ac4-ao-4250` (the prefix is the AO server UUID)
- Find sessions: `tmux list-sessions | grep ao-` or `tmux list-sessions | grep <session-id>`

## Script interface

```bash
# Poll once and report (for cron use)
python3 ~/.hermes/skills/ao-babysit/scripts/babysit.py poll \
  --session ao-4250 \
  --slack-channel ${SLACK_CHANNEL_ID} \
  --slack-thread-ts 1776524900.599649 \
  --task-summary "Skeptic goals/tenets proof gate implementation"

# Run continuous babysitter loop (for manual use)
python3 ~/.hermes/skills/ao-babysit/scripts/babysit.py babysit \
  --session ao-4250 \
  --slack-channel ${SLACK_CHANNEL_ID} \
  --slack-thread-ts 1776524900.599649 \
  --task-summary "Skeptic goals/tenets proof gate implementation"
```

## Babysitter loop behavior

The `babysit` command runs continuously, polling every 5 minutes:
1. Check tmux session alive
2. Read terminal scrollback (last 50 lines)
3. Analyze for failure modes above
4. If stuck/off-track → send corrective ao send
5. If alive + working → post brief progress to Slack
6. If dead → post death report + suggestions
7. If PR detected → post PR URL and exit

## Anti-patterns this prevents

- **Parent session context compaction before worker produces output** — the babysitter runs in a separate process (cron or subprocess), not in the parent session
- **Silent death** — every poll checks session alive; dead sessions are reported immediately
- **Context drift** — terminal is read on every poll; off-track work is corrected before it compounds

## When to use babysit vs cron

- **babysit loop**: When you want Hermes to actively steer the worker (send nudges, corrective instructions)
- **Cron poll**: When you just want status reports and to know when it's done

Both share the same `poll` logic. The babysit loop adds active steering.

## Status to post in Slack

Format for progress updates (every ~5 min):
```
[ao-4250] Working — [what it's currently doing based on terminal]
Last activity: [timestamp]
```

Format for completion:
```
[a] ao-4250 done — [PR URL or summary of what was produced]
```

Format for death:
```
[alarm] ao-4250 died — [what it was doing when it died, last terminal lines]
Next: [suggestion: restart, manual intervention, etc.]
```

## Nudge patterns

When sending corrective nudges via `ao send`, always:
1. Identify what specifically is wrong (quote terminal output)
2. Give ONE concrete next action
3. Say "I'm Hermes, an AI agent — not a human" per terminal-agent-communication skill
4. Use ao send for tmux-based workers (not cmux send)

## Required environment

- `tmux` available
- `ao` CLI on PATH
- `SLACK_TRIGGER_TS` and `SLACK_TRIGGER_CHANNEL` for threading
- Write access to `~/.hermes/skills/ao-babysit/state/` for poll state
