# jleechanclaw Orchestration Design

> North star vision: OpenClaw replaces Jeffrey in the autonomous development loop.
> Jeffrey only sees work when it genuinely needs human judgment or final approval.
>
> **Last updated:** 2026-03-14

---

## The Stack

```
  Jeffrey (human)
       │  ▲
       │  │ escalation only (budget exhausted, product ambiguity, risky change)
       ▼  │
  OpenClaw (LLM brain — replaces Jeffrey in the loop)
  ├── memory: project memories, feedback, prior patterns (mem0/qdrant)
  ├── judgment: deterministic rules first, LLM for ambiguity
  ├── learning: outcome ledger feeds next decision
  └── webhook: receives AO escalations on port 9100
       │
       ▼
  agent-orchestrator (AO) — lifecycle state machine + reaction engine
  ├── session manager: spawn, send, kill, liveness
  ├── scm-github: PR detection, CI parsing, merge readiness
  └── reactions: ci-failed, changes-requested, approved-and-green, agent-stuck
       │
       ▼
  Headless agents (claude -p, codex, gemini — fresh context each call)
```

OpenClaw sits above the AO loop. It has persistent memory and can make the same
decisions Jeffrey would make, looping autonomously until work is done or a genuine
human judgment is needed.

**Implementation boundary:** no changes to AO code. All new logic lives in
`src/orchestration/` (Python). AO is consumed via:
- `notifier-openclaw` plugin (AO → OpenClaw escalation webhooks)
- `ao` CLI commands (OpenClaw → AO: `ao spawn`, `ao send`, `ao kill`)

---

## Core Principle: Deterministic First, LLM for Judgment

AO handles the predictable 80% deterministically. OpenClaw handles the 20% that
requires judgment. The LLM is not called for routine reactions.

| Signal | Handler | Decision maker |
|--------|---------|----------------|
| CI failed ≤ retry cap | `ci-failed` → `send-to-agent` | AO deterministic |
| Review comment received | `changes-requested` → `send-to-agent` | AO deterministic |
| PR approved + CI green | `approved-and-green` → OpenClaw review | AO triggers, OpenClaw reviews |
| Agent stuck > threshold | `agent-stuck` → kill + respawn | OpenClaw deterministic |
| CI failed, budget ≥ 2 left, parseable error | parallel retry (2-3 strategies) | OpenClaw deterministic |
| Retry budget exhausted | escalate with failure summary | OpenClaw → Jeffrey |
| Vague review needing interpretation | interpret + dispatch fix | OpenClaw LLM |
| New feature request → subtask decomposition | plan + spawn parallel sessions | OpenClaw LLM |
| "Should we abandon this approach?" | strategy decision | OpenClaw LLM |
| Risky change in sensitive path | explicit escalation + warning | OpenClaw LLM |

When OpenClaw makes a judgment call, it returns a **confidence score (0–1)**.
If confidence < `min_confidence` (default 0.6), auto-escalate to Jeffrey instead
of acting. This prevents silent bad decisions on ambiguous cases.

---

## Webhook Ingress Pipeline

AO's notifier-openclaw plugin posts escalation webhooks to `localhost:9100/webhook`
(the webhook daemon). The orchestration layer handles these with
proper queue durability and worker isolation:

```
AO notifier-openclaw plugin
  ↓ POST http://localhost:9100/webhook
webhook_ingress (HTTP server, HMAC validate, SQLite queue)
  ↓ row inserted into webhook_deliveries
webhook_worker (polling loop, PR lock, bounded retries)
  ↓ dequeue → normalize
ao_events.parse_ao_webhook → AOEvent
  ↓
escalation_handler.handle_escalation
  ├── escalation_router.route_escalation (deterministic rules)
  └── action_executor.execute_action (ao_cli / slack notifier)
  ↓
action_log.jsonl + failure_budget.json (audit trail)
```

