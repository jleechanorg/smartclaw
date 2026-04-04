# Genesis: Persistent Orchestration Layer for OpenClaw

## Overview

Genesis is a **configuration and content layer on top of OpenClaw** — not a new system.
After reading the OpenClaw docs and source, most of what Genesis originally proposed
already exists natively. The real work is **filling in existing files** and **tuning config**.

This doc covers: what OpenClaw already provides, what's genuinely new, and the plan.

## What OpenClaw Already Provides (that we just need to use)

### Memory System (docs: https://docs.openclaw.ai/concepts/memory)

OpenClaw uses **plain Markdown files as source of truth**. The sqlite DB at
`~/.openclaw/memory/<agentId>.sqlite` is just an auto-generated index for search.

**Workspace**: `~/.openclaw/workspace/`

| File | Purpose | Current Status |
|------|---------|----------------|
| `SOUL.md` | Agent personality ("Genesis" identity) | Filled — good generic personality |
| `USER.md` | User preferences, projects, context | **BLANK — needs filling** |
| `TOOLS.md` | Local environment (SSH hosts, devices) | **BLANK — needs filling** |
| `MEMORY.md` | Long-term curated knowledge | **DOESN'T EXIST — needs creation** |
| `HEARTBEAT.md` | Periodic self-review tasks | Exists |
| `memory/YYYY-MM-DD.md` | Daily notes (auto-loaded at session start) | Stopped Feb 14 — needs revival |

**At session startup**, OpenClaw loads: today's daily log, yesterday's daily log, SOUL.md, USER.md.

**memory_search** and **memory_get** are built-in agent tools for semantic + keyword search.

**Memory flush before compaction** auto-reminds the agent to persist important context
before the session's context window fills up.

### Configuration Options We Should Enable

In `~/.openclaw/openclaw.json` under `agents.defaults`:

```jsonc
{
  "agents": {
    "defaults": {
      "memorySearch": {
        "enabled": true,
        // Index worldarchitect.ai and other project dirs
        "extraPaths": [
          "/Users/jleechan/projects/worldarchitect.ai/.claude/learnings.md",
          "/Users/jleechan/projects/worldarchitect.ai/CLAUDE.md"
        ],
        "query": {
          "hybrid": {
            "enabled": true,
            "vectorWeight": 0.7,
            "textWeight": 0.3,
            "temporalDecay": {
              "enabled": true,     // Prefer recent memories
              "halfLifeDays": 30   // 30-day half-life
            },
            "mmr": {
              "enabled": true,     // Diversity in results
              "lambda": 0.7
            }
          }
        },
        "experimental": {
          "sessionMemory": true    // Index session transcripts too
        }
      },
      "compaction": {
        "memoryFlush": {
          "enabled": true          // Already enabled by default
        }
      }
    }
  }
}
```

### Cron System

Already running 3x daily Slack check-ins + 4-hourly backups via `~/.openclaw/cron/jobs.json`.
Genesis cron jobs use the same native format.

Repo source of truth for tracked schedules:
- `openclaw-config/cron/jobs.json`

Current Genesis schedule in this repo:
- `genesis-memory-curation-weekly` (weekly Sunday 10pm PT)

Runtime note:
- Cron jobs are executed by OpenClaw runtime components; keep the OpenClaw gateway healthy/running (`openclaw gateway status`).

## What's Genuinely New (Genesis adds)

After subtracting everything OpenClaw already does, Genesis adds only:

### 1. MCP Mail Identity

