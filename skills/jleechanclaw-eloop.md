---
name: smartclaw-eloop
description: Custom evolve loop for smartclaw orchestrator — drains dropped Slack thread backlog via /claw, fixes openclaw issues, proposes new work items. Max 50 items, newest-first.
type: skill
---

# smartclaw Custom Eloop

**Canonical paths:** repository `skills/smartclaw-eloop.md` (this file); after `scripts/bootstrap.sh`, also `~/.smartclaw/skills/smartclaw-eloop.md` (symlink). Claude Code entry: `.claude/skills/smartclaw-eloop/SKILL.md` (pointer to this file).

## Purpose

This is the evolve loop for the smartclaw AO orchestrator. On each poll cycle:

1. **Drain the dropped-thread backlog** — find Slack threads that got no response from OpenClaw, dispatch each as a `/claw` work item (newest first, max 50 total)
2. **Fix openclaw issues** — check for system health problems and dispatch fixes
3. **Propose new work items** — create beads for patterns observed

## State file

Persist loop state at `~/.smartclaw/workspace/claw-backlog-progress.json`:

```json
{
  "dispatched": {"<thread_ts>": "<bead_id>"},
  "abandoned": ["<thread_ts>"],
  "processedCount": 0,
  "lastRunAt": "<ISO timestamp>"
}
```

Load this file at the start of each cycle. Create it if missing.

## Phase 1: Load state + check completion

```bash
STATE_FILE="$HOME/.smartclaw/workspace/claw-backlog-progress.json"
python3 -c "
import json, os
state = {'dispatched': {}, 'abandoned': [], 'processedCount': 0, 'lastRunAt': ''}
if os.path.exists('$STATE_FILE'):
    state.update(json.load(open('$STATE_FILE')))
# Persist so Phase 6 can load even on first run
with open('$STATE_FILE', 'w') as f:
    json.dump(state, f)
print(json.dumps(state))
"
```

For each `<thread_ts>` in `dispatched`, check if its bead is closed/abandoned:
- `br show <bead_id>` — if status is `closed` or `abandoned`:
  1. Increment processedCount (it has been processed)
  2. Remove from `dispatched` dict
  3. Save updated state immediately:
  ```bash
  python3 -c "
  import json, subprocess
  state = json.load(open('$STATE_FILE'))
  to_remove = []
  for ts, bead_id in state['dispatched'].items():
      result = subprocess.run(['br', 'show', bead_id], capture_output=True, text=True)
      status = result.stdout.lower()
      if 'closed' in status or 'abandoned' in status:
          state['processedCount'] = state.get('processedCount', 0) + 1
          to_remove.append(ts)
  for ts in to_remove:
      del state['dispatched'][ts]
  with open('$STATE_FILE', 'w') as f:
      json.dump(state, f)
  print(json.dumps(state))
  "
  ```

## Phase 2: Find dropped Slack threads

Search the openclaw Slack channels for threads that Jeffrey or other humans posted but that received NO reply from the OpenClaw bot. These are "dropped threads."

**Channels to search** (in priority order, newest first within each):
1. `C0AJ3SD5C79` — openclaw design/ops channel (primary)
2. `${SLACK_CHANNEL_ID}` — #ai-slack-test (openclaw bot IS present here — check for its replies)
3. `${SLACK_CHANNEL_ID}` — #all-jleechan-ai (openclaw bot is NOT in this channel — skip bot-reply check; threads here are informational only)

**Method**: Use `mcp__slack__conversations_history` on each channel. For each thread:
- Get messages in the thread: `mcp__slack__conversations_replies`
- A thread is "dropped" if: started by jleechan (human, not bot), and NO reply from the OpenClaw bot user (`$OPENCLAW_BOT_USER_ID`) exists in the thread

**Filter out**:
- Threads older than 30 days
- Threads already in `dispatched` or `abandoned`
- Threads that appear to be bot-generated (starts with "[AI Terminal:", "[agento]", etc.)
- Threads already answered (has openclaw bot reply)

**Sort**: Newest `ts` first (reverse chronological)

**Limit**: Stop collecting when total would exceed 50 items.

## Phase 3: Dispatch backlog items

For each dropped thread (newest first, stopping at `processedCount >= 50`):

1. **Read the thread content** to understand what Jeffrey was asking
2. **Classify**:
   - If it's a task/request → dispatch via `/claw`: create a bead, spawn a jc session
   - If it's a question/comment with no clear work → mark `abandoned` with reason
   - If it's already handled by an existing open PR or bead → mark `abandoned` with reference

