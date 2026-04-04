# Harness Engineering Analyzer

> **NEW (2026-03-18):** This repo now includes an automated daily harness engineering analyzer that runs at 9am via launchd.

Automated daily analysis tool for the smartclaw repository that checks for harness engineering violations and takes corrective action.

## Quick Reference: Daily Analyzer

| Item | Details |
|------|---------|
| **Schedule** | 9:00 AM Pacific daily |
| **Script** | `harness-automation/harness-analyzer.sh` |
| **LaunchAgent** | `~/Library/LaunchAgents/com.jleechan.harness-analyzer.plist` |
| **Logs** | `harness-automation/harness-analyzer.log` |

### What the Analyzer Does

1. **Clones** the smartclaw/smartclaw repository
2. **Analyzes** the codebase for harness engineering violations
3. **Creates PRs** to fix any violations found  
4. **Comments** on all open PRs with harness engineering suggestions

### Violations Detected

| Pattern | Description |
|---------|-------------|
| Hardcoded Credentials | API keys, passwords, tokens found in code |
| Missing Error Handling | Async functions without try-catch or .catch() |
| Improper Async Patterns | async without await, await in non-async |
| Missing Input Validation | Function parameters without validation |
| Console.log Leaks | console.log statements in production code |
| Missing .gitignore | Missing entries for secrets/credentials |
| Unhandled Promise Rejections | Promises without .catch() handlers |

---

# smartclaw

Smoke test bead: rev-faf

Tools, scripts, and configuration for **smartclaw** — an autonomous orchestrator agent that replaces Jeffrey as the day-to-day operator across all jleechanorg projects.

## User-Focused System Overview

smartclaw is the **control layer**. It turns plain-English requests into reliable engineering execution:

1. You ask in Slack (or another connected channel).
2. smartclaw expands context (thread history, policy, constraints, memory).
3. It routes work to the right execution engine (often Agent Orchestrator / `ao`).
4. Worker agents execute in isolated git worktrees, run checks, and push updates.
5. smartclaw reports back with proof artifacts (PR URL, commit URL, checks).

### smartclaw + Agent Orchestrator (exact repo pairing)

