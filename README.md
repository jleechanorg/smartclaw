# smartclaw

> **⚠️ WARNING: This is a non-working prototype / Work In Progress (WIP)**
>
> This repository is under active development. Features may be incomplete, broken, or subject to change without notice. Do not use in production.

---

## Source from jleechanclaw

> This document adapts content from the private jleechanorg/jleechanclaw harness. See source references inline below.

---

## What is smartclaw?

SmartClaw is an **AI agent orchestration package** that provides:

- **CLI Passthrough**: Direct invocation of agent CLIs (Claude, Codex, Gemini, MiniMax, Cursor)
- **Async tmux Mode**: Spawn detached sessions for long-running tasks
- **Task Dispatcher**: Programmatic multi-agent orchestration
- **Worktree Support**: Auto-create git worktrees for isolated agent contexts

### What This Package Does vs Agent-Orchestrator

| Aspect | smartclaw | Agent-Orchestrator |
|--------|-----------|-------------------|
| **Purpose** | Task execution via CLI wrappers | Multi-agent coordination & messaging |
| **Interface** | CLI (`ai_orch`) + Python API | Redis-backed A2A protocols |
| **Session Mgmt** | tmux-based isolation | Dynamic agent lifecycle |
| **Use Case** | Single-task execution | Complex multi-agent workflows |
| **Status** | WIP/Prototype | More mature |

**When to use smartclaw**: Quick ad-hoc agent tasks, CLI passthrough, simple async execution.

**When to use Agent-Orchestrator**: Multi-agent coordination, A2A messaging, complex task graphs, production automation.

---

## Architecture

### Source: jleechanclaw ORCHESTRATION_DESIGN.md

The orchestration system follows a layered approach:

```
Human (Developer)
       ▲
       │ escalation (budget exhausted, ambiguity)
       ▼
┌─────────────────────────────────────────────────────┐
│          LLM Brain (OpenClaw-style)                  │
│  • memory: project memories, feedback                │
│  • judgment: deterministic rules first, LLM second   │
│  • learning: outcome ledger feeds next decision      │
└─────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────┐
│    Agent-Orchestrator (AO) — lifecycle + reactions   │
│  • session manager: spawn, send, kill, liveness    │
│  • scm-github: PR detection, CI parsing              │
│  • reactions: ci-failed, changes-requested, etc.    │
└─────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────┐
│   Headless Agents (claude, codex, gemini)            │
│   Fresh context per call (no accumulated state)       │
└─────────────────────────────────────────────────────┘
```

### Core Principle: Deterministic First, LLM for Judgment

> Source: jleechanclaw docs/HARNESS_ENGINEERING.md

The orchestration handles the predictable 80% deterministically. The LLM handles the 20% that requires judgment.

| Signal | Handler | Decision Maker |
|--------|---------|----------------|
| CI failed ≤ retry cap | `ci-failed` → `send-to-agent` | AO deterministic |
| Review comment received | `changes-requested` → `send-to-agent` | AO deterministic |
| Agent stuck > threshold | `agent-stuck` → kill + respawn | Deterministic |
| Retry budget exhausted | escalate with failure summary | LLM → Human |
| Vague review needing interpretation | interpret + dispatch fix | LLM |
| New feature → subtask decomposition | plan + spawn parallel | LLM |

---

## Harness Engineering Philosophy

> Source: jleechanclaw docs/HARNESS_ENGINEERING.md

Harness engineering shifts human engineers from writing code to designing:

1. **Environments** — isolated workspaces, tool access, credentials
2. **Constraints** — architectural rules, CLAUDE.md policies, dependency layering
3. **Feedback loops** — CI reactions, review automation, escalation policies
4. **Intent specifications** — prompts, task descriptions, acceptance criteria

### Key Principles

#### 1. Documentation Is Infrastructure

> "From the agent's perspective, anything it can't access in-context doesn't exist."

CLAUDE.md, AGENTS.md are not documentation — they are infrastructure. They are read by agents on every turn and directly control agent behavior.

