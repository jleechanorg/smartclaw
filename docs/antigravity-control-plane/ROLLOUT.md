# Antigravity Control Plane — Rollout Plan

**Bead:** `ORCH-ag1`
**Status:** Design in progress

---

## Rollout Overview

Four sequential versions with explicit go/no-go gates between each phase.

```
v0 ────► v1 ────► v2 ────► v3
         │         │         │
         ▼         ▼         ▼
      Design    AO+MCP   Hardening
      merged     Mail     + observability
                 integration
```

---

## v0 — Design Merge (THIS PR)

**Goal:** Commit design artifacts to `jleechanorg/smartclaw` main.

### Go Criteria

- [ ] All 7 design documents written and reviewed
- [ ] No conflicting decisions with existing smartclaw architecture
- [ ] ORCH-ma4 and existing Antigravity skill referenced correctly
- [ ] Branch `feat/antigravity-control-plane-design` opened as PR

### No-Go Conditions

- Any design document references a capability not confirmed to exist in the Antigravity skill
- Design conflicts with existing `src/orchestration/` patterns (must be consistent)
- Missing any required deliverable (OVERVIEW, ARCHITECTURE, DATA-CONTRACTS, TDD-PLAN, UI-INVENTORY, ROLLOUT, DECISIONS)

### What Gets Merged

```
docs/antigravity-control-plane/
  OVERVIEW.md
  ARCHITECTURE.md
  DATA-CONTRACTS.md
  TDD-IMPLEMENTATION-PLAN.md
  UI-CAPABILITIES-INVENTORY.md
  ROLLOUT.md
  DECISIONS.md
```

**No runtime code** — v0 is design only.

---

## v1 — Minimal Control Plane Skeleton

**Goal:** Runnable control plane with no real AO or Antigravity integration.

### What Gets Built

| File | Description | Testing |
|---|---|---|
| `src/orchestration/antig_control_plane/__init__.py` | Package init | `PYTHONPATH=src python -c "from src.orchestration import antig_control_plane"` |
| `src/orchestration/antig_control_plane/models.py` | Job, State, Priority dataclasses | Unit tests (Phase 1) |
| `src/orchestration/antig_control_plane/db.py` | SQLite schema + migrations | Unit tests |
| `src/orchestration/antig_control_plane/scheduler.py` | Enqueue, dedupe, list, update | Unit tests (Phase 1) |
| `src/orchestration/antig_control_plane/global_lock.py` | flock-based singleton lock | Unit tests (Phase 3) |
| `src/orchestration/antig_control_plane/dispatcher.py` | Poll + dispatch (mock AO) | Unit tests (Phase 2) |
| `src/orchestration/antig_control_plane/ao_adapter.py` | MCP Mail format builder + mock | Unit + integration tests (Phase 2) |
| `src/orchestration/antig_control_plane/recovery_loop.py` | Stale job detection + re-enqueue | Unit tests (Phase 3) |
| `src/orchestration/antig_control_plane/reporter.py` | Outcome ledger + Slack | Unit tests (Phase 4) |
| `src/orchestration/antig_control_plane/cli.py` | CLI: enqueue, list, cancel | Manual smoke test |
| `src/tests/test_antig_control_plane/` | Full TDD suite (note: repo pytest is configured with `testpaths = ["tests"]`; use `PYTHONPATH=src python -m pytest src/tests/` from repo root) | All tests pass |
| `launchd/ai.smartclaw.antig-control-plane.plist` | LaunchAgent for daemon | Manual load test |

### Go Criteria (v1 → v2 gate)

- [ ] All TDD phases 1-4 tests pass
- [ ] Integration smoke test: enqueue → dispatch loop with mock AO works
- [ ] CLI enqueue + list + cancel all function correctly
- [ ] LaunchAgent starts, acquires lock, and stays alive
- [ ] LaunchAgent fails to start if another instance is already running
- [ ] Second LaunchAgent attempt logs clear error about lock contention
- [ ] No peeking at Antigravity or AO required for v1 tests

### No-Go Conditions

- Any TDD phase has failing tests
- Lock mechanism has a race condition
- Jobs can be dispatched twice (idempotency failure)
- Recovery loop re-enqueues jobs that should be deadlettered

---

## v2 — AO + MCP Mail Integration

**Goal:** Real Antigravity jobs executed by AO Sonnet workers.

### What Gets Built

| Component | Description | Testing |
|---|---|---|
| `src/orchestration/antig_control_plane/executor.py` | Peekaboo job execution loop | Integration tests (requires Peekaboo) |
| `src/orchestration/antig_control_plane/ao_adapter.py` (upgrade) | Real MCP Mail send/receive | Integration tests with stubbed AO |
| AO worker template | Script that receives job, runs Antigravity skill, reports result | Manual end-to-end test |
| Worktree manager | Creates/resets worktrees per job | Unit tests |
| Heartbeat manager | Sends periodic heartbeats from executor to control plane | Integration tests |
| Blocker resolution UI | Human resolves BLOCKED job via Slack or CLI | Manual test |