This harness is designed to work directly with the [`jleechanorg/agent-orchestrator`](https://github.com/jleechanorg/agent-orchestrator) fork
(not the upstream [`ComposioHQ/agent-orchestrator`](https://github.com/ComposioHQ/agent-orchestrator)):

- AO plugin interfaces: `packages/core/src/types.ts`
- OpenClaw notifier plugin: `packages/plugins/notifier-openclaw/src/index.ts`
- GitHub SCM integration: `packages/plugins/scm-github/src/index.ts`
- tmux runtime plugin: `packages/plugins/runtime-tmux/src/index.ts`

In short: **smartclaw handles intent, policy, and user communication**; **Agent Orchestrator handles parallel execution, worktree isolation, and PR/CI feedback loops**.

<!-- E2E probe 1773342687 -->

## Quick Start (Fresh Machine)

```bash
git clone https://github.com/jleechanorg/smartclaw.git ~/.smartclaw
cd ~/.smartclaw
# Create openclaw.json with your real tokens (no redacted template is committed)
bash scripts/bootstrap.sh                 # writes/links baseline config + launchd files
# Set env vars in ~/.bashrc (see Environment Variables below)
bash install.sh                           # installs Python package + launchd services
./health-check.sh                         # verify
```

`install.sh` will:
1. Set up symlinks and config copies
2. Verify `openclaw.json` exists and has real tokens
3. Install the local Python orchestration package (`pip install -e .`)
4. Install all launchd services (LaunchAgents on macOS)

## Environment Variables

All Slack channels and runtime tunables are configurable via environment variables. Set them in `~/.bashrc` (or `~/.bash_profile` / `~/.zshrc`) and they will be picked up by `monitor-agent.sh` and other scripts on the next run.

### New Machine Setup

All Slack channel IDs and user IDs are env vars. On a fresh machine, add this block to `~/.bashrc` (or `~/.zshrc`) before running anything:

```bash
# ── Slack identity ────────────────────────────────────────────────────────────
export SLACK_BOT_TOKEN="xoxb-..."        # from Slack app OAuth & Permissions
export OPENCLAW_SLACK_APP_TOKEN="xapp-..."        # optional — Socket Mode only
export SLACK_USER_TOKEN="xoxp-..."                # your personal user token (for agento triggers)

# ── Slack user/bot IDs (change if using a different Slack workspace) ──────────
export JLEECHAN_SLACK_USER_ID="U09GH5BR3QU"      # your Slack user ID
export OPENCLAW_BOT_USER_ID="U0AEZC7RX1Q"        # openclaw bot user ID

# ── Slack channels (change if using a different Slack workspace) ──────────────
export AGENTO_CHANNEL="C0AJQ5M0A0Y"              # #ai-general — agento dispatch
export SLACK_TEST_CHANNEL="${SLACK_CHANNEL_ID}"          # #ai-slack-test — E2E test channel
export JLEECHAN_DM_CHANNEL="${SLACK_CHANNEL_ID}"         # your DM channel (for notifications)
export OPENCLAW_MONITOR_SLACK_TARGET="C0AP8LRKM9N"     # canary/monitor alert channel
export OPENCLAW_MONITOR_THREAD_REPLY_CHANNEL="C0AP8LRKM9N"

# ── Gateway ───────────────────────────────────────────────────────────────────
export OPENCLAW_URL="http://127.0.0.1:18789"
export OPENCLAW_GATEWAY_TOKEN="<token>"           # from openclaw.json
export OPENCLAW_AO_HOOK_TOKEN="<token>"           # from openclaw.json hooks.token (or OPENCLAW_HOOKS_TOKEN)
```

To find channel/user IDs in a new Slack workspace: open the channel in Slack → right-click → "Copy link" — the ID is the last segment (starts with `C` for channels, `D` for DMs, `U` for users).

### Slack Credentials

| Variable | Required | Description |
|----------|----------|-------------|
| `SLACK_BOT_TOKEN` | Yes | Slack bot token (`xoxb-…`). Used by monitor and notifier to post alerts. |
| `OPENCLAW_SLACK_APP_TOKEN` | Optional | Slack app-level token (`xapp-…`). Used for Socket Mode if enabled. |
| `SLACK_USER_TOKEN` | For agento | Personal user token (`xoxp-…`). Required to post as you (not the bot) when triggering agento. |

### Slack Identity

| Variable | Value | Description |
|----------|-------|-------------|
| `JLEECHAN_SLACK_USER_ID` | `U09GH5BR3QU` | Your Slack user ID. Scripts use this to identify messages from you. |
| `OPENCLAW_BOT_USER_ID` | `U0AEZC7RX1Q` | openclaw bot user ID. Gateway ignores its own messages using this. |

### Slack Channels

All channel IDs are env vars — change them in `~/.bashrc` when moving to a new workspace.

| Variable | Channel | ID | Purpose |
|----------|---------|-----|---------|
| `AGENTO_CHANNEL` | `#ai-general` | `C0AJQ5M0A0Y` | Agento dispatch — post here as you to trigger agento |
| `SLACK_TEST_CHANNEL` | `#ai-slack-test` | `${SLACK_CHANNEL_ID}` | E2E test channel for mctrl/monitor tests |
| `JLEECHAN_DM_CHANNEL` | jleechan DM | `${SLACK_CHANNEL_ID}` | Your DM channel — notifications land here |
| `OPENCLAW_MONITOR_SLACK_TARGET` | monitor canary channel | `C0AP8LRKM9N` | Primary alert channel for monitor problem reports |
| `OPENCLAW_MONITOR_PROBE_SLACK_TARGET` | same | same | Probe channel for gateway Slack connectivity |
| `OPENCLAW_MONITOR_GATEWAY_PROBE_TARGET` | same | same | Channel monitor sends probe messages to |
| `OPENCLAW_MONITOR_GATEWAY_PROBE_MESSAGE_ENABLE` | `0` | n/a | Set to `1` to post startup "monitor check started" probe; default `0` keeps startup probe silent |
| `OPENCLAW_MONITOR_STATUS_SLACK_TARGET` | monitor canary channel | `C0AP8LRKM9N` | Status-broadcast — receives periodic health summary |
| `OPENCLAW_MONITOR_THREAD_REPLY_CHANNEL` | same | same | Scanned for open threads lacking a bot reply |

Other known channel IDs (not yet env vars):

| Channel | ID |
|---------|----|
| `#all-jleechan-ai` | `${SLACK_CHANNEL_ID}` |
| `#smartclaw` | `C0AJ3SD5C79` |
| `#novel` | `C0ANS2MF15G` |
| `#antig-orchestrator` | `C0ANX2HU5V1` | Anti-gravity orchestrator discussion |
| `#ralph-status` | `C0AGX2Q0EA3` |
| `#disk-usage-alerts` | `C0AKNDEARS5` |

### Agent Orchestrator

| Variable | Required | Description |
|----------|----------|-------------|
| `OPENCLAW_AO_HOOK_TOKEN` | Yes | Webhook token for the AO → openclaw notifier. Set this so `agent-orchestrator.yaml` can authenticate to the local openclaw gateway at `http://127.0.0.1:18789/hooks/agent`. Get the value from the openclaw gateway config: `openclaw.json` → `hooks.token` (commonly exposed as `OPENCLAW_HOOKS_TOKEN`), or reuse a previously generated token. |

Config file lives in this repo as `agent-orchestrator.yaml`. `bootstrap.sh` creates `~/agent-orchestrator.yaml` as a symlink to the repo copy — so on any machine, clone this repo and run `bash scripts/bootstrap.sh` to get the live config in place.

Example:
```bash
# ~/.bashrc
export OPENCLAW_AO_HOOK_TOKEN="<token>"
```

### Monitor Agent — Behaviour Tunables

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENCLAW_MONITOR_HTTP_GATEWAY_URL` | `http://127.0.0.1:18789/health` | URL for the HTTP health-check probe against the local gateway. |
| `OPENCLAW_MONITOR_SLACK_API_BASE` | `https://slack.com/api` | Base URL for Slack API calls (override for testing). |
| `OPENCLAW_MONITOR_CANARY_TIMEOUT_SECONDS` | `45` | How long to wait for a canary Slack round-trip before declaring failure. |
| `OPENCLAW_MONITOR_CANARY_POLL_INTERVAL_SECONDS` | `3` | Polling interval while waiting for canary response. |
| `OPENCLAW_MONITOR_RUN_CANARY` | `1` | Slack canary probe enabled by default. Sender token precedence: `OPENCLAW_MONITOR_CANARY_BOT_TOKEN` → `~/.mcp_mail/credentials.json:SLACK_BOT_TOKEN` → `SLACK_USER_TOKEN` (legacy fallback). Set to `0` to disable. |
| `OPENCLAW_MONITOR_CANARY_BOT_TOKEN` | _(unset)_ | Optional dedicated bot token for canary sender identity (recommended for bot-to-bot canary checks). |
| `OPENCLAW_MONITOR_PHASE1_REMEDIATION_ENABLE` | `1` | Set to `0` to disable Phase 1 auto-remediation (gateway restart). |
| `OPENCLAW_MONITOR_PHASE2_ENABLE` | `1` | Set to `0` to disable Phase 2 deep-diagnosis. |
| `OPENCLAW_MONITOR_PHASE2_AUTOFIX_ENABLE` | `1` | Set to `0` to prevent Phase 2 from applying auto-fixes. |
| `OPENCLAW_MONITOR_PHASE2_ALLOW_CONFIG_MUTATIONS` | `0` | Set to `1` to allow Phase 2 to mutate openclaw config files. |
| `OPENCLAW_MONITOR_PHASE2_TIMEOUT_SECONDS` | `120` | Max seconds Phase 2 may run before being killed. |
| `OPENCLAW_MONITOR_STATUS_BROADCAST_ENABLE` | `1` | Set to `0` to suppress the periodic status broadcast. |
| `OPENCLAW_MONITOR_THREAD_REPLY_CHECK` | `1` | Set to `0` to disable the open-thread reply check. |
| `OPENCLAW_MONITOR_THREAD_REPLY_LOOKBACK_SECONDS` | `21600` | How far back (in seconds) to look for unanswered threads (default 6 h). |
| `OPENCLAW_MONITOR_THREAD_REPLY_GRACE_SECONDS` | `120` | Grace period after a message is posted before it counts as unanswered. |
| `OPENCLAW_MONITOR_THREAD_REPLY_MAX_THREADS` | `12` | Max threads to inspect per run. |
| `OPENCLAW_MONITOR_THREAD_REPLY_FAILURE_REGEX` | (error patterns) | Extended-regex matched against thread messages to detect failure signatures. |
| `OPENCLAW_MONITOR_THREAD_REPLY_FAILURE_MAX_AGE_SECONDS` | `900` | Threads older than this (15 min) with failure patterns are suppressed. |
| `OPENCLAW_MONITOR_BOT_USER_ID` | _(auto-detected)_ | Slack user ID of the openclaw bot. Used to check whether the bot has already replied. |
| `OPENCLAW_MONITOR_LOCK_STALE_SECONDS` | `7200` | Lock file is considered stale after this many seconds (allows a stuck run to be superseded). |
| `OPENCLAW_MONITOR_DOCTOR_SH_ENABLE` | `1` | Set to `0` to skip running `scripts/doctor.sh` during monitoring. |
| `OPENCLAW_MONITOR_DOCTOR_SH_ALWAYS` | `1` | Set to `0` to run doctor only when a problem is detected. |
| `OPENCLAW_MONITOR_DOCTOR_SH_PATH` | _(auto-detected)_ | Path to `doctor.sh`. Override if the script is not at its default location. |

---

## What This Is

This repo is a **harness** — the environment, constraints, and feedback loops that enable AI agents to do reliable work across all jleechanorg projects. It follows the [harness engineering](https://openai.com/index/harness-engineering/) methodology where human engineers design environments and constraints while agents write the code.

The harness configures an OpenClaw agent named **smartclaw** that manages fleets of coding agents (Claude Code, Codex, Gemini, Cursor), monitors their work, handles PR lifecycle via [agent-orchestrator](https://github.com/ComposioHQ/agent-orchestrator), and only escalates when human judgment is truly needed. See `docs/HARNESS_ENGINEERING.md` for the philosophy and `roadmap/ORCHESTRATION_DESIGN.md` for the orchestration design.

Inspired by the [Zoe pattern](https://x.com/eRvissun) — a one-person dev team where the orchestrator holds all business context and delegates specialized coding work to a fleet of agents.

### The Two-Tier Principle

Context windows are zero-sum. Fill it with code and there's no room for business context.

**smartclaw** (orchestrator) holds business context: project goals, roadmaps, past decisions, what worked, what failed. Coding agents hold code context: files, tests, types. Each agent is loaded with exactly what it needs.

## Architecture

```
                    ┌─────────────────────┐
                    │     smartclaw     │
                    │   (orchestrator)     │
                    │  Business context    │
                    │  Task planning       │
                    │  Agent management    │
                    │  PR lifecycle        │
                    └──────────┬──────────┘
                               │
              ┌────────────────┼────────────────┐
              │                │                │
     ┌────────▼──────┐ ┌──────▼───────┐ ┌──────▼───────┐
     │  Claude Code  │ │    Codex     │ │   Gemini     │
     │  (frontend,   │ │  (backend,   │ │  (design,    │
     │   git ops,    │ │   complex    │ │   UI specs,  │
     │   fast iter)  │ │   reasoning) │ │   creative)  │
     └───────────────┘ └──────────────┘ └──────────────┘
```

## How smartclaw Operates

1. **Receives work** — from Jeffrey via Slack, from GitHub notifications, from scanning failing CI
2. **Plans tasks** — breaks work into focused pieces, selects the right agent for each
3. **Spawns agents** — via `ai_orch run` with precise prompts containing full business context
4. **Monitors progress** — CI status, PR reviews, agent health, launchd schedulers
5. **Handles failures** — diagnoses why an agent failed, writes a better prompt, retries (max 3)
6. **Delivers results** — notifies Jeffrey when PRs are ready to merge
7. **Learns** — logs what worked, what didn't, refines future prompts

## Dependencies

### Core

| Dependency | What It Does | Install |
|-----------|--------------|---------|
| [OpenClaw](https://github.com/openclaw/openclaw) | Agent runtime — persistent memory, channels, gateway | `npm install -g openclaw` |
| [jleechanorg-orchestration](https://pypi.org/project/jleechanorg-orchestration/) | Task dispatch, agent spawning, tmux management | `pip install jleechanorg-orchestration` |
| [jleechanorg-automation](https://pypi.org/project/jleechanorg-automation/) | PR monitor, comment-validation, codex-update, fixpr | `pip install jleechanorg-automation` |

### OpenClaw Plugins

| Plugin | What It Does | Install |
|--------|--------------|---------|
| [openclaw-mem0](https://github.com/jleechanorg/openclaw-mem0) | Long-term semantic memory via Qdrant + mem0 — auto-captures and recalls context across sessions | `openclaw plugins install openclaw-mem0` (already bundled in extensions/) |
| [lossless-claw](https://github.com/martian-engineering/lossless-claw) | Replaces sliding-window context truncation with hierarchical DAG summarization — all messages persist in SQLite (`~/.smartclaw/lcm.db`), compressed via LLM when context fills. Exposes `lcm_grep`, `lcm_describe`, `lcm_expand` tools so agents can search full history | `openclaw plugins install --link ~/projects_reference/lossless-claw` (linked local clone) |

### Agent CLIs

| CLI | Use Case | Auth |
|-----|----------|------|
| `claude` (`@anthropic-ai/claude-code`) | Frontend, git ops, fast iteration | Anthropic OAuth |
| `codex` (`@openai/codex`) | Backend, complex reasoning, workhorse | OpenAI OAuth (profile: `openai-codex`) |
| `gemini` (Gemini CLI) | Design, UI specs, creative generation | Google OAuth |
| `cursor-agent` | IDE-integrated tasks | Cursor account |
| `minimax` | Uses `claude` CLI with MiniMax API backend via `ANTHROPIC_BASE_URL` | MiniMax API key |

### Coordination and Monitoring

| Tool | Purpose | Config |
|------|---------|--------|
| [MCP Agent Mail](https://github.com/jleechanorg/mcp_mail) | Cross-agent messaging — "Gmail for coding agents"; routes tasks between claude/codex/gemini | `~/.claude.json` mcpServers |
| [Beads / beads_rust](https://github.com/jleechanorg/beads) | Lightweight issue tracker; `br` CLI (symlinked as `bd`); issues in `.beads/issues.jsonl` | `BEADS_PATH`, `BEADS_WORKING_DIR` |
| [ai_orch](https://pypi.org/project/jleechanorg-orchestration/) | Spawns agents in tmux worktrees; dispatches tasks from `dispatch_task.py` | Installed via pip |
| Qdrant | Vector DB for openclaw-mem0 semantic memory | LaunchAgent: `ai.smartclaw.qdrant` |

### Infrastructure

| Requirement | Version |
|-------------|---------|
| Node.js | 22+ |
| Python | 3.11+ |
| tmux | any |
| git + gh | any |

## What's In This Repo

### Root-level configuration

| File/Dir | Purpose |
|----------|---------|
| `SOUL.md` | smartclaw personality, goals, and decision-making rules |
| `TOOLS.md` | Tool allow/deny list and usage policy |
| `USER.md` | User context (Jeffrey's preferences, communication style) |
| `IDENTITY.md` | Agent identity definition |
| `AGENTS.md` | Per-agent configuration |
| `openclaw.json` | Live local gateway config (gitignored; must contain real tokens and runtime settings) |
| `*.plist` | Launchd plists for macOS services |
| `AUTO_START_GUIDE.md` | How to set up all launchd services from scratch |
| `BACKUP_AND_RESTORE.md` | Backup and restore runbook |
| `SLACK_SETUP_GUIDE.md` | Slack app and token setup |
| `HEARTBEAT.md` | Heartbeat / health-check protocol |
| `security-policy.md` | Tool execution security policy |
| `skills/` | Custom openclaw skills |
| `agents/` | Per-agent session state |

### `workspace/` — Agent configuration

| File/Dir | Purpose |
|----------|---------|
| `SOUL.md` | smartclaw personality |
| `TOOLS.md` | Tool policy |
| `USER.md` | User context |
| `IDENTITY.md` | Agent identity |
| `AGENTS.md` | Agent rules |
| `MEMORY.md` | Persistent memory |
| `docs/` | Design docs and runbooks |
| `roadmap/` | Planning documents |

### `src/` — Python source

| Module | Purpose |
|--------|---------|
| `src/orchestration/` | 50+ module orchestration engine — see [Orchestration Engine](#orchestration-engine-srcorchestration) below |
| `src/genesis/` | Config, cron, and memory generation utilities |
| `src/tests/` | pytest suite for orchestration modules |

### `testing_integration/` — Real I/O integration tests

| Directory | Purpose |
|-----------|---------|
| `testing_integration/` | Real I/O integration tests for orchestration modules (no mocks, no monkeypatching) |

### `scripts/` — Shell utilities

| Script | Purpose |
|--------|---------|
| `backup-openclaw-full.sh` | Full recursive backup of `~/.smartclaw/` with secret redaction |
| `run-openclaw-backup.sh` | Backup runner with locking and failure alerts |
| `dropbox-openclaw-backup.sh` | Dropbox-targeted backup |
| `install-openclaw-backup-jobs.sh` | Install launchd plist for scheduled backups |
| `check-openclaw-cron-guardrail.sh` | CI guardrail: launchd-only scheduling for repo-managed OpenClaw jobs |
| `setup-openclaw-full.sh` | Full first-time OpenClaw setup |
| `install-launchagents.sh` | Install all openclaw launchd plists from root directory |
| `install-symphony-daemon.sh` | Install/start the launchd-managed Symphony daemon used by `sym` routing |
| `sym-dispatch.sh` | Queue a freeform task or plugin payload into the Symphony daemon |
| `sym-send-5-leetcode-hard.sh` | Queue the default 5 LeetCode Hard tasks via Symphony plugin |
| `sym-send-5-swebench-verified.sh` | Queue the default 5 SWE-bench Verified tasks via Symphony plugin |
| `claude_start.sh` | Start Claude Code agent session |
| `push.sh` | Safe push with branch verification |
| `sync_branch.sh` | Sync branch with upstream |
| `resolve_conflicts.sh` | Semi-automated conflict resolution |
| `create_snapshot.sh` | Create workspace snapshot |
| `consolidate-workspace-snapshots.sh` | Merge workspace snapshots |
| `peekaboo-preflight.sh` | Preflight checks for Peekaboo UI automation |
| `run_lint.sh` | Lint the Python source |
| `run_tests_with_coverage.sh` | Run tests with coverage report |
| `codebase_loc.sh` / `loc.sh` | Lines-of-code counters |
| `setup_email.sh` | Email notification setup |
| `setup-github-runner.sh` | Self-hosted GitHub Actions runner setup |

### Root-level scripts

| Script | Purpose |
|--------|---------|
| `health-check.sh` | Gateway and agent health checks |
| `create_worktree.sh` | Create isolated git worktrees for parallel agent work |
| `blank-to-pr.sh` | Scaffold a branch through to PR |
| `bootstrap-openclaw-config.sh` | Bootstrap fresh OpenClaw configuration |
| `enable-auto-backup.sh` | Enable automated backup scheduling |
| `integrate.sh` | Integration tooling |

### `docs/` — Design docs

| File | Purpose |
|------|---------|
| `HARNESS_ENGINEERING.md` | Harness engineering philosophy — how this repo is a harness |
| `GENESIS_DESIGN.md` | Original design doc (historical) |
| `ORCHESTRATION_RESEARCH_2026.md` | Competitive analysis of orchestration alternatives |
| `openclaw-backup-jobs.md` | Backup job documentation |
| `orchestration-system-justification.md` | Why the Python orchestration layer exists |
| `symphony-runtime-dedupe.md` | What Symphony runtime behavior remains local vs delegated upstream |
| `STAGING_PIPELINE.md` | 3-stage dev pipeline reference: staging branch, canary, CI gate (HTML: `STAGING_PIPELINE.html`) |
| `user_preferences_learnings.md` | Learned user preferences log |

### `roadmap/` — Planning docs

| File | Purpose |
|------|---------|
| `GENESIS_DESIGN.md` | Renamed smartclaw design (canonical) |
| `ORCHESTRATION_DESIGN.md` | Orchestration system design (AO + OpenClaw) |
| `ORCHESTRATION_IMPL_ROADMAP.md` | TDD implementation roadmap (7 phases, 24 commits) |
| `NATURAL_LANGUAGE_DISPATCH.md` | Why config > code for scheduling |
| `DURABLE_BEHAVIOR_HARDENING_PLAN.md` | Plan for making behavior deterministic |
| `SYMPHONY_WEBHOOK_PR_REMEDIATION_DESIGN.md` | Webhook-first design for PR comments/CI auto-remediation |
| `ORCHESTRATION_EVIDENCE_STANDARDS.md` | Evidence standards for orchestration claims |
| `OUTCOME_LEDGER_DESIGN.md` | Outcome tracking design |
| `PEEKABOO_ANTIGRAVITY_UI_AUTOMATION.md` | UI automation design |
| `TDD_EXECUTION_ROADMAP.md` | TDD execution plan |
| `BACKUP_REDUNDANCY_DESIGN.md` | Backup redundancy design |

### `discord-eng-bot/`

Standalone OpenClaw agent config for a Discord engineering bot — separate `openclaw.json` and `SOUL.md` for a Discord-channel-native agent persona.

## Orchestration Engine (`src/orchestration/`)

The orchestration engine goes beyond what Agent Orchestrator (AO) provides: convergence intelligence, evidence-gated merges, LLM-powered PR review, and utilities that don't exist in AO's TypeScript core.

### Convergence Intelligence — detect and surface systemic issues

| Module | Purpose |
|--------|---------|
| `auto_triage.py` | Scans `outcomes.jsonl` for repeated escalations; Slack DMs when same error class escalates 2+ times in 7 days |
| `regression_detector.py` | Weekly health monitor: computes MTTR, escalation rate, win rate; alerts on week-over-week regressions |

### Agent Coordination — manage agent sessions and tasks

| Module | Purpose |
|--------|---------|
| `guidance_tracker.py` | Tracks MCP mail guidance delivery/acknowledgment; auto-files beads when agents ignore 2+ messages |
| `task_tracker.py` | Cross-session task/subtask state with file-locked atomic persistence; links subtasks to AO session IDs |
| `decomposition_dispatcher.py` | Spawns AO sessions for subtasks in parallel (max 4 concurrent); retries once on failed spawn |
| `session_tail.py` | Live tmux output streaming — one-shot `logs` mode and `--follow` streaming mode |

### PR Lifecycle — evidence-gated merges and LLM review

| Module | Purpose |
|--------|---------|
| `evidence.py` | Evidence packet schema with completeness classification (COMPLETE / PARTIAL / MISSING) |
| `evidence_review_gate.py` | Fail-closed merge gate: blocks if evidence review fails; passes with warning when no evidence required |
| `pr_reviewer.py` | Assembles full LLM review context: PR diff, CI status, CLAUDE.md rules, memory, prior review patterns |
| `pr_review_decision.py` | Pure LLM review engine — no hardcoded rules; LLM reads full context and decides approve / request\_changes / escalate |
| `auto_resolve_threads.py` | DEPRECATED stub — AO's `autoResolveThreads()` is canonical; stubs remain for backward compat |
| `merge_gate.py` | DEPRECATED stub — AO's `merge-gate.ts` (checkMergeGate) is canonical; stubs remain for backward compat |

### Dispatch & Routing — webhook ingress and MCP routing

| Module | Purpose |
|--------|---------|
| `webhook.py` | Combined webhook ingress / queue / worker / daemon: HMAC validation, SQLite dedup, PR-lock bounded retries |
| `mcp_http.py` | JSON-RPC 2.0 MCP router — tools, resources, and prompts via single POST `/mcp` endpoint |

### Symphony — task queue daemon

| Module | Purpose |
|--------|---------|
| `symphony_daemon.py` | Launchd-managed supervisor for the Symphony task queue |
| `symphony_plugins.py` | Symphony plugin definitions (LeetCode Hard, SWE-bench Verified) |

### Utilities — shared helpers

| Module | Purpose |
|--------|---------|
| `backup_redaction.py` | Secret redaction for `~/.smartclaw/` backups before offsite copy |
| `slack_util.py` | Slack posting helpers (thread-ts normalization, channel routing) |
| `datetime_util.py` | Datetime parsing and formatting helpers |
| `event_util.py` | Event serialization and routing utilities |
| `jsonfile_util.py` | Atomic JSONL read/write helpers |
| `path_util.py` | Common path resolution helpers |

### Retired modules (migrated to AO)

Many modules have been retired now that AO handles the equivalent functionality. They raise `ImportError` on import to prevent accidental use. The AO equivalents are documented in each file's header.

Retired modules include: `supervisor`, `session_registry`, `session_reaper`, `gh_integration`, `pr_lifecycle`, `lifecycle_reactions`, `escalation`, `escalation_handler`, `escalation_router`, `coderabbit_gate`, `mcp_mail`, `openclaw_notifier`, `ao_cli`, `ao_events`, `action_executor`, `bead_lifecycle_validator`, `mctrl_status`, `reviewer_agent`, `stage2_reviewer`, `webhook_bridge`, `webhook_worker`, `webhook_queue`, `webhook_reconciler`, `webhook_metrics`, `anomaly_detector`, `reconciliation` (stub), `failure_budget` (stub), `parallel_retry` (stub).

---

## 3-Stage Dev Pipeline

> **Status**: Operational as of 2026-03-31. All P0 items done. One P1 gap remains (git hooks).

Changes to `~/.smartclaw/` (live gateway config) go through a 3-stage safety pipeline before reaching production:

1. **Staging branch** — PRs target `staging` (not `main`); `~/.smartclaw-staging/` is a git worktree
2. **Canary** — `scripts/staging-canary.sh --port 18790` runs 6 health checks against staging gateway
3. **CI gate** — `.github/workflows/staging-canary-gate.yml` runs on every PR (checks 2/6 portable). **Stage 3b** — `.github/workflows/staging-canary-full.yml` runs the full 6/6 canary on a **self-hosted runner** (optional; see `.github/SELFHOSTED_TEST.md`)

**Quick start for config changes:**

```bash
# 1. Edit in feature worktree, open PR → staging (not main)
gh pr create --base staging --head feat/my-change

# 2. CI gate fires automatically

# 3. Merge to staging → staging worktree updates

# 4. Run canary locally
bash scripts/staging-canary.sh --port 18790

# 5. Promote to prod
bash scripts/staging-promote.sh
```

**CI vs local canary:**

| What | Where |
|------|-------|
| Config schema validation | CI + local |
| SDK protocol version | CI + local |
| Gateway health, Slack connectivity, native module ABI, heartbeat | Self-hosted CI (`staging-canary-full.yml`) or manual (`staging-canary.sh --port 18810` / `18790`) |

**Full reference**: [`docs/STAGING_PIPELINE.md`](docs/STAGING_PIPELINE.md) (HTML: [`docs/STAGING_PIPELINE.html`](docs/STAGING_PIPELINE.html))

---

## PR Automation System

> **Status**: PR automation jobs (pr-monitor, comment-validation, fixpr, fix-comment, codex-api) are **not currently configured** in this repository.

## SmartClaw Export (Portable Subset)

This repository can export a portable subset to a downstream mirror repository (default target: `jleechanorg/smartclaw`).

Export flow is map-driven and repeatable:

1. Run portability analysis:
```bash
bash scripts/update-smartclaw-export-map.sh
```
2. Review generated files:
- `scripts/smartclaw-export-map.tsv` (exact exported paths)
- `docs/SMARTCLAW_PORTABILITY_AUDIT.md` (include/exclude rationale + counts)
3. Export and open PR in `jleechanorg/smartclaw`:
```bash
bash scripts/sync-to-smartclaw.sh
```

Portable by design:
- docs, scripts, launchd templates, skills, selected workflow/README/setup files
- automatic sanitization of org/user/path/channel tokens (`jleechan*`, `/Users/jleechan`, `.smartclaw`)

Never exported:
- live config/secrets (`openclaw.json*`)
- runtime state (DBs/logs/memory/credentials)
- internal context snapshots and local-only generated artifacts

If PR automation is needed in the future, it would require:
- Installing `jleechanorg-automation` package
- Setting up `~/.smartclaw/cron/jobs.json` (lives in live `~/.smartclaw/`, not in this repo)
- Configuring the gateway's cron scheduler

## Launchd Scheduled Jobs (repo-managed)

Repo-managed recurring jobs run via launchd labels `ai.smartclaw.schedule.*`.
Plist files are in the repo root (`ai.opencloak.schedule.*.plist`).

> **Note**: Scheduled job payloads are managed in the live `~/.smartclaw/` directory, not in this repository. See `AUTO_START_GUIDE.md` for setting up launchd services.

| Plist | Purpose |
|-------|---------|
| `ai.smartclaw.schedule.backup-4h20.plist` | Backup every 4 hours |
| `ai.smartclaw.schedule.daily-checkin-9am.plist` | Morning check-in |
| `ai.smartclaw.schedule.daily-checkin-12pm.plist` | Midday check-in |
| `ai.smartclaw.schedule.daily-checkin-6pm.plist` | Evening check-in |
| `ai.smartclaw.schedule.genesis-memory-curation-weekly.plist` | Weekly memory curation |

### LaunchAgent Template Portability

Plist files in the root use placeholders (`@HOME@`, `@NODE_BIN_DIR@`, `@NODE_PATH@`, `@PYTHON3_PATH@`) that are resolved at install time by `install.sh`. Node and Python paths are auto-detected from the current environment — no hardcoded nvm versions or Homebrew paths.

## Quick Start

### Gateway health check

```bash
./health-check.sh

# Or directly
curl -sS http://127.0.0.1:18789/health
```

### Spawn an agent

```bash
pip install jleechanorg-orchestration

ai_orch run --agent-cli claude "Fix flaky integration tests and open PR"
ai_orch run --agent-cli codex "Refactor auth middleware"
```

### Backup

```bash
./scripts/backup-openclaw-full.sh
./scripts/install-openclaw-backup-jobs.sh  # set up scheduled backups
```

### Install launchd services

```bash
bash install.sh                              # full setup: Python package + all launchd services
# Or individually:
./scripts/install-launchagents.sh            # installs all plists from root directory
./scripts/install-openclaw-scheduled-jobs.sh # installs and migrates scheduled jobs
```

## Agent Selection Guide

| Task Type | Agent | Why |
|-----------|-------|-----|
| Backend logic, complex bugs, multi-file refactors | Codex | Deep reasoning, low false-positive rate |
| Frontend, git operations, fast iteration | Claude Code | Fast, broad context |
| UI design, specs, creative | Gemini | Design sensibility |
| IDE-integrated tasks | Cursor | Tight IDE integration |

## Projects Managed

| Repo | Description |
|------|-------------|
| [worldarchitect.ai](https://github.com/jleechanorg/worldarchitect.ai) | AI RPG — primary project |
| [codex_fork](https://github.com/jleechanorg/codex_fork) | Fork of Codex open source CLI |
| [beads](https://github.com/jleechanorg/beads) | Memory upgrade for coding agents |
| [ai_universe](https://github.com/jleechanorg/ai_universe) | MCP Backend Server (Firebase + Cerebras) |
| [ai_universe_frontend](https://github.com/jleechanorg/ai_universe_frontend) | Multi-model AI consultation platform |
| [mcp_mail](https://github.com/jleechanorg/mcp_mail) | Agent-to-agent mail coordination |
| [worldai_claw](https://github.com/jleechanorg/worldai_claw) | AI RPG powered by OpenClaw |
| [claude-commands](https://github.com/jleechanorg/claude-commands) | Claude command collection |

## License

Private — personal workspace and tools for jleechan's OpenClaw setup.

bead rev-22j
