---
name: smartclaw-eloop
description: Custom evolve loop for smartclaw orchestrator — drains dropped Slack thread backlog via /claw, fixes openclaw issues, proposes new work items. Max 50 items, newest-first.
type: skill
---

## Purpose

This is the **smartclaw**-specific backlog eloop **discovery entry** (distinct from `.claude/skills/evolve_loop/SKILL.md`). Use this file as a pointer to the canonical procedure rather than the authoritative source itself.

**Authoritative procedure** (full phases, bash snippets, Slack channels): read **`skills/smartclaw-eloop.md`** at the repository root of the harness checkout.

After `scripts/bootstrap.sh`, the same text is available at **`~/.smartclaw/skills/smartclaw-eloop.md`** (symlink to the repo file).

AO `orchestratorRules` and project `agentRules` reference the runtime path and/or repo `skills/smartclaw-eloop.md`; this `.claude/skills/.../SKILL.md` exists so Claude Code discovery finds the eloop. Agents and operators should follow the procedure in the authoritative file/path above.

**Related:** `agent-orchestrator.yaml` — **CUSTOM ELOOP — BACKLOG PROCESSOR (smartclaw)** summary and per-cycle limits.
