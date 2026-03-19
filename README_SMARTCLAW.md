# SmartClaw Orchestration Package

> **⚠️ WARNING: This is a non-working prototype / Work In Progress (WIP)**
>
> This package is under active development. Features may be incomplete, broken, or subject to change without notice. Do not use in production.

## What This Package Does

The SmartClaw orchestration package provides AI agent task execution through:

- **CLI Passthrough**: Direct invocation of agent CLIs (Claude, Codex, Gemini, MiniMax, Cursor)
- **Async tmux Mode**: Spawn detached sessions for long-running tasks
- **Task Dispatcher**: Programmatic multi-agent orchestration
- **Worktree Support**: Auto-create git worktrees for isolated agent contexts

### What This Package Does vs Agent-Orchestrator

| Aspect | SmartClaw | Agent-Orchestrator |
|--------|-----------|-------------------|
| **Purpose** | Task execution via CLI wrappers | Multi-agent coordination & messaging |
| **Interface** | CLI (`ai_orch`) + Python API | Redis-backed A2A protocols |
| **Session Mgmt** | tmux-based isolation | Dynamic agent lifecycle |
| **Use Case** | Single-task execution | Complex multi-agent workflows |
| **Status** | WIP/Prototype | More mature |

**When to use SmartClaw**: Quick ad-hoc agent tasks, CLI passthrough, simple async execution.

**When to use Agent-Orchestrator**: Multi-agent coordination, A2A messaging, complex task graphs, production automation.

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

### Python Packages

Install via `install.sh` or manually:

```bash
pip install jleechanorg-orchestration
```

---

## Setup Prerequisites

1. **Python 3.11+** installed
2. **tmux** installed and running
3. **Git** configured with GitHub access
4. **gh CLI** authenticated (`gh auth status`)
5. At least one agent CLI installed (see above)

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
- **Non-intrusive**: Only installs Python package, nothing else

---

## Configuration

No configuration required for basic usage. Advanced options:

- Set `PYTHON_BIN` to override Python interpreter
- Package version can be passed as argument: `./install.sh 0.1.40`

---

## Security Note: Secrets

> **⚠️ Never commit secrets to this repository**

- API keys, tokens, and credentials must be stored in environment variables or secure vaults
- Use `.env` files (ignored by git) for local development
- When using agent CLIs, ensure credentials are configured outside this package

Example `.env` setup:
```bash
# .env (add to .gitignore)
export ANTHROPIC_API_KEY="sk-..."
export GITHUB_TOKEN="ghp_..."
```

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    ai_orch CLI                          │
├─────────────────────────────────────────────────────────┤
│  Passthrough Mode  │  Async Mode (tmux)                │
│  ───────────────   │  ─────────────────               │
│  Direct exec       │  Spawn detached session           │
│  Stream output     │  Return immediately               │
├─────────────────────────────────────────────────────────┤
│              TaskDispatcher (Python API)               │
├─────────────────────────────────────────────────────────┤
│  Claude  │  Codex  │  Gemini  │  MiniMax  │  Cursor   │
└─────────────────────────────────────────────────────────┘
```

---

## Documentation

- [Full README](orchestration/README.md) - Detailed documentation
- [Design Doc](orchestration/design.md) - Architecture details
- [A2A Design](orchestration/A2A_DESIGN.md) - Agent-to-Agent protocols

---

## Support

This is a WIP prototype. For issues, check:

1. Agent CLI installation (`ai_orch --help`)
2. tmux availability (`tmux -V`)
3. Python version (`python3 --version`)
