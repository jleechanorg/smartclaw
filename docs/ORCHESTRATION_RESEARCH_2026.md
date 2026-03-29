# Orchestration Architecture Research (2026-03-14)

Research on multi-agent AI orchestration patterns, LLM-driven vs deterministic orchestration,
nested agent loops, and competitive landscape analysis. Conducted to validate the
jleechanclaw orchestration design doc (`roadmap/ORCHESTRATION_DESIGN.md`).

---

## 1. LLM-Driven vs. Deterministic Orchestration: Industry Consensus

The industry is converging on a **hybrid approach**: deterministic pipelines for predictable
lifecycle mechanics, LLM for planning and adaptation.

### Microsoft Agent Framework
One of the first frameworks to support both LLM-driven and deterministic orchestration in the
same system. The trade-off is described as the "predictability-adaptability frontier" — a
sequential pipeline is predictable; a conversational multi-agent system is adaptive. The
orchestrator's design implements this architectural trade-off.

### Spotify (Honk System)
Source: https://engineering.atspotify.com/2025/12/feedback-loops-background-coding-agents-part-3

Production lessons:
- **"Reduced flexibility increases predictability"** — agents are heavily constrained
- Agents can only: read codebase, edit files, execute verifiers
- Push ops, user comms, prompt authoring handled by surrounding infrastructure
- **LLM-as-Judge pattern**: secondary LLM evaluates diffs before merge, vetoes ~25% of sessions
- Agents successfully course-correct in ~50% of veto cases
- Three failure modes (in order of severity):
  1. Failed PR generation (minor — manual fallback acceptable)
  2. CI failures (frustrating — forces engineer remediation)
  3. Functionally incorrect but CI-passing code (critical — erodes trust)
- Inner loop (deterministic verifiers) runs before PR creation
- Outer loop (CI/CD) provides complementary validation

### Composio (agent-orchestrator)
Source: https://pkarnal.com/blog/open-sourcing-agent-orchestrator

Production data:
- **30 parallel agents** running simultaneously
- **61 PRs merged from 102 created** (60% success rate)
- **377 automated reviews** (Cursor Bugbot)
- **700 inline code comments** catching real issues
- **68% immediate fixes** by agents reading feedback
- **Zero human commits** to feature branches
- 40,000 lines of TypeScript, 17 plugins, 3,288 tests — built in 8 days
- PR #125 dashboard redesign: 12 CI failure→fix cycles, zero human intervention

Reaction engine in practice:
```yaml
reactions:
  ci_failed:
    action: spawn_agent
    prompt: "CI failed. Read failure logs and fix issues."
  changes_requested:
    action: spawn_agent
    prompt: "Address review comments and push fixes."
```

Three-layer review architecture:
- Automated review (69% of comments)
- Agent-to-agent review (30%)
- Human review (1%)

### Academic Research
Source: arxiv.org/abs/2511.15755

Multi-agent LLM orchestration achieves:
- 100% actionable recommendation rate vs 1.7% for single-agent (80x improvement)
- 140x improvement in solution correctness
- Zero quality variance across all trials — enables production SLA commitments

---

## 2. Nested Agent Loops: Validated Pattern

### Ralph Loop Method
Source: https://www.howdoiuseai.com/blog/2026-01-19-how-to-build-self-improving-ai-agents-with-the-ral

The core mechanism is the continuous loop: agents pick tasks, implement code, validate changes,
commit, update status, and reset context for the next iteration. Stateless-but-iterative design
prevents context overflow.

**Memory persistence across iterations** through four channels:
1. Git commit history (code changes visible via diffs)
2. Progress logs (chronological records of attempts)
3. Task state files (JSON tracking completion status)
4. AGENTS.md knowledge base (accumulated semantic wisdom)

Philosophy: "each improvement should make future improvements easier"

### Self-Improving Coding Agents
Source: https://addyosmani.com/blog/self-improving-agents/

