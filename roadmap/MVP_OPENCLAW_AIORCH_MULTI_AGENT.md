# MVP v1 Design: OpenClaw -> mctrl -> ai_orch (Multi-Agent, Multi-Worktree, Bead-Native)

Date: 2026-03-05 (updated 2026-03-07)

## Scope

First shipping MVP is:

- `OpenClaw` as orchestrator
- `mctrl` as the dispatch/supervision/notifier layer
- direct `ai_orch` execution underneath `mctrl` (multiple concurrent agents)
- one isolated git worktree per task
- beads as the canonical task system for all task lifecycle state
- no TaskPoller in the start path

OSS Mission Control is out of the target architecture.

## Hard Rules

1. Multi-agent + multi-worktree is default behavior.
2. Every task MUST be a bead (`ORCH-*` or repo issue prefix); no untracked tasks.
3. Start path is `OpenClaw -> mctrl -> ai_orch`; TaskPoller must not start tasks.
4. OpenClaw is notified for both terminal outcomes:
   - `task_finished`
   - `task_needs_human`

## Minimal Architecture

```text
OpenClaw (planner/orchestrator)
  -> beads (task source of truth)
  -> mctrl (dispatch + supervisor + reconciliation + notifier)
  -> ai_orch (spawn/send/kill/list)
```

## Full User-Visible Flow

```
1. jleechan messages OpenClaw in Slack (#ai-slack-test or DM)
2. OpenClaw acks in thread ("on it, spawning agent for ORCH-xxx")
3. OpenClaw: br create/claim → `mctrl` dispatch → `ai_orch` spawn → write BeadSessionMapping (with slack_trigger_ts)
4. Agent works in isolated worktree+session
5. mctrl supervisor detects session end → commit check → task_finished or task_needs_human
6. notify_slack_done: DM to jleechan + thread reply under original Slack message (step 1)
7. OpenClaw receives MCP Agent Mail notification
```

## Dispatch Contract (Bead-Native)

For each dispatched task, OpenClaw must persist:

```python
BeadSessionMapping(
    bead_id="ORCH-xxx",
    session_name="ai-claude-xxxxxx",
    worktree_path="/tmp/ai-orch-xxx/...",
    branch="feat/orch-xxx",
    agent_cli="claude",           # or codex
    status="in_progress",
    start_sha="<HEAD sha at spawn time>",   # for accurate commit detection
    slack_trigger_ts="1772857900.668299",    # ts of jleechan's Slack message → threads reply
)
```

Canonical state is always bead status + notes.

## Supervisor Loop (MVP)

Loop over active bead/session mappings (runs via launchd cron):

1. Check tmux/session liveness.
2. If session alive → continue.
3. If session gone:
   - `git log start_sha..HEAD` — any commits? → `task_finished`
   - no commits → `task_needs_human`
4. Update bead status + write notes.
5. Emit notifications in parallel:
   - MCP Agent Mail → OpenClaw agent thread
   - Slack DM to jleechan
   - Slack thread reply under slack_trigger_ts in #ai-slack-test

Future (post-MVP): step 2 also checks PR presence, CI status, review blockers before deciding continue/retry.

## OpenClaw Notification Contract

### Primary channel

MCP Agent Mail message to OpenClaw agent thread:

```json
{
  "event": "task_finished | task_needs_human",
  "bead_id": "ORCH-123",
  "session": "ai-claude-abc",
  "branch": "feat/orch-123",
  "worktree_path": "/tmp/...",
  "summary": "short reason",
  "action_required": "review_and_merge | human_decision",
  "slack_trigger_ts": "1772857900.668299"
}
```

### Fallback channel

If MCP mail is unavailable, append JSONL to `.messages/outbox.jsonl` and drain later.

### Slack channel

`notify_slack_done` in `src/orchestration/openclaw_notifier.py`:
- DM to jleechan (`$SMARTCLAW_DM_CHANNEL`)
- Thread reply in `#ai-slack-test` (`$SMARTCLAW_TRIGGER_CHANNEL`) using `thread_ts=slack_trigger_ts`
- Token: `SLACK_BOT_TOKEN` from `~/.openclaw/set-slack-env.sh` (xoxb-...)

## Slack Token Setup

```bash
source ~/.profile       # SLACK_USER_TOKEN (xoxp-...) — posts as jleechan
source ~/.bashrc        # OPENCLAW_SLACK_BOT_TOKEN (xoxb-...) — posts as openclaw bot
# OR
source ~/.openclaw/set-slack-env.sh  # SLACK_BOT_TOKEN = same bot token
```

## Mission Control Position

Mission Control is not part of the supported runtime path for this MVP:

- no lifecycle authority
- no task start authority
- no canonical state writes
- no required mirror path

## TaskPoller Requirement

TaskPoller may remain as legacy code temporarily, but:

- it must not be used to start tasks in MVP
- startup wiring for task start must be removed
- all starts must come from direct OpenClaw dispatch path

## MVP Bead Roadmap

Epic:

- `ORCH-g8c` MVP v1: OpenClaw -> mctrl -> ai_orch multi-agent multi-worktree (bead-native)

Core tasks and status:

| Bead | Status | Description |
|------|--------|-------------|
| ORCH-g8c.1 | CLOSED | Bead-native task lifecycle + session mapping |
| ORCH-g8c.2 | CLOSED | OpenClaw loopback notifier (task_finished/task_needs_human) + Slack threading |
| ORCH-g8c.3 | IN_PROGRESS | Persistent supervisor loop (launchd cron) + OpenClaw dispatch skill |
| ORCH-g8c.4 | IN_PROGRESS | MVP E2E: Slack loopback proven; N-bead multi-worktree not yet |
| ORCH-7sy | OPEN | Gateway-direct dispatch (eliminate TaskPoller) |

## What's Left for Full Loop (ORCH-g8c.3)

1. **OpenClaw dispatch skill** (`openclaw-config/skills/dispatch.md` or SOUL.md instruction):
   - When OpenClaw takes a task: `br claim ORCH-xxx` → `ai_orch spawn` → write BeadSessionMapping with `slack_trigger_ts` from the Slack message `ts`

2. **Persistent supervisor** (`launchd` plist or cron):
   - Runs `PYTHONPATH=src python -m orchestration.supervisor` every 30s
   - Calls `reconcile_registry_once` with real registry/outbox paths

## MVP Acceptance Criteria

1. jleechan asks in Slack → OpenClaw acks in thread.
2. OpenClaw dispatches agent → BeadSessionMapping written with `slack_trigger_ts`.
3. Supervisor detects completion → thread reply lands under original Slack message.
4. Bead states move correctly (`open` -> `in_progress` -> `closed` or human-blocked note).
5. OpenClaw receives `task_finished` and `task_needs_human` MCP mail events.
6. If MCP mail is down, events queue in `.messages/outbox.jsonl` and drain successfully.
7. Dispatch 3+ beads concurrently, each in isolated worktree/session.