#### 2. Deterministic First, LLM for Judgment

Don't use an LLM when a rule will do. CI failed? That's deterministic. Review comment? Deterministic. The LLM is called only when the deterministic router has no rule.

#### 3. Fresh Context, Not Accumulated Context

Each headless agent call gets a clean prompt with all context injected upfront. No memory of previous attempts — that's the harness's job.

#### 4. Build Rippable Harnesses

> "If you over-engineer the control flow, the next model update will break your system."

The orchestration layer is thin and replaceable. If a new tool adds native judgment support, the Python layer can be removed without touching agent configs.

#### 5. LLM Decides, Server Executes

For AI-driven features: the LLM gets full context and makes decisions. The server executes actions. Don't strip information "to optimize."

---

## Quickstart

```bash
# Install dependencies
./install.sh

# Run a task (passthrough mode)
ai_orch "explain this code"

# Run a task (async tmux mode)
ai_orch --async "implement feature X"
```

---

## Dependencies

### Required

| Dependency | Version | Purpose |
|------------|---------|---------|
| Python | 3.11+ | Runtime |
| tmux | latest | Session isolation |
| git | latest | VCS operations |
| gh | latest | GitHub CLI |

### Optional (Agent CLIs)

| CLI | Purpose |
|-----|---------|
| `claude` | Anthropic Claude Code |
| `codex` | OpenAI Codex CLI |
| `gemini` | Google Gemini CLI |
| `minimax` | MiniMax CLI |
| `cursor` | Cursor Agent CLI |

---

## Setup Prerequisites

1. **Python 3.11+** installed
2. **tmux** installed and running
3. **Git** configured with GitHub access
4. **gh CLI** authenticated (`gh auth status`)
5. At least one agent CLI installed

---

## Installation

```bash
./install.sh
```

This script will:
- Detect available Python interpreters
- Install the `jleechanorg-orchestration` package
- Verify installation

### Safety & Idempotency

- **No destructive actions**: Does not modify system files, crontab, or existing configurations
- **Idempotent**: Safe to run multiple times
- **Non-intrusive**: Only installs Python package

---

## Configuration Files

This repo includes configuration templates adapted from jleechanclaw:

| File | Purpose |
|------|---------|
| `openclaw.json` | Runtime configuration template |
| `agent-orchestrator.yaml` | Agent orchestrator config template |
| `docs/HARNESS_ENGINEERING.md` | Harness engineering philosophy |
| `roadmap/ORCHESTRATION_DESIGN.md` | Orchestration design document |

> **Note**: These are templates. Copy and customize for your environment. Never commit secrets.

---

## Security Note: Secrets

> **⚠️ Never commit secrets to this repository**

- API keys, tokens, and credentials must be stored in environment variables
- Use `.env` files (ignored by git) for local development
- When using agent CLIs, ensure credentials are configured outside this package

---

## Maturity Model

> Source: jleechanclaw docs/HARNESS_ENGINEERING.md

Based on the NxCode framework:

| Level | What You Have | Status |
|-------|---------------|--------|
| **1. Individual** | CLAUDE.md, pre-commit hooks, test suite | ✅ Available |
| **2. Team** | AGENTS.md, CI constraints, shared prompts | ✅ Available |
| **3. Organization** | Custom middleware, observability, escalation policies | 🔄 Future |

---

## References

- [OpenAI: Harness Engineering](https://openai.com/index/harness-engineering/)
- [Martin Fowler: Harness Engineering](https://martinfowler.com/articles/exploring-gen-ai/harness-engineering.html)
- [NxCode: Complete Guide to Harness Engineering](https://www.nxcode.io/resources/news/harness-engineering-complete-guide-ai-agent-codex-2026)
- [Composio: Agent Orchestrator](https://github.com/ComposioHQ/agent-orchestrator)

---

## Support

This is a WIP prototype. For issues, check:

1. Agent CLI installation (`ai_orch --help`)
2. tmux availability (`tmux -V`)
3. Python version (`python3 --version`)