Key patterns:
- Loop halts on red flags (failed tests, type errors, linting failures) and re-prompts
- For human feedback: open PRs rather than auto-merge
- Developers update AGENTS.md with corrections → persistent preference records
- **Compound loops** chain phases: Analysis → Planning → Execution
- **Planner-worker hierarchies**: planner decomposes; workers execute; judges assess
- Partitioned work (separate branches/features) beats pure parallelism

Production lessons:
- **Context bloat kills performance** — summarize older logs, archive obsolete guidance
- **Monitoring essential** — tail progress logs, inspect diffs, set hard stops
- **Safeguards**: feature branches, whitelisted operations, sandboxed environments, emergency stops
- Use different models for different roles (planning vs coding)
- Quality emerges from example — maintain clean tests, agents mirror that standard

### Self-Improving Coding Agent (Academic)
Source: researchgate.net/publication/390991089

LLM coding agent equipped with basic coding tools can autonomously edit itself and improve
performance on benchmark tasks: gains from 17% to 53% on SWE Bench Verified subset.

---

## 3. Gastown Analysis

### Overview
Source: https://github.com/steveyegge/gastown, https://www.wal.sh/research/gastown.html

Steve Yegge's Go-based multi-agent workspace manager. Core concepts:
- **Mayor**: AI coordinator (Claude Code instance with full workspace context)
- **Town**: Workspace directory containing all projects and agents
- **Rigs**: Project containers wrapping git repositories
- **Polecats**: Worker agents with persistent identity but ephemeral sessions
- **Hooks**: Git worktree-based persistent storage for agent work
- **Convoys**: Work tracking units bundling multiple beads
- **MEOW**: Mayor-Enhanced Orchestration Workflow (recommended pattern)
- **GUPP**: "If there is work on your hook, YOU MUST RUN IT"

### Strengths
- Git-backed state persistence addresses real problem of context loss on agent restart
- Clear role hierarchy with explicit delegation patterns
- Dashboard with htmx auto-refresh for workspace overview
- Beads integration for structured work tracking
- Multiple forks exist (gastown-copilot, ai-gastown, LCgastown)

### Weaknesses / Overengineering Concerns
- Whether pre-planned task decomposition outperforms dynamic LLM-driven orchestration is unproven
- Overhead of Mayor's initial analysis vs letting agents discover parallelizable work
- Git-backed persistence creates maintenance burden compared to simpler state stores
- "No evidence presented that structured convoy pre-planning outperforms adaptive orchestration"
- Mayor + Town + Rigs + Polecats + Hooks + Convoys is significant ceremony for "spawn agent on branch"
- GUPP ("must run work on hook") is rigid compared to reaction-based approaches

### Rejection Assessment
The jleechanclaw rejection of Gastown is defensible. The key distinction:
- Gastown's coordination layer (hooks, convoys, rigs) is deterministic Go code, not LLM-native
- AO covers session lifecycle, worktrees, multi-agent config, and web dashboard
- AO's session metadata + worktree model covers Gastown's "persistent work state" concept
- Beads format conflict (gt-* vs ORCH-* prefix)

---

## 4. Claude Code Agent Teams (Official, Feb 2026)

Source: https://code.claude.com/docs/en/agent-teams

Anthropic released Agent Teams on February 5, 2026 alongside Opus 4.6:
- Built-in multi-agent coordination with TeammateTool
- One session acts as team lead, teammates work independently
- Each agent runs in isolated git worktree via tmux
- Inter-agent messaging via custom SQLite mail system
- FIFO merge queue with 4-tier conflict resolution
- Split panes require tmux or iTerm2

**Risk for jleechanclaw design:** Agent Teams may eventually subsume what ai_orch does,
making that layer redundant. Worth monitoring.

### Community Frameworks
- **Overstory** (jayminwest): pluggable runtime adapters for Claude Code, Pi, and more
- **ccswarm** (nwiizo): Git worktree isolation, specialized AI agents, Claude Code CLI
- **IttyBitty** (adamwulf): lightweight agent orchestrator

---

