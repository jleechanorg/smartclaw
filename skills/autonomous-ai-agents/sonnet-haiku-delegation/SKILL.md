---
name: sonnet-haiku-delegation
description: Route subagent tasks to Sonnet (primary workhorse) or Haiku (quick/simple) using an Opus orchestrator. Inspired by team-claude.md (jleechanorg/claude-commands) and WorldAI planexec patterns.
triggers:
  - "spawn a subagent"
  - "delegate to sonnet"
  - "use haiku for"
  - "run subagent"
  - "parallel agent tasks"
  - "plan and execute"
  - "planexec"
---

# Sonnet-Haiku Delegation Skill

## Core Principle

Use **Opus** (your current session) for orchestration decisions, planning, and final review only.
Use **Sonnet** as the primary workhorse for coding, testing, verification, and code review.
Use **Haiku** for file lookups, simple checks, formatting, and trivial subtasks.

**Goal: Maximize Sonnet usage, minimize Opus token spend.**

## When to Use Each Model

| Model | Use For | Examples |
|-------|---------|----------|
| **Opus** | Orchestration, complex reasoning, final review, architecture decisions | Planning complex features, resolving ambiguous requirements, final PR review |
| **Sonnet** | Primary workhorse — all substantive implementation work | Writing code, running tests, debugging, code review, verification |
| **Haiku** | Quick/simple tasks under 30 seconds | File searches, glob patterns, simple grep, formatting, one-liner fixes |

## Sonnet-Haiku Routing Rules

### Rule 1: Sonnet for Judgment, Haiku for Mechanical

From `evidence-reviewer.md` (WorldAI):
- **Sonnet required**: Quality judgment, reasoning chains, architectural decisions, verification that requires evaluation
- **Haiku acceptable**: Pattern matching, file existence checks, grep/glob, simple formatting validation

### Rule 2: The 5-Criteria Delegation Check

From `subagent-strategy.md` (WorldAI) — delegate only when ALL pass:

```python
def should_delegate(task):
    # 1. Parallelism — can it run independently?
    if not task.can_run_in_parallel():
        return False

    # 2. Resource — system has capacity?
    if system.memory_usage_percent > 50 or active_instances >= 3:
        return False

    # 3. Overhead vs Benefit — task > ~5 minutes?
    if task.estimated_duration_seconds < 30:
        return False  # Haiku-style: do it directly

    # 4. Specialization — needs different expertise?
    if not task.requires_different_expertise():
        return False

    # 5. Independence — can run without coordination?
    if task.requires_frequent_coordination():
        return False

    return True  # Delegate to Sonnet
```

### Rule 3: Direct Execution Default

From WorldAI's `subagent-strategy.md`: **80% of tasks should be direct execution.** Only 20% warrant delegation.

Delegate when:
- Truly independent parallel work (e.g., multi-layer feature: frontend + backend + tests simultaneously)
- Specialized domain expertise needed (e.g., Firebase security rules, ML pipeline)
- Browser test suites that can run in parallel
- Cross-cutting refactors with clear boundaries

Do NOT delegate when:
- Task is under 2 minutes
- Sequential workflow requiring coordination
- Requires reading output before deciding next step
- Simple enough for Haiku (do it directly yourself)

## Plan-Execute with Approval Pattern

Based on WorldAI's `planexec.md` — use this for multi-step implementations:

### Phase 0: Context Assessment (Mandatory First Step)
```
/context → note remaining context %
```
Adapt planning depth to remaining context:
- **High** (60%+): Comprehensive analysis, detailed plan
- **Medium** (30-60%): Targeted analysis, efficient tool selection
- **Low** (<30%): Lightweight, essential tasks only

### Phase 1: Strategic Analysis
1. Consult Memory for relevant patterns/corrections
2. Run skill checks (code-centralization, file-justification, integration-verification)
3. Create beads for work items if complexity is Medium+

### Phase 2: Present Plan for Approval
Present the execution plan with:
- Context status and complexity level
- Bead IDs for tracking
- Tool selection rationale
- **Explicit approval gate** — do NOT proceed until user says APPROVED

### Phase 3: Execute After Approval
- Monitor context usage
- Use Sonnet agents for substantive work
- Use Haiku agents for simple parallel tasks
- Update beads as work progresses

### Phase 4: /simplify (Mandatory Final Step)
After any execution completes, run `/simplify` to:
- Review changed code for reuse opportunities
- Check code quality and efficiency
- Fix any issues before declaring complete

## AO Worker Model Routing

When using `ao spawn` / `dispatch-task` (Hermes's Agent Orchestrator):

| Task Type | Model | Rationale |
|-----------|-------|-----------|
| Multi-file feature implementation | Sonnet | Complex, multi-step, benefits from dedicated context |
| Verification / audit | Sonnet | Judgment-intensive checks |
| Parallel analysis (security + performance) | Sonnet x 2 | Independent, parallel, specialized |
| Quick file search / grep | Haiku | Under 30s, mechanical |
| Test formatting / lint checks | Haiku | Pattern matching, no judgment needed |
| Evidence bundle review | Sonnet | Quality judgment on measurements |
| Evidence structure audit (file presence only) | Haiku | Mechanical checks |

## WorldAI References (For Context)

These files in `~/worldarchitect.ai/` demonstrate this pattern in production:

- `.claude/commands/planexec.md` - plan-execute with approval, Phase 0 context assessment, `/simplify` final step
- `.claude/agents/evidence-reviewer.md` - Sonnet always (judgment-intensive)
- `.claude/agents/copilot-verifier.md` - Sonnet for checks 3/4 (judgment), Haiku for checks 1/2/5/6/7 (mechanical)
- `.claude/guides/subagent-strategy.md` - 5-criteria delegation framework, 80/20 direct/delegate rule
- `.claude/commands/orchestrate.md` - tmux multi-agent orchestration with opus-master

## Anti-Patterns

- **Do NOT use Opus for subagent work** - waste of tokens
- **Do NOT delegate trivial tasks** - overhead exceeds benefit
- **Do NOT skip the approval gate** on planexec - user must approve before execution
- **Do NOT skip `/simplify`** - Phase 5 is mandatory after any implementation
- **Do NOT create artificial parallelism** - only parallelize when tasks are truly independent
