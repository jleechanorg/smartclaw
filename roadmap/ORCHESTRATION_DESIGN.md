# Orchestration Design

> North star vision: AI agent orchestration replaces human in the autonomous development loop.
> Human only sees work when it genuinely needs human judgment or final approval.
>
> **Last updated:** 2026-03-14
>
> **Source**: Adapted from jleechanorg/jleechanclaw/roadmap/ORCHESTRATION_DESIGN.md

---

## The Stack

```
Human (developer)
       │  ▲
       │  │ escalation only (budget exhausted, product ambiguity, risky change)
       ▼  │
  LLM Brain (agent orchestration)
  ├── memory: project memories, feedback, prior patterns
  ├── judgment: deterministic rules first, LLM for ambiguity
  ├── learning: outcome ledger feeds next decision
  └── webhook: receives AO escalations
       │
       ▼
  Agent-Orchestrator (AO) — lifecycle state machine + reaction engine
  ├── session manager: spawn, send, kill, liveness
  ├── scm-github: PR detection, CI parsing, merge readiness
  └── reactions: ci-failed, changes-requested, approved-and-green, agent-stuck
       │
       ▼
  Headless agents (claude, codex, gemini — fresh context each call)
```

The LLM brain sits above the AO loop. It has persistent memory and can make the same
decisions a human would make, looping autonomously until work is done or a genuine
human judgment is needed.

**Implementation boundary:** no changes to AO code. All new logic lives in
Python orchestration layer. AO is consumed via:
- AO → LLM brain escalation webhooks
- CLI commands: `ao spawn`, `ao send`, `ao kill`

---

## Core Principle: Deterministic First, LLM for Judgment

AO handles the predictable 80% deterministically. The LLM handles the 20% that
requires judgment. The LLM is not called for routine reactions.

| Signal | Handler | Decision maker |
|--------|---------|----------------|
| CI failed ≤ retry cap | `ci-failed` → `send-to-agent` | AO deterministic |
| Review comment received | `changes-requested` → `send-to-agent` | AO deterministic |
| PR approved + CI green | `approved-and-green` → review | AO triggers, LLM reviews |
| Agent stuck > threshold | `agent-stuck` → kill + respawn | Deterministic |
| CI failed, budget ≥ 2 left, parseable error | parallel retry (2-3 strategies) | Deterministic |
| Retry budget exhausted | escalate with failure summary | LLM → Human |
| Vague review needing interpretation | interpret + dispatch fix | LLM |
| New feature request → subtask decomposition | plan + spawn parallel sessions | LLM |
| "Should we abandon this approach?" | strategy decision | LLM |
| Risky change in sensitive path | explicit escalation + warning | LLM |

When the LLM makes a judgment call, it returns a **confidence score (0–1)**.
If confidence < `min_confidence` (default 0.6), auto-escalate to human instead
of acting. This prevents silent bad decisions on ambiguous cases.

---

## Webhook Ingress Pipeline

AO's notifier plugin posts escalation webhooks to a local webhook endpoint.
The orchestration layer handles these with proper queue durability and worker isolation:

```
AO notifier plugin
  ↓ POST http://localhost:9100/webhook
webhook_ingress (HTTP server, HMAC validate, SQLite queue)
  ↓ row inserted into webhook_deliveries
webhook_worker (polling loop, PR lock, bounded retries)
  ↓ dequeue → normalize
parse_ao_webhook → AOEvent
  ↓
escalation_handler.handle_escalation
  ├── escalation_router.route_escalation (deterministic rules)
  └── action_executor.execute_action (ao_cli / notifier)
  ↓
action_log.jsonl + failure_budget.json (audit trail)
```

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
        → NotifyHumanAction (budget exhausted)

session.stuck
  → KillAndRespawnAction (ao kill + ao spawn)

merge.ready
  → auto_review_trigger → LLM reviews PR
    ├── approve → NotifyHumanAction ("ready to merge")
    ├── request_changes → RetryAction (dispatch fix agent)
    └── escalate → NotifyHumanAction ("needs your eyes")

unknown event
  → NotifyHumanAction (fail-safe)
```

---

## Parallel Retries

When CI fails and the retry budget has ≥ 2 attempts remaining, the orchestration spawns
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

---

## Failure Budgets

Three tiers of budget tracking:

| Tier | Owner | Limit | What happens on exhaust |
|------|-------|-------|------------------------|
| Per-session | AO natively | `retries: 2` in reaction config | AO escalates |
| Per-subtask | `failure_budget.py` | 30 min from first escalation | LLM escalates to human |
| Per-task | `failure_budget.py` | 2 strategy changes | LLM escalates to human |

`PersistentFailureBudget` is file-backed JSON with atomic writes and locking —
survives process restarts.

---

## Autonomous PR Review

Before a human sees a PR, the orchestration reviews it:

```
merge.ready event
  ↓
build_review_context(pr_number)
  ├── gh api: diff, commits, CI status, reviews
  ├── CLAUDE.md: repo-level + global rules
  ├── memory: project memories, feedback memories, prior approval patterns
  └── action_log.jsonl: prior decisions on same repo
  ↓
review_pr(context) → LLM call
  ↓
ReviewDecision:
  ├── approve   → post GH review + NotifyHuman "ready to merge"
  ├── changes   → post GH review + dispatch fix agent via ao send
  └── escalate  → NotifyHuman "needs your eyes" + attach review notes
```

**Design principle:** the LLM gets everything and decides. No keyword matching,
no path globs, no hardcoded thresholds. CLAUDE.md rules, memory, prior patterns —
all injected as context. The LLM applies them through inference.

---

## Memory Integration

The LLM's judgment calls use three memory tiers:

| Memory type | Source | Used for |
|-------------|--------|----------|
| **Project memories** | memory/project/ | Codebase conventions, known patterns |
| **Feedback memories** | memory/feedback/ | What human corrected — shapes judgment |
| **Outcome ledger** | state/outcomes.jsonl | Which fix strategies won/lost by error class |

---

## Autonomous Completion Contract

The system succeeds when the orchestration can run the full loop from intent to
merge-readiness, replacing human in all but final approval.

### Loop Invariant

For every active task:
1. AO dispatches headless agent with fresh context + failure summary injected
2. AO detects PR/CI/review state via `scm-github` plugin
3. AO auto-remediates CI/review failures deterministically (≤ 2 retries)
4. On escalation: LLM routes → parallel retry, respawn, or escalate to human
5. On merge-ready: LLM reviews PR autonomously
6. Repeat until merge-readiness gates pass or human is genuinely needed

### Merge-Readiness Gates

A PR is `ready/mergeable` only when ALL are true:
1. AO `getMergeability()` passes (CI green, approved, no conflicts)
2. CodeRabbit approved or rate-limited
3. LLM PR review: `approve` decision

### Escalation to Human (Stop Autonomy)

The system must escalate when:
1. Retry + judgment budget exhausted — multiple strategies failed
2. Product-level ambiguity — scope change, conflicting requirements
3. Missing credentials/permissions cannot be obtained
4. Confidence score < `min_confidence` (0.6) on a judgment call
5. Sensitive-path change detected (security, auth, credentials)

---

## References

- [OpenAI: Harness Engineering](https://openai.com/index/harness-engineering/)
- [Martin Fowler: Harness Engineering](https://martinfowler.com/articles/exploring-gen-ai/harness-engineering.html)
- [NxCode: Complete Guide to Harness Engineering](https://www.nxcode.io/resources/news/harness-engineering-complete-guide-ai-agent-codex-2026)
- [Composio: Agent Orchestrator](https://github.com/ComposioHQ/agent-orchestrator)
