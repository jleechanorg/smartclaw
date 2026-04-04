# Harness Engineering Philosophy

> "The discipline of building systems that make AI agents actually work."
> -- OpenAI, February 2026

This repo is a **harness** — not a codebase that agents work on, but the
environment, constraints, and feedback loops that enable agents to do reliable
work across all jleechanorg projects.

## What Is Harness Engineering?

Harness engineering shifts human engineers from writing code to designing:

1. **Environments** — isolated workspaces, tool access, credentials
2. **Constraints** — architectural rules, CLAUDE.md policies, dependency layering
3. **Feedback loops** — CI reactions, review automation, escalation policies
4. **Intent specifications** — prompts, task descriptions, acceptance criteria

The harness encodes all of this into machine-readable artifacts that agents
consume at runtime. From the agent's perspective, anything not in context
doesn't exist — so the harness must make everything accessible.

Reference: [OpenAI: Harness Engineering](https://openai.com/index/harness-engineering/)

## How This Repo Is a Harness

### Layer 1: Agent Environment (config-first)

| Artifact | What It Does |
|----------|-------------|
| `SOUL.md` | Agent personality, goals, decision-making rules |
| `TOOLS.md` | Tool allow/deny list and usage policy |
| `CLAUDE.md` | Project rules, coding style, safety rails |
| `AGENTS.md` | Agent-specific guidelines and conventions |
| `openclaw.json` | Runtime config (memory, compaction, gateway) |
| `agents/*/models.json` | Per-agent model configuration |
| `skills/` | Custom agent skills (agento, browser, etc.) |

These are the "scaffolding" — agents read them directly and operate within
their constraints. Changes here take effect immediately because the repo
root IS `~/.openclaw/`.

### Layer 2: Deterministic Feedback Loops (agent-orchestrator)

The [agent-orchestrator](https://github.com/ComposioHQ/agent-orchestrator)
provides the reaction engine — deterministic responses to predictable events:

```yaml
reactions:
  ci-failed:     { auto: true, action: send-to-agent, retries: 2 }
  changes-requested: { auto: true, action: send-to-agent, escalateAfter: 30m }
  agent-stuck:   { threshold: 10m, action: notify, priority: urgent }
```

This is the inner feedback loop: agent acts, CI/review state changes, AO
reacts, agent acts again. No LLM needed for the predictable 80%.

### Layer 3: LLM Judgment (OpenClaw)

OpenClaw sits above the reaction engine and handles the 20% that requires
judgment — vague reviews, task decomposition, conflicting failures, strategy
decisions. It has persistent memory, tools, and full project context.

This is the outer loop: when deterministic reactions exhaust their budget,
AO escalates to OpenClaw. OpenClaw decides what to do next — retry with a
different strategy, decompose the problem, or escalate to Jeffrey.

### Layer 4: Entropy Management

Harnesses degrade over time. Documentation drifts from code. Constraints
get bypassed. Prompts that worked stop working as models update.

Planned entropy management:
- **Self-improving prompts** (ORCH-04k) — log which prompts succeed vs fail,
  build a project-specific prompt library
- **Autonomous PR review** (ORCH-apr) — OpenClaw reviews PRs using memory,
  CLAUDE.md rules, and historical patterns before Jeffrey sees them
- **Convergence intelligence** (ORCH-cil) — learning, anomaly detection,
  escalation tier tuning based on historical success rates

## Key Principles

### 1. Documentation Is Infrastructure

> "From the agent's perspective, anything it can't access in-context doesn't exist."

CLAUDE.md, AGENTS.md, and SOUL.md are not documentation — they are
infrastructure. They are read by agents on every turn and directly control
agent behavior. Treat them with the same rigor as production config.

### 2. Deterministic First, LLM for Judgment

Don't use an LLM when a rule will do. CI failed? That's a deterministic
reaction. Review comment? Deterministic. The LLM is called only when the
deterministic router has no rule — ambiguous reviews, strategy decisions,
task decomposition.

This follows the Spotify Honk pattern: "reduced flexibility increases
predictability."

### 3. Fresh Context, Not Accumulated Context

Each headless agent call gets a clean prompt with all context injected
upfront. No memory of previous attempts — that's the harness's job
(AO session metadata + OpenClaw memory). The coding agent never
accumulates context, avoiding the context bloat that kills performance
in long-running agent loops.

### 4. Build Rippable Harnesses

> "If you over-engineer the control flow, the next model update will break
> your system."

The orchestration layer (~3k lines of Python) is thin and replaceable.
If AO adds native judgment support, or if Claude Agent Teams subsumes
the reaction engine, the Python layer can be removed without touching
agent configs or project structure.

### 5. LLM Decides, Server Executes

For AI-driven features: the LLM gets full context and makes decisions.
The server executes actions. Don't strip information "to optimize."
Don't pre-compute what the LLM should decide. Don't add keyword-based
intent detection to bypass LLM judgment.

## Harness Maturity Model

Based on the NxCode framework:

| Level | What You Have | Where We Are |
|-------|-------------|--------------|
| **1. Individual** | CLAUDE.md, pre-commit hooks, test suite, naming conventions | Done |
| **2. Team** | AGENTS.md, CI architectural constraints, shared prompt templates, documentation validation | Done |
| **3. Organization** | Custom middleware, observability integration, harness versioning, agent performance dashboards, escalation policies | In progress (ORCH-cvg, ORCH-cil) |

## References

- [OpenAI: Harness Engineering](https://openai.com/index/harness-engineering/)
- [Martin Fowler: Harness Engineering](https://martinfowler.com/articles/exploring-gen-ai/harness-engineering.html)
- [NxCode: Complete Guide to Harness Engineering](https://www.nxcode.io/resources/news/harness-engineering-complete-guide-ai-agent-codex-2026)
- [Spotify: Background Coding Agents (Honk)](https://engineering.atspotify.com/2025/12/feedback-loops-background-coding-agents-part-3)
- [Composio: Agent Orchestrator](https://pkarnal.com/blog/open-sourcing-agent-orchestrator)