**Status:** Full pipeline live as of 2026-03-15:
- GitHub webhooks → `/webhook` (HMAC) → ao_spawn: **E2E proven** (`processed=1, failed=0`)
- AO escalations → `/ao-notify` (Bearer) → escalation_handler: **E2E proven** (`RetryAction` routed)
- AO `notifier-orchestrator` in `~/agent-orchestrator.yaml` points to port 19888 (bead orch-eh15)
- `ao_events.parse_ao_webhook` handles both native AO format and flat format

**Idempotency:** `X-GitHub-Delivery` header used as delivery_id primary key —
re-delivery is a no-op at the ingress layer.

---

## Escalation Routing

`escalation_router.py` applies deterministic rules first; falls back to
`NeedsJudgmentAction` only when no rule matches.

```
reaction.escalated (ci-failed, changes-requested)
  ├── attempts ≤ max_retries AND parseable CI error AND budget ≥ 2
  │     → ParallelRetryAction (spawn 2-3 parallel fix sessions)
  ├── attempts ≤ max_retries (single retry)
  │     → RetryAction (ao send enriched prompt)
  └── attempts > max_retries
        → NotifyJeffreyAction (budget exhausted)

session.stuck
  → KillAndRespawnAction (ao kill + ao spawn)

merge.ready
  → auto_review_trigger → OpenClaw reviews PR
    ├── approve → NotifyJeffreyAction ("ready to merge")
    ├── request_changes → RetryAction (dispatch fix agent)
    └── escalate → NotifyJeffreyAction ("needs your eyes")

unknown event
  → NotifyJeffreyAction (fail-safe)
```

---

## Parallel Retries (Phase 3.5)

When CI fails and the retry budget has ≥ 2 attempts remaining, OpenClaw spawns
**multiple parallel AO sessions** — each with a different fix strategy injected
into its prompt. First session to get CI green wins; the rest are killed.

```
ci-failed + parseable error + budget ≥ 2
  ↓
generate_fix_strategies(ci_failure, diff, max_strategies=3)
  → [FixStrategy("check imports"), FixStrategy("fix type signature"), ...]
  ↓
For each strategy: ao spawn on separate worktree/branch
  ↓
while not timeout:
  poll all sessions for CI status
  first green → winner; kill rest
  all red → escalate
```

**Why:** Three sequential retries = 15-30 min. Three parallel = 5-10 min.
For a solo developer, wall-clock time is the scarce resource.

**Status:** implemented (`parallel_retry.py`). `check_ci_status` queries
GH Actions via `gh run list` — bead `orch-mner` **IMPLEMENTED**.

**Outcome recording:** winning strategies logged to `outcomes.jsonl` by error
class fingerprint. Future: skip speculation when a known fix exists.

---

## Failure Budgets

Three tiers of budget tracking:

| Tier | Owner | Limit | What happens on exhaust |
|------|-------|-------|------------------------|
| Per-session | AO natively | `retries: 2` in reaction config | AO escalates to OpenClaw |
| Per-subtask | `failure_budget.py` | 30 min from first escalation | OpenClaw escalates to Jeffrey |
| Per-task | `failure_budget.py` | 2 strategy changes | OpenClaw escalates to Jeffrey |

`PersistentFailureBudget` is file-backed JSON with atomic writes and `fcntl` locking —
survives process restarts. Beads `orch-mner` (CI polling) and `orch-u8gy` (webhook daemon)
are now **IMPLEMENTED**.

---

## Autonomous PR Review (Phase 6)

Before Jeffrey sees a PR, OpenClaw reviews it:

```
merge.ready event
  ↓
build_review_context(pr_number)
  ├── gh api: diff, commits, CI status, CodeRabbit reviews
  ├── CLAUDE.md: repo-level + global rules
  ├── memory: project memories, feedback memories, prior approval patterns
  └── action_log.jsonl: prior decisions on same repo
  ↓
review_pr(context) → LLM call (claude-sonnet-4-6)
  ↓
ReviewDecision:
  ├── approve   → post GH review + NotifyJeffrey "ready to merge"
  ├── changes   → post GH review + dispatch fix agent via ao send
  └── escalate  → NotifyJeffrey "needs your eyes" + attach review notes
```

