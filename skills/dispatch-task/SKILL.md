---
name: dispatch-task
version: 1.0.0
description: Dispatch a bead-tracked task to an ai_orch agent session, register the mapping, and ack in Slack thread.
---

# dispatch-task

Use this skill when jleechan asks you to work on a task and you decide to dispatch it to an agent.

## When to use

- jleechan asks you to implement, fix, or investigate something that warrants spawning an agent
- You have decided the task merits a full agent run (not a quick inline answer)

## Steps

### 1. Claim or create the bead

```bash
# If bead exists:
br update ORCH-xxx --status in_progress

# If new task:
br create -p P1 -t task --title "short description"
# Note the ORCH-xxx ID from output
```

### 2. Ack in Slack thread (REQUIRED)

**This is the Deterministic Slack Thread Response Contract.**

Reply to jleechan's original Slack message in the same thread:

> On it. Spawning agent for **ORCH-xxx** — will reply here when done.

Record the `ts` of jleechan's original message as `SLACK_TRIGGER_TS`.

**Proof-First Requirement**: When the supervisor posts completion, it MUST include at least one reviewable proof URL (PR, commit, or artifact):
- PR URL: `https://github.com/OWNER/REPO/pull/NUMBER`
- Commit URL: `https://github.com/OWNER/REPO/commit/SHA`
- Artifact URL: durable build/test/deploy artifact link
It SHOULD include multiple proof URLs when available (for example, PR + commit). No "task done" without at least one proof URL. See SOUL.md "Autopilot Policy" for the full contract.

### 3. Before dispatching: Search memories

**Always search memories before writing the task prompt.** Use `/mem-search` or the memory MCP to find:
- Past successes/failures for similar tasks
- Specific gotchas or patterns for this type of work
- Any injected context from previous failures

Inject relevant learnings into the task prompt to prevent repeat failures.

### 4. Dispatch via mctrl

```bash
cd ~/project_smartclaw/mctrl

PYTHONPATH=src python -m orchestration.dispatch_task \
  --bead-id ORCH-xxx \
  --task "full task description for the agent (enriched with memory learnings)" \
  --slack-trigger-ts "$SLACK_TRIGGER_TS" \
  --slack-trigger-channel "$SLACK_TRIGGER_CHANNEL" \
  --agent-cli claude
```

If Jeffrey explicitly requests `codex`, change the command to `--agent-cli codex`. Do not fall back to ACP Codex or a separate subagent path before attempting the mctrl dispatch. If the codex dispatch command fails, report the failure instead of claiming the task was queued.

This will:
- Run `ai_orch run --async --worktree` to spawn a new tmux session + git worktree
- Record `start_sha` (HEAD at spawn time) for accurate commit detection
- Write the BeadSessionMapping to `.tracking/bead_session_registry.jsonl`
- The supervisor loop will watch the session and notify you when it finishes

For GitHub/PR automation, the lifecycle lane should map directly into this
dispatch path. `comment-validation`, `fix-comment`, and `fixpr` are mctrl
lanes, not Mission Control board tasks.

### Cross-repo PRs

When the task involves making a PR to a different repo than the worktree:
- DO NOT clone the target repo into a subdirectory
- Use `gh pr create --repo owner/repo --base main --head <branch>` to PR cross-repo
- Example: for mctrl_test repo, use `gh pr create --repo jleechanorg/mctrl_test --base main`

The dispatcher will ensure the task text instructs the agent to push before it
stops. If your task text does not already include that, `dispatch_task.dispatch()`
appends it automatically. You may also include wording like:

> After making and committing the change, run `git push origin <branch>` and only then stop.

### 5. Confirm dispatch

The command prints:
```
dispatched bead=ORCH-xxx session=ai-claude-xxxxxx worktree=/tmp/ai-orch-worktrees/...
```

Update bead notes:
```bash
br update ORCH-xxx --append-notes "Dispatched to session ai-claude-xxxxxx. Supervisor watching."
```

## What happens next (automatic)

The mctrl supervisor loop (`ai.mctrl.supervisor` launchd agent) runs every 30s and:
1. Checks if the tmux session is still alive
2. When session ends: checks `git log start_sha..HEAD` for commits and verifies the branch is reachable on a configured remote
3. Posts DM to jleechan + thread reply under the original Slack message
4. Sends MCP Agent Mail notification to OpenClaw

**You do not need to poll.** The supervisor handles completion notification, but it will only classify the task as finished if the review surface exists on remote.

## Notes

- `SLACK_TRIGGER_TS` is the Slack `ts` field from jleechan's message (e.g. `1772857900.668299`)
- `SLACK_TRIGGER_CHANNEL` is the Slack channel ID for that same message (e.g. `C0AH3RY3DK6`)
- Always use `--async --worktree` flags so each task gets an isolated git worktree
- Finished means remote-reviewable on a configured remote, not merely committed locally inside the worktree
- The registry is at `.tracking/bead_session_registry.jsonl` in the mctrl repo
- If dispatch_task fails, check that `ai_orch` is on PATH and the mctrl repo is at `~/project_smartclaw/mctrl`
