# Context Window Comparison (Jeffrey's OpenClaw System)

```text
OPENCLAW (Orchestrator)                         AO WORKERS (Coding Agents)
────────────────────────────────────────        ────────────────────────────────────────

┌──────────────────────────────────────┐        ┌──────────────────────────────────────┐
│ RUNTIME + CHANNEL CONTEXT            │        │ TASK CONTEXT                         │
│ • Slack channel/thread routing       │        │ • User task text (verbatim)         │
│ • Gateway session + reply targeting  │        │ • Repo/worktree branch              │
│ • Heartbeat + scheduled nudges       │        │ • PR-specific review feedback        │
└──────────────────────────────────────┘        └──────────────────────────────────────┘
┌──────────────────────────────────────┐        ┌──────────────────────────────────────┐
│ POLICY + OPERATING RULES             │        │ CODE EXECUTION SURFACE               │
│ • SOUL.md commitments                │        │ • Files/code/tests in worktree       │
│ • AGENTS.md guardrails               │        │ • git commit / push / PR updates     │
│ • TOOLS.md local runtime notes       │        │ • CI retry loops / fix-forward       │
└──────────────────────────────────────┘        └──────────────────────────────────────┘
┌──────────────────────────────────────┐        ┌──────────────────────────────────────┐
│ MEMORY SYSTEM                        │        │ MERGE-GATE TARGETS                   │
│ • MEMORY.md (curated long-term)      │        │ • CI passing                         │
│ • memory/YYYY-MM-DD.md (daily log)   │        │ • CodeRabbit approved                │
│ • learned patterns + prior failures  │        │ • Bugbot clean + comments resolved   │
└──────────────────────────────────────┘        │ • Evidence + Skeptic pass (code PR) │
┌──────────────────────────────────────┐        └──────────────────────────────────────┘
│ AUTOMATION CONTROL                   │        ┌──────────────────────────────────────┐
│ • openclaw gateway + launchd jobs    │        │ LIMITS                               │
│ • cron/jobs.json scheduler           │        │ • No long-term memory between runs   │
│ • dispatch -> ao spawn/send          │        │ • Depends on prompt/context quality  │
└──────────────────────────────────────┘        │ • Escalates ambiguity to orchestrator│
                                                └──────────────────────────────────────┘

OPENCLAW IS GOOD AT:                            AO WORKERS ARE GOOD AT:
• Cross-tool orchestration                      • Fast implementation in repo scope
• Applying durable policy rules                 • Executing tests + fixing CI
• Memory-driven prioritization                  • Iterating on PR feedback
• Channel-aware status + escalation             • Shipping concrete code deltas

OPENCLAW RISK ZONES:                            AO WORKER RISK ZONES:
• Missing early ack can look silent             • Can stall without clear constraints
• Thread/context overload if not pruned         • Narrow view outside given context
• Needs tool auth for external writes           • No native cross-session memory
```

## Notes

- Uses **openclaw** naming (not Zoe) and maps to Jeffrey's live setup.
- Grounded in current workspace conventions: `SOUL.md`, `AGENTS.md`, `TOOLS.md`, `MEMORY.md`, launchd, and AO dispatch patterns.