3. **For dispatch**: Create a bead, then dispatch:
   ```bash
   BEAD_OUTPUT=$(br create "Process dropped Slack thread: <summary>" --type task --priority 2)
   BEAD_ID=$(echo "$BEAD_OUTPUT" | grep -oEi 'orch-[a-z0-9]+' | head -1)
   if [[ -z "$BEAD_ID" ]]; then echo "ERROR: could not parse bead ID from: $BEAD_OUTPUT"; return 1; fi
   # Write task description to tmp file
   cat > /tmp/claw-backlog-${BEAD_ID}.txt <<'TASK'
   <full thread content and context>
   TASK
   SPAWN_OUTPUT=$(ao spawn "$BEAD_ID" -p smartclaw 2>&1)
   SESSION_NAME=$(echo "$SPAWN_OUTPUT" | grep -oEi 'jc-[0-9]+' | tail -1)
   if [[ -z "$SESSION_NAME" ]]; then echo "ERROR: could not parse session name from spawn output: $SPAWN_OUTPUT"; return 1; fi
   ao send "$SESSION_NAME" --file /tmp/claw-backlog-${BEAD_ID}.txt
   ```

4. **Update state**: Add `thread_ts → bead_id` to `dispatched`, increment `processedCount`:
  ```bash
  python3 -c "
  import json
  state = json.load(open('$STATE_FILE'))
  state['dispatched']['$THREAD_TS'] = '$BEAD_ID'
  state['processedCount'] = state.get('processedCount', 0) + 1
  with open('$STATE_FILE', 'w') as f:
      json.dump(state, f)
  "

5. **Respect concurrency**: Don't dispatch more than 3 new sessions per cycle (lifecycle-worker will queue the rest). Check active sessions only (exclude dead/stopped): `ao session ls -p smartclaw 2>/dev/null | grep -E '^\s*jc-[0-9]+\s' | grep -vE 'dead|stopped|done' | wc -l` — if result >= 5, skip dispatching this cycle.

## Phase 4: Fix openclaw issues

After dispatching threads, run a quick health check:

```bash
bash ~/.smartclaw/scripts/staging-canary.sh --port 18810; RC=$?
if [[ "$RC" -ne 0 ]]; then
  echo "STAGING CANARY FAILED (rc=$RC)"
  # Fix or dispatch a jc session to remediate
fi
```

If any check FAILS:
- Log the failure
- Determine if it's fixable autonomously (config-edit scope) or needs a jc session (claw-dispatch)
- For config issues: fix via `openclaw.json` surgical edit
- For code issues: dispatch a jc session with `/claw fix: <issue description>`

**Common openclaw issues to check proactively**:
- Multiple gateway processes: `pgrep -x openclaw-gateway | wc -l != 1`
- Stale session locks: check `~/.smartclaw_prod/agents/main/sessions/*.lock`
- WS pong failures in logs: `tail -5 ~/.smartclaw_prod/logs/gateway.err.log | grep pong`
- Session lock silent failure: `tail -5 ~/.smartclaw_prod/logs/gateway.err.log | grep "session file locked"`

## Phase 5: Propose new work items

After the main dispatch loop, scan for patterns and create new beads:

1. **Recurring openclaw failures** (2+ in logs today): Create a bead for systemic fix
2. **Stale PRs** (open > 3 days, no CI activity): Create a bead to investigate and close
3. **Outdated docs** (if `docs/context/SYSTEM_SNAPSHOT.md` is > 7 days old): Create a bead to refresh
4. **Unanswered MCP mail** (from `memory/mcp-mail-ack-log.md` entries with `action_needed=yes` > 24h old): Create beads

Use `br create "..." --type task --priority 2` for each. Don't create duplicates — check `br list --status open` first and grep for a unique keyword from the proposed title.

## Phase 6: Update state and recap

```bash
STATE_FILE="$HOME/.smartclaw/workspace/claw-backlog-progress.json"
python3 -c "
import json, datetime
state = json.load(open('$STATE_FILE'))
state['lastRunAt'] = datetime.datetime.utcnow().isoformat() + 'Z'
with open('$STATE_FILE', 'w') as f:
    json.dump(state, f, indent=2)
"
```

Post a brief recap to MCP mail:
```
subject: "eloop cycle: <N> dispatched, <M> remaining, <K> fixes"
body: summary of what was dispatched, what issues found, what beads created
```

## Loop termination

Stop the loop (set processedCount to 50 to suppress future dispatches) when:
- `processedCount >= 50`
- No more dropped threads found across all channels
- All channels return empty results 3 cycles in a row

When terminated, post to MCP mail: `"Backlog eloop complete: processed <N> items, <M> in PR, <K> abandoned"`