### Go Criteria (v2 → v3 gate)

- [ ] One real Antigravity job completes end-to-end: enqueue → dispatch → execute → complete
- [ ] PR created by Antigravity worker is verifiable on GitHub
- [ ] Recovery loop correctly re-enqueues a killed worker
- [ ] BLOCKED jobs are reported via Slack with exact blocker description
- [ ] Per-repo exclusivity verified: two jobs for same repo are not dispatched simultaneously
- [ ] AO worker logs show screenshot evidence for each step
- [ ] Outcome ledger contains complete record of job execution

### No-Go Conditions

- Antigravity crashes and cannot be restarted automatically
- AO worker dies mid-job with no recovery
- Job cancellation does not stop running Antigravity operations
- Structured output (PR URL, etc.) cannot be extracted reliably

### Known Risks at v2

| Risk | Probability | Mitigation |
|---|---|---|
| Antigravity UI selector breaks | Medium | Pin Peekaboo version; add regression test |
| AO worker session timeout | Medium | Reduce job timeout to 60 min; checkpoint progress |
| Blocked jobs accumulate | Medium | Slack alert for BLOCKED > 1 hour |
| Two workers race for same worktree | Low (enforced by dispatcher) | Add worktree lock file in addition to dispatcher check |

---

## v3 — Hardening and Observability

**Goal:** Production-ready control plane with full observability.

### What Gets Built

| Component | Description |
|---|---|
| Metrics | Prometheus-compatible `/metrics` endpoint for job throughput, latency, failure rate |
| Alerting | PagerDuty/Slack alerts for deadlettered jobs, lock contention, Peekaboo permission loss |
| Rate limiting | Executor backs off when Antigravity shows rate-limit UI |
| Session checkpointing | Executor periodically snapshots Antigravity state for faster resume |
| Automatic Peekaboo permission monitoring | Background check for Accessibility + Screen Recording permissions |
| Deadletter audit | Weekly report of deadlettered jobs with root cause analysis |
| Dashboard | Grafana board: job throughput, queue depth, worker utilization, error rate |

### Go Criteria (v3 — production)

- [ ] 95th percentile job completion time < 2 hours
- [ ] Deadletter rate < 1% over 30-day window
- [ ] Mean time to recovery from worker crash < 10 minutes
- [ ] Zero cases of two workers operating on same worktree simultaneously
- [ ] All deadlettered jobs have human-readable root cause in ledger
- [ ] Peekaboo permission loss detected and alerted within 5 minutes

---

## Rollout Sequence (Real Environment)

### Week 1: v0 Design Merge
- [ ] Review design with jleechan
- [ ] Merge `feat/antigravity-control-plane-design` PR
- [ ] Sonnet workers briefed on TDD implementation plan

### Week 2-3: v1 Skeleton
- [ ] Implement Phase 1 (models, scheduler, db)
- [ ] Implement Phase 2 (dispatcher, ao_adapter mock)
- [ ] Implement Phase 3 (global lock, recovery)
- [ ] Implement Phase 4 (reporter, ledger)
- [ ] Write CLI + LaunchAgent
- [ ] Pass all go criteria
- [ ] Open v1 PR

### Week 4: v2 AO Integration
- [ ] Implement executor with real Peekaboo
- [ ] Configure AO workers with Antigravity skill
- [ ] End-to-end smoke test: enqueue → Antigravity creates PR
- [ ] Recovery loop test: kill worker, verify re-enqueue
- [ ] Open v2 PR

### Week 5-6: v3 Hardening
- [ ] Add metrics endpoint
- [ ] Configure Slack alerting
- [ ] Write Grafana dashboard
- [ ] 30-day production run begins (runs concurrently; no explicit end date — continues until v3 success criteria are met)
- [ ] Open v3 PR (if needed as separate PR, or merge into v2)

---

## Feature Flags

For canary rollout within v2:

| Flag | Default | Description |
|---|---|---|
| `antig_control_plane.enabled` | `false` | Master kill switch |
| `antig_control_plane.repos` | `[]` | List of allowed repos; empty = all repos allowed |
| `antig_control_plane.max_concurrent_per_repo` | `1` | Max concurrent jobs per repo |
| `antig_control_plane.ao_adapter.mock` | `true` in v1, `false` in v2 | Use mock AO adapter |
| `antig_control_plane.recovery.auto_reenqueue` | `true` | Auto re-enqueue stale jobs vs. deadletter immediately |
| `antig_control_plane.slack.notify.completed` | `true` | Send Slack notification on job completion |
| `antig_control_plane.slack.notify.deadlettered` | `true` | Send alert on deadletter (pager-worthy) |

Feature flags are set in `openclaw.json` under `antig_control_plane:` section, following the surgical update pattern (never rewrite full file).
