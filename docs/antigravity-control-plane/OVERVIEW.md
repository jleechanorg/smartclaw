# Antigravity Control Plane — Overview

**Bead:** `ORCH-ag1`
**Status:** Design in progress
**Created:** 2026-03-24
**Owner:** jleechan (harness design)
**Build phase:** v0 design → v1 skeleton → v2 AO integration → v3 hardening

---

## What This Is

A **singleton super-orchestrator** that drives Google Antigravity IDE across multiple repositories and worktrees, coordinated from `jleechanorg/smartclaw` (the OpenClaw harness), with Agent Orchestrator (AO) workers dispatched via MCP Mail.

Antigravity is a GUI-based AI coding environment on macOS. The Antigravity skill already enables single-session control via Peekaboo CLI against the Manager window. This design extends that into a **multi-repo, multi-worker control plane** with durable job queuing, global locking, and AO integration.

---

## Problem Statement

Today, Antigravity is operated **manually per session**:
- One human or agent starts one Antigravity workspace for one repo.
- No coordination across repos.
- No durable job queue.
- No global safety (two workers could stomp on the same worktree).
- No integration with AO workers (which are CLI-based, not GUI-based).

The Antigravity skill (`~/.claude/skills/antigravity-computer-use/SKILL.md`) provides a solid foundation for the single-session GUI automation loop. ORCH-ma4 (`roadmap/PEEKABOO_ANTIGRAVITY_UI_AUTOMATION.md`) established the Peekaboo bridge and preflight checks. This design builds the **orchestration layer** on top.

---

## Constraints

| Constraint | Rationale |
|---|---|
| Control plane lives in `smartclaw`, not `agent-orchestrator` | smartclaw is the harness/home; agent-orchestrator is AO-core only |
| Antigravity is controlled via Peekaboo, not native API | No native Antigravity API confirmed; Peekaboo is the established automation layer |
| Singleton controller — never two writers | Antigravity Manager window is single-threaded from the user's perspective |
| AO workers are stateless job executors | AO workers receive job payloads via MCP Mail, execute against Antigravity UI, report results |
| Jobs are opaque to the control plane | Control plane routes and coordinates; AO workers own the execution logic |
| All state in SQLite + JSONL | Consistent with existing smartclaw patterns (see `src/orchestration/webhook.py`) |
| Design-only PR; no runtime code in v0 | Sonnet workers build the implementation in follow-up phases |

---

## Non-Goals

- **Not** a replacement for AO core dispatch logic — AO keeps its PR polling, merge gate, and supervisor.
- **Not** a native Antigravity API client — Peekaboo is the automation substrate.
- **Not** a cross-platform solution — macOS + Antigravity on macOS is the only target.
- **Not** a general-purpose job queue — scope is Antigravity job dispatch only.

---

## Why the Control Plane Lives in smartclaw

smartclaw is the **harness repo** — the home for all orchestration, automation, and coordination logic that operates across projects. It already contains:

- `src/orchestration/` — Python orchestration layer (webhook, evidence, merge gate, AO integration)
- `~/.smartclaw/cron/jobs.json` — scheduled jobs via OpenClaw gateway cron (live definitions, not tracked in repo)
- `skills/` — agent skills (including the Antigravity shim)
- `launchd/` — macOS launch agent templates

The Antigravity control plane fits this mission: coordinate multi-repo work across the harness environment. AO provides the worker execution infrastructure; smartclaw provides the **coordination and policy layer**.

---

## Why the AO Adapter Is Thin

The AO adapter is the MCP Mail bridge between the smartclaw control plane and AO workers. It is intentionally thin because:

1. **AO owns worker lifecycle** — spawning, supervising, respawning. The control plane should not replicate this.
2. **AO has its own event model** — AO workers emit events; the adapter translates them to control plane events.
3. **Separation of concerns** — control plane handles routing, locking, and policy; AO handles execution.
4. **Avoids coupling** — a thin adapter can be replaced or stubbed without breaking the control plane.

The thick boundary is at **MCP Mail messages**: well-defined JSON schemas for job dispatch and result reporting. The adapter translates to/from those schemas.

---

## Relationship to Existing Artifacts

| File | Relationship |
|---|---|
| `skills/antigravity-computer-use/SKILL.md` | Canonical single-session skill (added in this PR); control plane dispatches jobs that invoke this skill |
| `roadmap/PEEKABOO_ANTIGRAVITY_UI_AUTOMATION.md` (ORCH-ma4) | Established Peekaboo bridge and preflight checks; control plane requires these prerequisites |
| `roadmap/AGENT_ORCHESTRATOR_MCP_MAIL_INTEGRATION.md` | Defines MCP Mail contract that the AO adapter implements against |
| `src/orchestration/mcp_mail.py` | Retired/removed; AO adapter (see `ao_adapter.py` in this design) replaces this stub |
| `src/orchestration/webhook.py` | Patterns for SQLite deduplication, HMAC validation, bounded retries — control plane adopts these |

---

## What Lives Where

```
smartclaw/
  docs/antigravity-control-plane/    ← NEW (this design)
    OVERVIEW.md                       this file
    ARCHITECTURE.md                   component design, safety model, routing
    DATA-CONTRACTS.md                 job schema, state machine, idempotency
    TDD-IMPLEMENTATION-PLAN.md        phased test-first build plan
    UI-CAPABILITIES-INVENTORY.md       what the Antigravity skill supports today
    ROLLOUT.md                        version gates and rollout phases
    DECISIONS.md                      ADR-style choices

  src/orchestration/
    antig_control_plane/              ← v1+ implementation (Sonnet workers)
      __init__.py
      scheduler.py                    job queue, priority, enqueue API
      dispatcher.py                   MCP Mail job dispatch to AO workers
      executor.py                     Peekaboo job execution loop (per-worker)
      global_lock.py                  singleton safety (file lock + lease)
      recovery_loop.py                stale job detection and re-queue
      reporter.py                     outcome recording, Slack notification
      ao_adapter.py                   MCP Mail ↔ control plane translation
      models.py                       dataclasses for jobs, states, events
      tests/                          TDD test suite

  skills/antigravity-computer-use/SKILL.md   ← added in this PR (v0)
```

---

## Key Risks

| Risk | Mitigation |
|---|---|
| Antigravity UI changes break Peekaboo selectors | Lock selector versions; add automated regression test for Manager window |
| Two control planes race for global lock | Singleton enforced at OS level (flock); control plane refuses to start if lock held |
| AO worker dies mid-job, Antigravity left in bad state | Recovery loop detects stale `running` jobs; recovery policy = kill + re-enqueue |
| MCP Mail delivery failure | Control plane retries with exponential backoff; idempotency key prevents double-dispatch |
| Peekaboo permissions revoked | Preflight check at startup and before each job; alert via Slack if permissions missing |
