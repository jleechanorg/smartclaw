# Harness Engineering Skill

## Purpose

When a mistake or failure pattern is identified, analyze whether the root cause is a gap in the **harness** (instructions, skills, tests, guardrails, automation) rather than just the code. Produce a concrete fix at the harness level so the same class of mistake cannot recur without human intervention.

**Canonical reference:** `~/.claude/skills/harness-engineering/SKILL.md` — this file is the master copy maintained in the Claude skills directory. This Hermes skill acts as a reference wrapper; always read the canonical file for the full protocol.

## Scope

- **Hermes workspace:** `~/.hermes/skills/harness/SKILL.md` (this file)
- **Canonical source:** `~/.claude/skills/harness-engineering/SKILL.md`
- **Collision:** If the canonical file is more recent or has additional guidance, prefer it over any stale local copy.

## Harness Layers (ordered by durability)

| Layer | Files | What it prevents |
|-------|-------|-----------------|
| **Instructions** | `SOUL.md`, `AGENTS.md`, `~/.claude/CLAUDE.md` | Wrong approach, wrong assumptions, wrong defaults |
| **Skills** | `~/.hermes/skills/*.md`, `~/.claude/skills/*.md` | Repeated manual workflows, forgotten validation steps |
| **Memory** | `mem0`, `memory/` | Forgetting user preferences, past corrections, project context |
| **Integration tests** | `tests/test_*.py` | Regressions in real behavior |
| **CI gates** | `.github/workflows/*.yml`, cron jobs | Merging broken code, mislabeled artifacts |

## Analysis Protocol

When invoked, execute this sequence **in full, every time**:

### Step 1: Identify the failure class

Classify what went wrong:
- **Mislabeled artifact** — something was called X but didn't meet X's criteria
- **Wrong approach** — took an approach the user has previously corrected
- **Missing validation** — produced output without checking it meets requirements
- **Repeated manual fix** — user had to manually correct the same type of issue more than once
- **Silent degradation** — something broke but nothing flagged it (includes: harness layer present but broken)
- **Knowledge gap** — didn't know about a constraint, convention, or tool
- **LLM path error** — the agent reasoned toward a wrong solution despite having sufficient context

### Step 2: 5 Whys — the technical problem

Ask "Why?" five times about the technical failure, drilling into root cause:

```
Why 1: Why did the observable failure happen?
Why 2: Why did the mechanism that caused Why 1 exist?
Why 3: Why wasn't that mechanism caught or prevented?
Why 4: Why wasn't there a guardrail at that level?
Why 5: Why was the system designed without that guardrail?
→ Root cause: <single sentence>
```

### Step 3: 5 Whys — the agent path

Ask "Why?" five times about why **the LLM** went down the wrong path. **This is mandatory.** Every harness failure has two dimensions: the technical problem AND the agent reasoning failure that let it slip through.

```
Why 1: Why did the agent not catch/prevent the failure?
Why 2: Why did the agent reason or act that way?
Why 3: Why didn't the agent's instructions prevent that reasoning?
Why 4: Why wasn't there a skill, memory, or rule that would have redirected the agent?
Why 5: Why was the harness incomplete for this class of agent behavior?
→ Agent root cause: <single sentence>
```

Key questions:
- Did the agent **trust existing code/context** without verifying it was sufficient?
- Did the agent describe the problem correctly but at the wrong level of abstraction?
- Did the agent assume "context present = task clear" when it wasn't?
- Did the agent skip execution because it second-guessed itself instead of acting?
- **Did the agent verify every relevant context source before asking for clarification?**

### Step 4: Find the harness gap

For each failure class, check which harness layers are missing or insufficient:

1. **Read existing instructions** — `~/.hermes/SOUL.md`, `~/.hermes/AGENTS.md`, `~/.claude/CLAUDE.md`
   - Is the rule already documented? If yes → it's an adherence problem, add a stronger enforcement instruction
   - If no → add the rule
2. **Check for existing skills** — `~/.hermes/skills/`, `~/.claude/skills/`
   - Is there a skill that should have caught this? If yes → update it
   - If no and the pattern is repeatable → create a skill
3. **Check memory** — mem0, `~/.hermes/memory/`
   - Was this corrected before? If yes → strengthen the memory
   - If no → save feedback memory
4. **Check tests** — are there tests that would catch this regression?
5. **Check CI** — would CI have caught this before merge?

**Critical check — harness layer present but broken:**
For each harness layer that exists, verify it **actually works**, not just that it exists.

### Step 5: Propose the fix

```
FAILURE CLASS: <classification>

5 WHYS — TECHNICAL:
1. <why>
2. <why>
3. <why>
4. <why>
5. <why>
→ Root cause: <sentence>

5 WHYS — AGENT PATH:
1. <why>
2. <why>
3. <why>
4. <why>
5. <why>
→ Agent root cause: <sentence>

HARNESS FIXES (in order of priority):
1. [LAYER] FILE: <path> — <what to add/change>
2. [LAYER] FILE: <path> — <what to add/change>
...

VERIFICATION: <how to confirm the fix prevents recurrence>
```

### Step 6: Implement

After user approval:
- Apply all harness fixes
- Run verification
- Report what was changed

## Decision Rules

- **If the same correction has been given twice**: This is a mandatory harness fix. No exceptions.
- **If the fix is a one-liner in code but the pattern could recur**: Harness fix first, code fix second.
- **If unsure whether it's a harness gap or a one-off**: Ask the user.
- **Never add instructions that duplicate what's already documented**: Check first.
- **Prefer the most durable layer**: Instructions > Skills > Memory > Tests > CI
- **5 Whys are mandatory**: Never skip them. Short-circuit analysis produces shallow fixes.
- **Agent path is mandatory**: Never analyze only the technical dimension.

## Anti-patterns

- Adding a memory entry when the fix should be an instruction
- Writing a skill for a one-time operation
- Skipping the agent 5 Whys because "the technical fix is obvious"
- Assuming a harness layer works because it exists — verify it
- Responding with a clarification question when all necessary context is already available

## See also

- Canonical harness engineering skill: `~/.claude/skills/harness-engineering/SKILL.md`
- Harness command: `/harness` (defined in `~/.claude/commands/harness.md`)