**Design principle:** the LLM gets everything and decides. No keyword matching,
no path globs, no hardcoded thresholds. CLAUDE.md rules, memory, prior patterns —
all injected as context. The LLM applies them through inference.

**Idempotency:** reviewed PRs tracked in `reviewed_prs.json`. Same PR not
reviewed twice within a session.

**Status:** scaffolded (`pr_reviewer.py`, `pr_review_decision.py`,
`auto_review_trigger.py`). `_call_llm` now wires to Claude API —
bead `orch-gwli` **IMPLEMENTED**.

---

## Memory Integration

OpenClaw's judgment calls use three memory tiers:

| Memory type | Source | Used for |
|-------------|--------|----------|
| **Project memories** | `~/.openclaw/memory/project/` | Codebase conventions, known patterns, historical decisions |
| **Feedback memories** | `~/.openclaw/memory/feedback/` | What Jeffrey corrected — shapes judgment and review |
| **Outcome ledger** | `~/.openclaw/state/outcomes.jsonl` | Which fix strategies won/lost by error class |

In `review_pr`: all three memory tiers are loaded into `ReviewContext` before the
LLM call. The LLM uses them as criteria, not the code.

In `route_escalation`: `failure_budget.get_attempts()` is the only memory read
today. ORCH-cil (Convergence Intelligence Layer) will add anomaly detection and
learned escalation tiers.

---

## Outcome Ledger + Self-Improving Prompts (ORCH-qvd / ORCH-04k)

Every agent spawn today is a fresh guess. The outcome ledger closes the learning loop:

```
agent session completes
  ↓
outcome_recorder.record_outcome(error_class, winner_strategy, loser_strategies)
  → outcomes.jsonl: {error_class, winning_strategy, timestamp, session_id}
  ↓
pattern_synthesis (ORCH-qvd) — cron job
  → "for ImportError on Django models, approach-2 (add migration) wins 80%"
  ↓
generate_fix_strategies (ORCH-04k)
  → seed with known-winning strategies for error class before speculating
```

**Status:** `outcome_recorder.py` implemented. Pattern synthesis cron (`ORCH-qvd`)
is implemented; strategy seeding (`ORCH-04k`) remains not started.

---

## Autonomous Completion Contract

The system succeeds when OpenClaw can run the full loop from intent to
merge-readiness, replacing Jeffrey in all but final approval.

### Loop Invariant

For every active task:
1. AO dispatches headless agent with fresh context + failure summary injected
2. AO detects PR/CI/review state via `scm-github` plugin
3. AO auto-remediates CI/review failures deterministically (≤ 2 retries)
4. On escalation: OpenClaw routes → parallel retry, respawn, or escalate to Jeffrey
5. On merge-ready: OpenClaw reviews PR autonomously
6. Repeat until merge-readiness gates pass or Jeffrey is genuinely needed

### Merge-Readiness Gates

A PR is `ready/mergeable` only when ALL are true:
1. AO `getMergeability()` passes (CI green, approved, no conflicts)
2. CodeRabbit approved or rate-limited (rate-limited is acceptable)
3. OpenClaw PR review: `approve` decision (or Jeffrey explicitly approved)

### Escalation to Jeffrey (Stop Autonomy)

OpenClaw must escalate when:
1. Retry + judgment budget exhausted — multiple strategies failed
2. Product-level ambiguity — scope change, conflicting requirements, risky tradeoff
3. Missing credentials/permissions OpenClaw cannot obtain
4. Confidence score < `min_confidence` (0.6) on a judgment call
5. Sensitive-path change detected in PR review (security, auth, credentials)