**Done.** Registered as "Genesis" (agent #1778) on MCP Mail.
This enables cross-agent coordination — subagents can send reports to Genesis.

### 2. Filled-In Context Files

The biggest value-add is actually just **writing content into the existing blank files**:

**`~/.openclaw/workspace/USER.md`**:
```markdown
# USER.md - About Your Human

- **Name:** Jeffrey Lee-Chan
- **What to call them:** Jeff
- **Timezone:** America/Los_Angeles (PST/PDT)

## Projects
- **worldarchitect.ai** — AI RPG built with Python/Firebase/Gemini 3. Goal: first 100 users.
  Repo: /Users/jleechan/projects/worldarchitect.ai
  GitHub: https://github.com/jleechanorg/worldarchitect.ai
  6,341 commits. This is the main project.

- **openclaw** (this repo) — Personal OpenClaw config backup + Genesis orchestration layer.
  Repo: /Users/jleechan/projects_other/openclaw
  GitHub: https://github.com/jleechanorg/openclaw

- **worldai_claw** — AI RPG powered by OpenClaw.
  GitHub: https://github.com/jleechanorg/worldai_claw

## Preferences
- Concise, direct responses. No filler.
- Smallest safe change that solves the request.
- Prefer editing existing files over creating new ones.
- Uses Claude Code, Codex, and Gemini for coding.
```

**`~/.openclaw/workspace/MEMORY.md`**:
```markdown
# MEMORY.md - Long-Term Knowledge

## Architecture Decisions
- worldarchitect.ai: LLM decides, server executes (core principle)
- FastEmbed classifier for intent detection (<50ms)
- Gemini 3 code execution mode REQUIRED
- 10min/600s timeout across all layers

## Patterns That Work
- Use worktrees for parallel agent work
- CI must pass before merge, no exceptions
- memory_search for semantic recall, memory_get for direct reads
- Daily memory files for session context; MEMORY.md for durable facts

## Key Paths
- OpenClaw workspace: ~/.openclaw/workspace/
- OpenClaw config: ~/.openclaw/openclaw.json
- worldarchitect.ai: /Users/jleechan/projects/worldarchitect.ai
- openclaw backup repo: /Users/jleechan/projects_other/openclaw

## Current Goals
- Get first 100 users for worldarchitect.ai (AI RPG)
- Set up Genesis orchestration layer (this)
```

### 3. MEMORY.md Curation Cron Job (optional)

A cron job that periodically reads daily logs (`memory/YYYY-MM-DD.md`) and curates
important patterns/decisions into `MEMORY.md`. This is the one piece that goes beyond
what OpenClaw does automatically.

**How**: OpenClaw cron job with an `agentTurn` payload like:
```
Review daily memory files from the last 7 days (memory/*.md).
Extract any important decisions, patterns, or project status updates.
Update MEMORY.md with curated findings. Don't duplicate existing entries.
Keep MEMORY.md concise and focused on durable knowledge.
```

**Schedule**: Weekly (Sunday 10pm PT) or after N daily files accumulate.

### 4. Active Task Registry (optional)

`~/.openclaw/workspace/tasks.md` — A markdown file (not JSON, so the agent can read/update
it naturally) tracking what's in flight across projects. No native OpenClaw equivalent exists.

```markdown
# Active Tasks

## worldarchitect.ai
- [ ] Get first 100 users
  - [ ] Launch landing page
  - [ ] Beta invite flow
  - [ ] Polish core game loop
- [ ] PR #2162: Gemini 3 upgrade

## openclaw
- [x] Register Genesis on MCP Mail
- [x] Set up IDENTITY.md
- [ ] Fill in USER.md, MEMORY.md, TOOLS.md
- [ ] Configure memorySearch.extraPaths
- [ ] Add MEMORY.md curation cron job
```

## What Was Wrong in V1 of This Design

For honesty/learning:

1. **"Generate MEMORY.md from sqlite"** — Backwards. MEMORY.md is the source; sqlite indexes it.
2. **`genesis/handoff.md`** — Redundant with `memory/YYYY-MM-DD.md` daily logs.
3. **`genesis/projects/*.md`** — Mostly redundant with `memorySearch.extraPaths`.
4. **`scripts/memory-regen.sh`** — Not needed. Write MEMORY.md directly.
5. **`scripts/session-summarize.sh`** — Memory flush before compaction already does this.
6. **"Feed coding decisions into sqlite"** — Wrong direction. Write markdown, sqlite auto-indexes.

## Implementation Plan

### Phase 1: Fill In What Exists (immediate)
- [ ] Create `~/.openclaw/workspace/MEMORY.md` with curated knowledge
- [ ] Fill in `~/.openclaw/workspace/USER.md` with user context
- [ ] Fill in `~/.openclaw/workspace/TOOLS.md` with local env details
- [ ] Update `SOUL.md` to reference "Genesis" identity
- [ ] Write today's `memory/2026-02-25.md` daily log

### Phase 2: Tune Config
- [ ] Add `memorySearch.extraPaths` to `openclaw.json` for worldarchitect.ai
- [ ] Enable `temporalDecay` and `mmr` in memory search
- [ ] Enable `experimental.sessionMemory` for session transcript indexing

### Phase 3: Automation (optional)
- [ ] Add MEMORY.md curation cron job (weekly)
- [ ] Create `tasks.md` in workspace for cross-project tracking
- [ ] Define MCP Mail protocols for subagent reporting

## Comparison with External Orchestrators

| Feature | Genesis (configure OpenClaw) | Mission Control | Command Center |
|---------|------------------------------|-----------------|----------------|
| **Approach** | Fill in existing files + config | Next.js dashboard | Vanilla JS dashboard |
| **New dependencies** | None | Node.js, Next.js | None (200KB) |
| **New running processes** | None | Node server (~150MB) | Static file server |
| **Context persistence** | Native OpenClaw memory | Database-backed | SSE + localStorage |
| **Setup time** | Minutes (write markdown) | Hours (deploy app) | Minutes (serve files) |
| **Best for** | Solo dev, CLI-native | Team with visual needs | Monitoring + cost |

Genesis is the **zero-overhead option** — it doesn't add anything to run. It just uses
what OpenClaw already built, properly.

## Open Questions

1. **SOUL.md customization**: Should we replace the generic SOUL.md with a Genesis-specific one, or keep it as-is and put project-specific identity in MEMORY.md?
2. **extraPaths scope**: How many project files to index? Just CLAUDE.md + learnings.md, or broader?
3. **Curation frequency**: Is weekly MEMORY.md curation enough, or should it be daily?
4. **Session memory**: The experimental session transcript indexing — worth enabling?