## 5. Framework Landscape (2025-2026)

Source: https://aimultiple.com/llm-orchestration

Top frameworks: LangChain, CrewAI, Ray (enterprise); LlamaIndex, Langflow, Botpress (domain-specific).

Key pattern: dual-layered architecture with Planner layer (task decomposition) and Executor layer
(tool interaction), allowing specialized prompts or different models per layer.

OpenAI Agents SDK: https://openai.github.io/openai-agents-python/multi_agent/
AWS Strands Agents: https://aws.amazon.com/blogs/machine-learning/customize-agent-workflows-with-advanced-orchestration-techniques-using-strands-agents/
Temporal for AI Agents: https://temporal.io/blog/of-course-you-can-build-dynamic-ai-agents-with-temporal

---

## 6. Risks Identified for jleechanclaw Architecture

### ai_orch Layer Redundancy
AO has its own tmux runtime plugin. ai_orch has 78 releases of battle-tested tmux management.
Both do worktree isolation. Key question: does ai_orch add enough (multi-CLI fallback, A2A
messaging, agent monitoring) to justify the extra layer?

### Context Bloat in Nested Loops
Research consistently warns that context bloat kills performance in iterative agent loops.
The outer Ralph loop must aggressively summarize and prune context between iterations.

### 60% PR Success Rate
Composio's own data shows 61/102 PRs merged. The 40% failure rate means robust escalation
and human-in-the-loop pathways are critical. The current escalation config (2 retries for CI,
30min for reviews) needs empirical validation.

### Single-Machine Resource Constraints
30 parallel agents is the proven ceiling on a single machine. With 20+ repos, resource
contention (CPU, memory, API rate limits) will be a real constraint.

### Agent Teams Convergence Risk
Anthropic's official Agent Teams feature may eventually make custom orchestration layers
unnecessary. The architecture should be loosely coupled enough to swap out ai_orch if
Agent Teams becomes the standard substrate.

---

## Sources

