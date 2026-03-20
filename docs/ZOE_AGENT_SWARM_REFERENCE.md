# ZOE: OpenClaw + Agent Swarm Reference

**Source:** Email from Elvis (@eRvissun) — "OpenClaw + Codex/ClaudeCode Agent Swarm: The One-Person Dev Team [Full Setup]"
**Saved:** 2026-03-09
**Relevance:** This is essentially the "replace me" architecture Jeffrey wants to build — OpenClaw as orchestrator (Zoe), spawning Codex/Claude agents, monitoring via cron, notifying on completion.

---

## Core Thesis

> "I went from managing claude code, to managing an openclaw agent that manages a fleet of other claude code and codex agents."

**Context windows are zero-sum.** Two-tier system:
- **Orchestrator (Zoe/OpenClaw):** business context, decisions, memory, prompt-writing
- **Coding agents (Codex/CC):** codebase, conventions, task execution

Specialization through context, not different models.

---

## Proof Points

- 94 commits in one day (3 client calls, editor never opened)
- ~50 commits/day average
- 7 PRs in 30 minutes
- Cost: ~$100/mo Claude + $90/mo Codex (can start at $20)
- Success rate: one-shots almost all small-medium tasks

---

## The 8-Step Workflow

### Step 1: Scope with Orchestrator
- Customer request → discuss with Zoe
- Zoe has full context: meeting notes (Obsidian vault), CRM, past decisions
- Zoe: tops up credits, pulls prod DB config (read-only), writes precise prompt, spawns agent

### Step 2: Spawn Agent
Each agent gets:
- Own git worktree (`git worktree add`)
- Own tmux session
- Detailed prompt with full context

```bash
# Codex
codex --model gpt-5.3-codex \
  -c "model_reasoning_effort=high" \
  --dangerously-bypass-approvals-and-sandbox \
  "Your prompt here"

# Claude Code
claude --model claude-opus-4.5 \
  --dangerously-skip-permissions \
  -p "Your prompt here"
```

**Key insight: Use tmux over codex exec / claude -p** — enables mid-task redirection without killing the agent:
```bash
tmux send-keys -t codex-templates "Stop. Focus on the API layer first." Enter
```

### Step 3: Monitor (Cron Every 10 Min)

Deterministic, token-efficient script reads JSON registry:
```bash
.clawdbot/check-agents.sh
```
- Checks tmux session liveness
- Checks open PRs on tracked branches
- Checks CI status via `gh` cli
- Auto-respawns failed agents (max 3 attempts)
- Only alerts if human attention needed

Task registry: `.clawdbot/active-tasks.json`
```json
{
  "id": "feat-custom-templates",
  "tmuxSession": "codex-templates",
  "agent": "codex",
  "status": "running",
  "notifyOnComplete": true
}
```

### Step 4: Agent Creates PR

`gh pr create --fill` — **not done yet**. PR alone doesn't trigger notification.

**Definition of Done:**
- PR created
- Branch synced to main (no conflicts)
- CI passing (lint, types, unit, E2E)
- Codex review passed
- Claude Code review passed
- Gemini review passed
- Screenshots included (if UI changes — CI fails without them)

### Step 5: Automated Code Review (3 Models)

| Reviewer | Strength | Notes |
|----------|----------|-------|
| **Codex** | Edge cases, logic errors, race conditions | Most thorough, low false positives |
| **Gemini Code Assist** | Security, scalability | Free, catches things others miss |
| **Claude Code** | Validation | Tends toward overengineering suggestions; skip unless critical |

### Step 6: Automated Testing

- Lint + TypeScript checks
- Unit tests
- E2E tests
- Playwright against preview env (identical to prod)
- **UI changes require screenshot in PR description** (CI enforced)

### Step 7: Human Review

Telegram notification: "PR #341 ready for review."
Review time: 5-10 minutes. Many merges without reading code — screenshot tells the story.

### Step 8: Merge

Daily cron cleans up orphaned worktrees and task registry JSON.

---

## The Ralph Loop V2

Classic Ralph Loop: context → output → evaluate → save learnings (static prompt each cycle).

**Zoe's improvement:** When agent fails, doesn't respawn with same prompt. Uses full business context to unblock:
- Out of context? "Focus only on these three files."
- Wrong direction? "Stop. Customer wanted X. Here's what they said."
- Needs clarification? "Here's their email and what the company does."

**Pattern logging:** When agents succeed, the prompt pattern gets logged. Over time, Zoe writes better prompts because she remembers what shipped. Reward signals: CI passing + all 3 reviews passing + human merge.

**Proactive work (no waiting):**
- Morning: Scans Sentry → spawns agents for errors
- After meetings: Scans meeting notes → flags feature requests → spawns agents
- Evening: Scans git log → updates changelog/docs

---

## Agent Selection Guide

| Agent | Best For |
|-------|----------|
| **Codex** | Backend logic, complex bugs, multi-file refactors, reasoning across codebase. Workhorse (90% of tasks) |
| **Claude Code** | Frontend, git operations, faster turnaround |
| **Gemini** | UI design sensibility — generate HTML/CSS spec, hand to Claude to implement |

Zoe routes: billing bug → Codex. Button fix → Claude Code. New dashboard → Gemini designs, Claude builds.

---

## Context Window Architecture

```
ZOE (Orchestrator)              CODEX (Coding Agent)
─────────────────               ────────────────────
BUSINESS CONTEXT                AGENTS.md
  • Customer CRM                  • Repo conventions
  • Meeting notes                 • Code style guide
  • Competitor intel
  • Who's paying               ENGINEERING DOCS
                                  • Design docs
SKILLS                            • Feature specs
  • Marketing                     • API schemas
  • Research
  • Writing                    CODEBASE
  • Web search                    • src/components/
                                  • src/lib/
MEMORY SYSTEM                     • Type definitions
  • MEMORY.md                     • Test patterns
  • Daily notes
  • Past decisions             (just task prompt)

(minimal code)

GOOD AT:                        GOOD AT:
• Understanding why             • Understanding codebase
• Customer priorities           • Following conventions
• Research + analysis           • Writing correct code
• Writing prompts               • Running tests

BAD AT:                         BAD AT:
• Writing actual code           • Knowing why it matters
• File structures               • Prioritizing features
• Code conventions              • Long-term memory
```

---

## Hardware Note

RAM is the bottleneck. Each agent = own worktree + node_modules + builds/tests.
- 16GB Mac Mini: tops out at 4-5 parallel agents
- Elvis bought Mac Studio M4 Max 128GB ($3,500) to scale

---

## Mapping to Jeffrey's Setup

| Elvis's System | Jeffrey's Equivalent |
|----------------|---------------------|
| Zoe (orchestrator) | OpenClaw (jleechanclaw agent) |
| Obsidian vault | MEMORY.md + weekly memory files + SOUL.md |
| `.clawdbot/active-tasks.json` | `src/orchestration/session_registry.py` |
| `check-agents.sh` | `src/orchestration/supervisor.py` |
| Telegram notifications | Slack DM via `openclaw_notifier.py` |
| Codex agents | `ai_orch` workers in tmux+worktrees |
| 3-model PR review | `/copilot` + CodeRabbit |

**Key gap:** Elvis's Zoe writes *context-rich prompts* for each agent based on business history. Jeffrey's OpenClaw dispatches tasks but doesn't yet inject deep context from MEMORY.md into the agent prompt. This is the L2 gap in `REPLACE_ME_DESIGN.md`.

**The "replace me" goal IS Zoe.** The memory system work (seed_memory, extract_patterns, ghost) is building Zoe's business context layer.
