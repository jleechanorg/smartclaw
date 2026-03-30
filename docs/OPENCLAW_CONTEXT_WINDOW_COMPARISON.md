# Context Window Comparison (Business Focus: OpenClaw + worldarchitect.ai)

```text
OPENCLAW (Business Orchestrator)                AO WORKERS (Execution Agents)
────────────────────────────────────────        ────────────────────────────────────────

┌──────────────────────────────────────┐        ┌──────────────────────────────────────┐
│ BUSINESS CONTEXT                     │        │ DELIVERY CONTEXT                     │
│ • AI RPG product goals               │        │ • Issue/PR scope for one change      │
│ • Player retention + engagement      │        │ • Repo branch + code files           │
│ • Revenue/pricing priorities         │        │ • Tests + CI checks                  │
│ • Roadmap sequencing                 │        │ • Reviewer comments to resolve       │
└──────────────────────────────────────┘        └──────────────────────────────────────┘
┌──────────────────────────────────────┐        ┌──────────────────────────────────────┐
│ DECISION SYSTEM                      │        │ IMPLEMENTATION SYSTEM                │
│ • Prioritizes what matters now       │        │ • Writes/fixes code quickly          │
│ • Converts goals -> concrete tasks   │        │ • Pushes commits + updates PRs       │
│ • Chooses when to escalate/block     │        │ • Iterates until checks pass         │
└──────────────────────────────────────┘        └──────────────────────────────────────┘
┌──────────────────────────────────────┐        ┌──────────────────────────────────────┐
│ MEMORY + OPERATING HISTORY           │        │ CODE + QUALITY TARGETS               │
│ • MEMORY.md (durable patterns)       │        │ • App behavior correctness           │
│ • daily logs (wins/failures)         │        │ • Type/test/lint health              │
│ • prior CI/review failure patterns   │        │ • Merge-gate compliance              │
└──────────────────────────────────────┘        └──────────────────────────────────────┘
┌──────────────────────────────────────┐        ┌──────────────────────────────────────┐
│ AUTOMATION + CONTROL                 │        │ LIMITS                               │
│ • Slack/Gateway command center       │        │ • Narrow scope per run               │
│ • launchd + scheduler loops          │        │ • Limited long-horizon context       │
│ • dispatch to AO for implementation  │        │ • Needs clear objective + constraints│
└──────────────────────────────────────┘        └──────────────────────────────────────┘

OPENCLAW IS GOOD AT:                            AO WORKERS ARE GOOD AT:
• Product/ops prioritization                    • Fast coding + refactors
• Connecting business goals to execution        • Test-fix loops and CI recovery
• Cross-repo coordination and routing           • Resolving concrete review items
• Durable memory of what worked                 • Shipping focused PR increments

OPENCLAW RISK ZONES:                            AO WORKERS RISK ZONES:
• Over-broad context can slow decisions         • Can optimize locally, miss strategy
• Needs reliable tool/auth plumbing             • Can stall without clear acceptance criteria
• Must keep priorities explicit                 • No durable cross-session product memory
```

## worldarchitect.ai mapping (AI RPG)

- **Business lane (OpenClaw):** decides whether to prioritize player experience, progression reliability, security hardening, or release velocity for the AI RPG.
- **Execution lane (AO workers):** implements the selected change in `worldarchitect.ai`, drives CI/review to green, and returns proof (PR/commits/checks).
- **Feedback loop:** production/review outcomes are captured in memory and used by OpenClaw to improve the next prioritization decision.

## Notes

- Uses **openclaw** naming (not Zoe).
- Reframed for business outcomes first, then engineering execution.
