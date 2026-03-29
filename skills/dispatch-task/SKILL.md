---
name: dispatch-task
version: 1.0.0
description: Dispatch a bead-tracked task via ao spawn/ao send, register the mapping, and ack in Slack thread.
---

# dispatch-task

Use this skill when jleechan asks you to work on a task and you decide to dispatch it to an agent.

## When to use

- jleechan asks you to implement, fix, or investigate something that warrants spawning an agent
- You have decided the task merits a full agent run (not a quick inline answer)
- This applies regardless of how the request arrived: Slack, HTTP gateway, cron, or inline prompt

## NEVER use `sessions_spawn` for coding tasks

`sessions_spawn` is openclaw's internal nested-agent tool. It does NOT create a git worktree, does NOT handle PR lifecycle, pastes prompts without auto-submitting Enter, and allows silent task rewriting. **It is banned for any task involving code, files, or PRs.**

Always use this skill and the `ao` CLI (agent-orchestrator), not OpenClaw's nested `sessions_spawn`.

## Task description: preserve + expand, never condense

Build the task body you pass to `ao send` (via `--file`) in two parts:
1. **User's original text verbatim** — copy it exactly, do not shorten or paraphrase
2. **Memory expansion** — append relevant findings from `/mem-search` or the memory MCP: past failures, known gotchas, patterns that apply to this task

Final task = original text + appended memory context. Never replace the user's words with a summary. If the original is long, that is intentional.

## Steps

### 1. Claim or create the bead

```bash
# If bead exists:
br update ORCH-xxx --status in_progress

# If new task (match CLAUDE.md / PROJECTS_BEADS.md):
br create "short description" --type task --priority 1
# Note the ORCH-xxx ID from output
```

### 2. Ack in Slack thread (REQUIRED)

**This is the Deterministic Slack Thread Response Contract.**

Record the Slack context from jleechan's original message:
- `SLACK_TRIGGER_TS` = the `ts` field from jleechan's message (e.g. `1772857900.668299`)
- `SLACK_TRIGGER_CHANNEL` = the channel ID (e.g. `$SLACK_CHANNEL_ID`)

Reply to jleechan's original Slack message in the same thread:

> On it. Spawning agent for **ORCH-xxx** — will reply here when done.

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

### 4. Dispatch via ao

First, determine the ao project ID from the bead or context:

```bash
# Look up which project the bead belongs to
ao projects list        # shows all configured projects and their IDs
br show ORCH-xxx        # bead description often names the repo/project
```

Example IDs that sometimes appear in configs include `jleechanclaw`, `worldai`, `mctrl`, and `agent-orchestrator`. Always confirm the correct ID with `ao projects list` and the bead/repo context—do not pick one from this list by guesswork.

If jleechan explicitly requests Codex (or another agent CLI), use the override flags your `ao spawn` supports (`ao spawn --help`); defaults live under `defaults.agent` in `agent-orchestrator.yaml`. Do not fall back to `sessions_spawn`.

Then spawn and send:

```bash
# 1. Create worktree + session
ao spawn ORCH-xxx -p <project-id>

# 2. Send task verbatim (auto-submits — no manual Enter needed)
TASK_FILE=$(mktemp)
trap 'rm -f "$TASK_FILE"' EXIT
cat > "$TASK_FILE" <<'TASK'
<full task description enriched with memory learnings>
TASK
ao send <session-name> --file "$TASK_FILE"
```

If ao spawn or ao send fails, report the failure instead of claiming the task was queued.

For GitHub/PR automation, the lifecycle lane should map directly into this
dispatch path. `comment-validation`, `fix-comment`, and `fixpr` are mctrl
lanes, not Mission Control board tasks.

### Cross-repo PRs

When the task involves making a PR to a different repo than the worktree:
- DO NOT clone the target repo into a subdirectory
- Use `gh pr create --repo owner/repo --base main --head <branch>` to PR cross-repo
- Example: for a repo, use `gh pr create --repo $GITHUB_ORG/$REPO --base main`

Ensure the task text instructs the agent to push before it stops. Include wording like:

> After making and committing the change, run `git push origin <branch>` and only then stop.

### 5. Confirm dispatch

The `ao spawn` command prints the session name. Note it for tracking.

Update bead notes with the session name **and Slack context** so the supervisor knows which thread to reply to:
```bash
br update ORCH-xxx --append-notes "Dispatched to session <session-name>. slack_trigger_ts=<SLACK_TRIGGER_TS> slack_trigger_channel=<SLACK_TRIGGER_CHANNEL>. Supervisor watching."
```

The mctrl supervisor reads `slack_trigger_ts` and `slack_trigger_channel` from bead notes to post the completion reply in the correct Slack thread.

## What happens next (automatic)

The mctrl supervisor loop (`ai.mctrl.supervisor` launchd agent) runs every 30s and:
1. Checks if the tmux session is still alive
2. When session ends: checks `git log start_sha..HEAD` for commits and verifies the branch is reachable on a configured remote
3. Posts DM to jleechan + thread reply under the original Slack message
4. Sends MCP Agent Mail notification to OpenClaw

**You do not need to poll.** The supervisor handles completion notification, but it will only classify the task as finished if the review surface exists on remote.

## Notes

- `ao spawn` creates an isolated git worktree for each task automatically (configured in `agent-orchestrator.yaml`)
- Finished means remote-reviewable on a configured remote, not merely committed locally inside the worktree
- If `ao spawn` fails, check that `ao` is on PATH and agent-orchestrator is properly configured