### Subtask Decomposition

For large tasks, OpenClaw decomposes into independent subtasks:

```
"implement user auth for the API"
  ↓ OpenClaw LLM decomposition
  ├── subtask 1: "add JWT middleware to api/auth.py"
  ├── subtask 2: "add login/register endpoints"
  └── subtask 3: "add auth tests"
  ↓
DecompositionDispatcher: spawn AO session per subtask (max 4 parallel)
  Each session has own reaction loop + failure budget
  Cross-task file conflicts surface at merge via github merge-conflicts state
  ↓
OpenClaw monitors all sessions via SSE events
  handles cross-task conflicts (deterministic: notify Jeffrey)
  handles subtask exhaustion (deterministic: escalate to Jeffrey)
```

**Status:** `task_tracker.py` + `decomposition_dispatcher.py` implemented.
SSE event monitoring (`ORCH-cvg`) is implemented.

---

## Implementation Status

| Component | Module | Status |
|-----------|--------|--------|
| AO event parser | `ao_events.py` | ✅ Done (PR #134) |
| AO CLI wrapper | `ao_cli.py` | ✅ Done (PR #134) |
| Escalation router | `escalation_router.py` | ✅ Done (PR #134) |
| Failure budget (persistent) | `failure_budget.py` | ✅ Done (PR #134) |
| Action executor | `action_executor.py` | ✅ Done (PR #134) |
| Parallel retry (strategy + loop) | `parallel_retry.py` | ✅ Done (PR #134) |
| Outcome recorder | `outcome_recorder.py` | ✅ Done (PR #134) |
| CodeRabbit gate | `coderabbit_gate.py` | ✅ Done (PR #134) |
| Task tracker | `task_tracker.py` | ✅ Done (PR #134) |
| Decomposition dispatcher | `decomposition_dispatcher.py` | ✅ Done (PR #134) |
| Escalation handler (wiring) | `escalation_handler.py` | ✅ Done (PR #134) |
| PR review context builder | `pr_reviewer.py` | ✅ Done (PR #134) |
| PR review decision (LLM) | `pr_review_decision.py` | ✅ Done (orch-gwli) |
| Auto-review trigger | `auto_review_trigger.py` | ✅ Done |
| Webhook ingress | `webhook_ingress.py` | ✅ Done (PR #134) |
| Webhook worker | `webhook_worker.py` | ✅ Done (PR #134) |
| **check_ci_status (real)** | `parallel_retry.py` | ✅ Done (orch-mner) |
| **_call_llm wiring** | `pr_review_decision.py` | ✅ Done (orch-gwli) |
| **Webhook daemon** | `webhook_daemon.py` + plist | ✅ Done (orch-u8gy) |
| Pattern synthesis cron | `pattern_synthesizer.py` | ✅ Done (ORCH-qvd) |
| SSE event monitoring | `subtask_events.py` | ✅ Done (ORCH-cvg) |
| **GitHub → ao_spawn dispatch (E2E)** | `webhook_worker.py` + plist PATH/cwd fix | ✅ Done (2026-03-15) |
| **AO /ao-notify ingress + Bearer auth** | `webhook_ingress.py` | ✅ Done (orch-eh15) |
| **AO native format support** | `ao_events.py` — nested `event.type` | ✅ Done (orch-eh15) |
| **AO → escalation_handler routing** | `webhook_daemon._is_ao_webhook` | ✅ Done (orch-pz9d) |
| **AO notifier wired to port 19888** | `~/agent-orchestrator.yaml` `orchestrator` notifier | ✅ Done (orch-eh15) |
| Self-improving prompts | — | 🔴 Not started (orch-4vl8) |
| Auto-triage GitHub notifications | — | 🔴 Not started (orch-w3i1) |
| Convergence Intelligence Layer | — | 🔴 Not started (orch-cn8y) |
| Security-sensitive approval gates | — | 🔴 Not started (orch-3nmu) |

**Test coverage:** 429 unit tests passing across all ✅ Done modules.
**E2E proven:** GitHub webhook → `Worker processed=1, failed=0` (2026-03-15).
**E2E proven:** AO escalation → `/ao-notify` → `RetryAction` routing (2026-03-15).

---

## Open Beads

| Bead | Title | Blocks |
|------|-------|--------|
| `orch-mner` | `check_ci_status` — real GH Actions polling | ✅ Done |
| `orch-gwli` | `_call_llm` — Claude API wiring | ✅ Done |
| `orch-u8gy` | Webhook daemon + LaunchAgent plist | ✅ Done |
| ORCH-cvg | Convergence gateway — SSE event stream | ✅ Done |
| ORCH-qvd | Outcome Ledger + pattern synthesis cron | ✅ Done |
| orch-eh15 | Wire AO /ao-notify → webhook_daemon port 19888 | ✅ Done |
| orch-pz9d | E2E test AO escalation path → escalation_handler routing | ✅ Done |
| orch-4vl8 | Self-improving prompts (seed from outcomes) | Faster CI fixes |
| orch-cn8y | Convergence Intelligence Layer — anomaly detection | Smarter escalation |
| orch-w3i1 | Auto-triage GitHub notifications → AO sessions | Proactive dispatch |
| orch-3nmu | Security-sensitive approval gates | Production safety |

---

## Superpowers (Future)

### Parallel Agent Swarms (ORCH-6k1)
Subtask decomposition already spawns parallel sessions. Next: smarter
conflict detection across worktrees before merge, cross-task dependency
tracking.

### Self-Improving Prompts (ORCH-04k)
Outcome ledger feeds a project-specific fix strategy library. Over time,
`generate_fix_strategies` seeds with known-winning approaches before
speculating. "The system that's never seen a React import error before is
dumb. The system that's fixed 50 of them has a playbook."

### Auto-Triage Notifications (ORCH-2oy)
OpenClaw scans GitHub notifications across repos, triages into AO sessions
automatically. Jeffrey stops looking at GitHub; the system surfaces only
decisions that need human judgment.

### Convergence Intelligence Layer (ORCH-cil)
Anomaly detection on escalation patterns. If the same error class is
escalating repeatedly, ORCH-cil detects the pattern and creates a bead
before Jeffrey asks what's wrong. Learns escalation thresholds per-project
from historical data.

---

## Why Not Gastown

Gastown is Steve Yegge's Go-based multi-agent workspace manager: 348,530 lines,
63 internal packages. Evaluated and rejected.

AO does the same lifecycle management in ~3,000 lines of TypeScript that matter.
The Gastown concept of persistent work state surviving agent restarts is valid —
AO's session metadata covers it without the ceremony.

Full comparison: `docs/ORCHESTRATION_RESEARCH_2026.md`.

---

## File Locations

| What | Where |
|------|-------|
| This design doc | `~/.openclaw/roadmap/ORCHESTRATION_DESIGN.md` |
| Implementation roadmap (detailed TDD plan) | `~/.openclaw/roadmap/ORCHESTRATION_IMPL_ROADMAP.md` |
| E2E test plan | `~/.openclaw/testing_llm/e2e_orchestration_webhook.md` |
| AO config | `~/agent-orchestrator.yaml` |
| AO source | `~/projects_reference/agent-orchestrator/` |
| AO-OpenClaw integration | `~/projects_reference/agent-orchestrator/DESIGN-OPENCLAW-PLUGIN.md` |
| Symphony webhook design | `~/.openclaw/roadmap/SYMPHONY_WEBHOOK_PR_REMEDIATION_DESIGN.md` |
| Outcome Ledger design | `~/.openclaw/roadmap/OUTCOME_LEDGER_DESIGN.md` |
| Research & alternatives | `~/.openclaw/docs/ORCHESTRATION_RESEARCH_2026.md` |