- [Spotify: Background Coding Agents Feedback Loops](https://engineering.atspotify.com/2025/12/feedback-loops-background-coding-agents-part-3)
- [Addy Osmani: Self-Improving Coding Agents](https://addyosmani.com/blog/self-improving-agents/)
- [Composio: Open-Sourcing Agent Orchestrator](https://pkarnal.com/blog/open-sourcing-agent-orchestrator)
- [Gastown: Multi-Agent Orchestration for Claude Code](https://www.wal.sh/research/gastown.html)
- [Gastown GitHub](https://github.com/steveyegge/gastown)
- [agent-orchestrator GitHub](https://github.com/ComposioHQ/agent-orchestrator)
- [Claude Code Agent Teams Docs](https://code.claude.com/docs/en/agent-teams)
- [Microsoft AI Agent Design Patterns](https://learn.microsoft.com/en-us/azure/architecture/ai-ml/guide/ai-agent-design-patterns)
- [Multi-Agent LLM Orchestration (arxiv 2511.15755)](https://arxiv.org/abs/2511.15755)
- [Ralph Loop Method](https://www.howdoiuseai.com/blog/2026-01-19-how-to-build-self-improving-ai-agents-with-the-ral)
- [Autonomous Coding Agents Guide 2026](https://www.sitepoint.com/autonomous-coding-agents-guide-2026/)
- [Self-Improving Coding Agent (ResearchGate)](https://www.researchgate.net/publication/390991089_A_Self_Improving_Coding_Agent)
- [LLM Orchestration Frameworks 2026](https://aimultiple.com/llm-orchestration)
- [OpenAI Agents SDK Multi-Agent](https://openai.github.io/openai-agents-python/multi_agent/)
- [Shipyard: Claude Code Multi-Agent](https://shipyard.build/blog/claude-code-multi-agent/)
- [ccswarm GitHub](https://github.com/nwiizo/ccswarm)

---

---

## 7. Framework Comparison: Which Orchestrator Performs Best?

No single winner — depends on optimization target.

| Framework | Best At | Evidence | Weakness |
|---|---|---|---|
| **agent-orchestrator (Composio)** | Production lifecycle automation | 61/102 PRs merged (60%), 30 parallel agents, 377 auto-reviews, zero human commits | Cross-agent conflict resolution still manual |
| **Claude Agent Teams (Anthropic)** | Simplicity — it's just a prompt | Built-in to Claude Code, no external framework, native SQLite messaging | Experimental, limited customization, no reaction engine |
| **Gastown (Yegge)** | Maximum parallelism | 20-30 parallel agents, git-backed persistence | "100% vibe coded", complex ceremony, solo dev focus |
| **Multiclaude (Lorenc)** | Hands-off autonomy | Auto-merge on CI pass ("Brownian ratchet"), multiplayer review mode | Aggressive — merges anything that passes CI |
| **Metaswarm** | Quality enforcement | 18 agents, 11-phase pipeline, cross-model adversarial review | Complex pipeline overhead |
| **Genie (Cosine)** | Benchmark accuracy | 72% on SWE-Lancer | Enterprise-only, small team |
| **Ralph (bash loop)** | Simplicity | Proven "faith-based iteration" pattern | No lifecycle state machine, no reaction engine |

Source: https://rywalker.com/research/autonomous-agentic-engineering-tools

### Why agent-orchestrator Wins for jleechanclaw

1. **Only production-proven reaction engine** as declarative config (CI failure → auto-fix → escalate)
2. **Real production data** at scale (not just benchmarks)
3. OpenClaw notifier plugin already wired
4. Declarative YAML config per project fits 20+ repo surface
5. Plugin architecture allows swapping runtimes without changing orchestration logic

The closest competitor is Claude Agent Teams — if Anthropic adds a reaction engine and
per-project config, it could subsume AO for Claude-only setups. But today it's task
coordination without lifecycle automation.

### Market Trajectory

"By 2027, enterprise adoption will shift toward orchestration platforms" as the distinction
between autonomous agents and orchestrators blurs. The current landscape is consolidating —
GPT Engineer and Smol Developer are already archived.

---

## 8. Beads Reference

| Bead | Relevance |
|------|-----------|
| ORCH-cvg | Convergence orchestration gateway — validated by research as the right pattern |
| ORCH-cil | Convergence Intelligence Layer — maps to "self-improving agent" patterns from Addy Osmani |
| ORCH-nrl | Nested Ralph Loops — validated by compound loop research (analysis → planning → execution) |
| ORCH-5gc | Readiness gates — matches Spotify's "stop hook" pattern and AO's merge readiness aggregation |
| ORCH-36u | Escalation policy — Composio data shows 40% PR failure rate, escalation is critical |

---

## Additional Sources

- [Shipyard: Multi-Agent Orchestration for Claude Code](https://shipyard.build/blog/claude-code-multi-agent/)
- [Ry Walker: Autonomous Agentic Engineering Tools](https://rywalker.com/research/autonomous-agentic-engineering-tools)
- [Composio: The Self-Improving AI System](https://composio.dev/blog/the-self-improving-ai-system-that-built-itself)
- [MarkTechPost: Composio Open Sources Agent Orchestrator](https://www.marktechpost.com/2026/02/23/composio-open-sources-agent-orchestrator-to-help-ai-developers-build-scalable-multi-agent-workflows-beyond-the-traditional-react-loops/)
- [Multiclaude HN Discussion](https://news.ycombinator.com/item?id=46902368)
- [Microsoft Agent Framework Orchestrations](https://learn.microsoft.com/en-us/agent-framework/user-guide/workflows/orchestrations/overview)
- [Deloitte: AI Agent Orchestration Predictions](https://www.deloitte.com/us/en/insights/industry/technology/technology-media-and-telecom-predictions/2026/ai-agent-orchestration.html)

---

---

## 9. Second Opinion: Gemini Architectural Review

**Executive Summary:** "You have built an incredibly robust, battle-hardened execution engine
in Python (ai_orch), but you are currently suffocating it under an 'orchestration sandwich.'
For a single developer on a single machine, this stack is heavily overengineered vanity
infrastructure, though it contains pockets of genuine brilliance."

### ai_orch Redundancy vs. AO

Gemini says **AO is the unnecessary layer, not ai_orch.** The Python codebase contains 78
releases of hard-won domain knowledge — cross-repo worktree resolution, detached HEAD
workarounds, transient remote push failure handling, CAS guards on JSONL registries. AO's
"native tmux spawning" is almost certainly naive compared to this. By stacking AO (TypeScript)
on top of ai_orch (Python), you've created a polyglot orchestration sandwich.

**Recommendation:** Kill AO. OpenClaw should call ai_orch directly.

### Nested Ralph Loop Realism

Gemini says **aspirational hand-waving.** LLMs are notoriously bad at stateful backtracking
and "modifying strategy" over long horizons. When an inner loop fails 3 times, feeding massive
diff history and CI failure logs back into the outer LLM will cause context exhaustion. The LLM
will likely enter an apology loop and hallucinate the exact same code.

**Recommendation:** If you want nested loops, the outer loop must be deterministic code, not
an open-ended LLM prompt. The LLM should be a stateless function called BY the loop, not the
loop itself.

### Gastown Rejection

Gemini says **the right call, but deeply ironic.** Git hooks obscure intent and create spooky
action-at-a-distance. But you rejected Gastown for being "overengineered non-LLM code" then
built a TypeScript orchestrator, a Python daemon, outbox queues, and JSONL registries. You
traded Yegge's Go complexity for your own TypeScript/Python complexity.

### Biggest Architectural Risks

1. **Split-Brain State (Critical):** AO tracking state via hash-namespaced sessions and Python
   tracking state via JSONL with CAS. When these desync (SIGKILL drops TypeScript but leaves
   Python tmux session running), you need manual database surgery.
2. **Error Masking (High):** By the time the LLM sees an error, it has crossed three
   serialization boundaries — crucial stack trace nuance is truncated into a generic
   "CI_FAILED" enum.
3. **Context Collapse (High):** Pushing state management into the LLM prompt for the nested
   loop will blow past the model's effective attention span.
4. **Latency Tax (Medium):** IPC overhead of LLM → TypeScript → Python → CLI Agent means
   feedback loops measured in minutes rather than seconds.

### Gemini's Guidance: Flatten the Stack

1. Kill AO. It provides nothing that a clean LLM tool-call directly into Python scripts couldn't do.
2. Elevate ai_orch to be the sole programmatic API.
3. Let OpenClaw consume the Python API directly.
4. Move "Outer Loop" logic out of the LLM prompt into a standard Python `while` loop that
   calls the LLM statelessly.

---

## 10. Second Opinion: Grok Contrarian Analysis

### Four Layers Is Too Many

Grok says **yes, absolutely collapse them.** The separation between mctrl and ai_orch is
artificial. `dispatch_task` and tmux session spawning are subprocess calls — they don't warrant
separate systems. "78 releases on ai_orch sounds impressive until you realize that's 78
chances for drift between systems that should be one."

### LLM-as-Orchestrator Is a Liability

Grok says **putting an LLM at the top of the decision chain is the biggest risk.** LLMs
hallucinate plans, forget context mid-session, and cascade errors. Gastown's deterministic Go
code and Spotify's Honk (constrain LLMs to read/edit/verify ONLY) are the models that actually
work in production.

**Recommendation:** Flip the architecture. Make the supervisor fully deterministic — a
rule-based FSM: `if webhook → parse event → queue task → execute`. Reserve OpenClaw as a
plugin for ambiguity resolution only, not as the top-level planner. "The current approach where
the LLM decides what to dispatch is why 40% of PRs fail. A deterministic router would push
success rates toward 90%."

### Nested Ralph Loops Will Never Converge

Grok says **this is a fractal failure factory.** Concrete failure modes:
- Outer loop hallucinates a plan, inner loop spins on a CI flake, outer re-dispatches with
  mutated state — infinite regression
- State explosion as evidence packets bloat memory across nested iterations
- Escalation hell when best-effort webhooks miss events between nested dispatch cycles
- Non-terminating conditions when the LLM deems "not converged" forever on vague review comments

**Recommendation:** Flatten to a single convergence loop per PR. Poll/parse/fix/retry with
hard cap (3-5 attempts max), then escalate to human. No nesting. No recursion.

### Gastown Rejection Is NIH Syndrome

Grok says **textbook Not Invented Here.** mctrl's session_registry, lifecycle_reactions,
evidence packets, and beads tracking are Python-flavored git hooks and workspace persistence —
exactly what Gastown provides in Go. The stated reason for rejection ("overengineered non-LLM
code") contradicts the fact that 90% of the current system is deterministic anyway.

### What Grok Would Build from Scratch

A single Python monolith (~2k LOC max), run as a daemon:
- **Deterministic core**: FSM-driven event loop with asyncio. Everything except code generation
  is rule-based. No top-level LLM planning. Simple router: CI fail → dispatch fixer; review
  comment → targeted patcher. LLMs only for code write/review.
- **Execution**: Single-threaded queue with git worktree isolation. Parallelism via
  multiprocessing for 4-8 agents max. No tmux — use subprocesses directly.
- **Integrations**: Native GitHub App for webhooks with HMAC. Slack on escalation only.
- **Failure handling**: Hard timeouts (10min/task), stuck detection (no state change in 2
  polling cycles), manual replay via CLI flag.
- **Config**: One `swarm.json`. No SOUL.md, no TOOLS.md, no markdown manifestos.
- **Why it wins**: "80% less code, zero cross-layer bugs, deterministic 90%+ success rate
  following the Honk/Spotify model."

---

## 11. Synthesis: Points of Agreement Across All Sources

Both consultants + research converge on these themes:

| Theme | Gemini | Grok | Research |
|---|---|---|---|
| **Outer loop should be deterministic, not LLM** | "LLM as stateless function called BY the loop" | "Rule-based FSM, LLM only for code" | Spotify Honk: "reduced flexibility increases predictability" |
| **Nested loops are high-risk** | "Context exhaustion, apology loops" | "Fractal failure factory" | Addy Osmani: "context bloat kills performance" |
| **Split-brain state is the #1 operational risk** | "Manual database surgery to unbrick" | "Cross-layer state mismatches" | Composio: cross-agent conflict resolution still manual |
| **ai_orch Python code is genuinely valuable** | "Pockets of genuine brilliance" | "78 releases of hard-won knowledge" | N/A |
| **Single-machine complexity ceiling** | "Vanity infrastructure" | "MacBook is not a cluster" | 30 agents is proven ceiling |

### Key Disagreement

- **Gemini:** Kill AO, keep ai_orch, let OpenClaw call Python directly
- **Grok:** Kill AO, merge ai_orch into mctrl, make supervisor deterministic
- **Current design:** Keep AO as the lifecycle layer, ai_orch as convenience

### The Actionable Takeaway

Both consultants recommend the same structural change: **make the outer loop deterministic
code, not an LLM prompt.** This is the single highest-impact design change the research
supports. The nested Ralph loop north star should be reframed as:

```
Deterministic Python while-loop (outer):
  calls LLM statelessly for planning decisions
  dispatches AO/ai_orch sessions
  polls for convergence signals
  applies hard caps (3-5 retries, 10min timeout)
  escalates to human on non-convergence

AO/ai_orch sessions (inner):
  standard CI/review remediation with reaction engine
  deterministic state transitions
  LLM only writes/reviews code
```

This preserves the nested loop vision but moves the orchestration control flow out of the
LLM's context window and into reliable, debuggable Python code.

---

*Generated: 2026-03-14 by /research + /secondo commands*
*Consultants: Gemini (architectural review), Grok (contrarian analysis)*
