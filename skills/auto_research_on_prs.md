# Skill: auto_research_on_prs

Implements Karpathy's auto-research technique: autonomously test paper techniques against your own real PR history.

## Core Loop
```
Paper/Technique → Implement → Test on Real PRs → Measure → Iterate → Repeat
```

## Setup

Prerequisites:
- `papers/` directory with experiment subdirectories
- `tests/test_prs.md` — curated PR set (10 PRs covering small/medium/complex/hotfix)
- Python 3.10+ with `gh` CLI authenticated
- Access to LLM Wiki: `wiki/sources/` and `wiki/concepts/`

## Step 1 — Pick a Technique

From the LLM Wiki (`wiki/sources/` or `wiki/concepts/`), select one technique based on priority:

| Priority | Technique | Source |
|---|---|---|
| P0 | Self-Refine / Self-Critique | Self-Refine 2023, ReVeal 2026 |
| P0 | Test-Time Compute / Extended Reasoning | o3/o4, DeepSeek-R1 |
| P1 | Process Reward Models (PRM) | PRM papers |
| P1 | SWE-bench Harness | SWE-bench 2026 |
| P1 | Formal Verification | Lean/Coq/Z3 |
| P1 | TerminalBench-2 Meta-Harness | optimizing the harness itself |
| P2 | ReTool / Reasoning+Tool Interleaving | ReTool papers |
| P2 | Reflexion | verbal reinforcement from failure traces |

Pick P0 first. Skip already-completed experiments (check `papers/experiment_<name>/integration.md` or `abandoned.md`).

## Step 2 — Form a Hypothesis

Create `papers/experiment_<name>/hypothesis.md`:

```markdown
---
experiment: <name>
technique: <paper/technique name>
date: YYYY-MM-DD
---

## Hypothesis

> "If I apply [technique] to my code generation on [PR type], I expect [improvement]."

## Context
- Source: [[wiki/concepts/TechniqueName]]
- Why this technique applies to our codebase
- What gap it fills

## Expected Outcomes
1. ...
2. ...
3. ...

## Success Criteria
- >10% improvement in [metric] OR
- Significant quality gain (define: ...)

## Risks / Threats to Validity
- ...
```

## Step 3 — Implement

Create `papers/experiment_<name>/technique.py`:

### Structure
```python
"""
<technique name> implementation.

Baseline: [what we compare against]
Technique: [what this implements]
"""

# === BASELINE ===
BASELINE_SYSTEM_PROMPT = "..."

# === TECHNIQUE ===
TECHNIQUE_SYSTEM_PROMPT = "..."

# === INVOCATION ===
def invoke_baseline(pr_description: str, diff: str) -> str:
    ...

def invoke_technique(pr_description: str, diff: str) -> str:
    ...

# === TEST RUNNER ===
def run_on_prs(pr_numbers: list[str], dry_run: bool = False) -> dict:
    """
    For each PR:
      1. Fetch PR details via gh CLI
      2. Run baseline invocation
      3. Run technique invocation
      4. Compute diff
      5. Apply and test if possible
    Returns dict with per-PR results.
    """
    ...
```

Requirements:
- Core implementation of the technique (not just a prompt wrapper)
- A baseline version for comparison
- Prompt templates for invocation
- gh CLI integration for fetching real PR data
- Output structured results for comparison

## Step 4 — Test on Real PRs

Run `python papers/experiment_<name>/technique.py run` (or equivalent).

The test runner will:
1. Fetch each PR from `tests/test_prs.md` using `gh pr view`
2. Run baseline (no technique) on each PR
3. Run technique version on each PR
4. Measure: pass rate, token usage, iterations, quality

Save outputs:
- `papers/experiment_<name>/baseline_outputs/` — one file per PR
- `papers/experiment_<name>/technique_outputs/` — one file per PR
- `papers/experiment_<name>/before.txt` and `after.txt` — cumulative diffs

## Step 5 — Evaluate

Use ReVeal 2026 evidence standards:

### Correctness
- Did it solve the problem?
- Does the generated code compile?
- Does it pass existing tests?

### Edge Cases
- Boundary conditions handled?
- Null/undefined inputs?
- Empty diffs or trivial PRs?

### Security
- New vulnerabilities introduced?
- Injection vectors from PR descriptions?
- Dependency additions safe?

### Performance
- Token overhead vs improvement?
- Latency impact?
- API cost delta?

### Style
- Fits codebase conventions?
- Naming consistent?
- Documentation adequate?

## Step 6 — Record Results

Create `papers/experiment_<name>/results.md` with the measurement format:

```markdown
## Experiment: <technique>
Date: YYYY-MM-DD
Hypothesis: <what we expected>
PR Set: tests/test_prs.md

### Results
| PR | Baseline | With-Technique | Delta | Notes |
|----|----------|----------------|-------|-------|
| ... | ... | ... | ... | ... |

### Per-PR Analysis
... detailed notes per PR ...

### ReVeal Evidence Check
- Correctness: ...
- Edge cases: ...
- Security: ...
- Performance: ...
- Style: ...

### Summary
- Pass rate improvement: X%
- Token overhead: ±Y%
- Decision: KEEP | ABANDON
```

Also output structured JSONL to `papers/experiment_<name>/results.jsonl`:

```jsonl
{"pr": "123", "baseline_pass": true, "technique_pass": true, "delta": "improvement", "tokens_baseline": 1234, "tokens_technique": 1500}
{"pr": "124", "baseline_pass": false, "technique_pass": true, "delta": "improvement", "tokens_baseline": 800, "tokens_technique": 2100}
```

## Step 7 — Integrate or Abandon

### KEEP (>10% improvement or significant quality gain)
Create `papers/experiment_<name>/integration.md`:

```markdown
---
experiment: <name>
decision: KEEP
date: YYYY-MM-DD
improvement: X%
---

## Integration Decision

Technique `<name>` is adopted.

## How to Use
... usage instructions ...

## Changes Made
- ... files modified ...
- ... new patterns added ...

## Known Limitations
... what the technique doesn't cover ...
```

Update `wiki/syntheses/` with a synthesis of findings.

### ABANDON (no improvement)
Create `papers/experiment_<name>/abandoned.md`:

```markdown
---
experiment: <name>
decision: ABANDON
date: YYYY-MM-DD
reason: ...
---

## Abandonment Decision

Technique `<name>` did not meet success criteria.

## What Was Tried
... brief summary ...

## Why It Failed
... reasons ...

## Lessons Learned
... what we learned about our codebase ...
```

## Test PR Set Format

`tests/test_prs.md` should be a curated list:

```markdown
# Test PR Set

## Selection Criteria
- 2 small fixes (1-5 files, <100 lines)
- 3 medium features (4-8 files, 100-500 lines)
- 3 complex integrations (10+ files, 500+ lines)
- 2 post-merge hotfixes (regressions caught after merge)

## PRs
| # | Type | Files | Lines | Description |
|---|------|-------|-------|-------------|
| PR-123 | small-fix | 3 | 45 | Fix null pointer in auth |
| ... | ... | ... | ... | ... |
```

## Technique Priority Queue

1. **Self-Refine / Self-Critique (P0)** — ReVeal 2026 + Self-Refine 2023
   - Key paper: Self-Refine: Iterative Refinement with Self-Feedback (2023)
   - Core: Generate → Critique → Refine loop
   - Expected: Better edge case handling, fewer regressions

2. **Test-Time Compute / Extended Reasoning (P0)** — o3/o4, DeepSeek-R1
   - Key paper: DeepSeek-R1: Incentivizing Reasoning Capability in LLMs
   - Core: Extended thinking tokens, chain-of-thought with verification
   - Expected: Better complex PR handling, fewer missed requirements

3. **Process Reward Models (P1)** — PRM for step-level feedback
   - Key paper: PRM papers from wiki/concepts/ProcessRewardModels.md
   - Core: Step-level grading during generation
   - Expected: Fewer wasted tokens on wrong paths

4. **SWE-bench Harness (P1)** — real-world issue → test → fix loop
   - Key paper: SWE-bench 2026
   - Core: Structured test harness for issue→test→fix cycles
   - Expected: Better regression coverage, faster debug loops

5. **Formal Verification (P1)** — Lean/Coq/Z3 for correctness-critical code
   - Key papers: wiki/concepts/Lean.md, wiki/concepts/Coq.md, wiki/concepts/Isabelle.md
   - Core: Formal specs + proof for critical code paths
   - Expected: Zero regressions on critical paths

6. **ReTool / Reasoning+Tool Interleaving (P2)**
   - Core: Interleave reasoning with tool calls
   - Expected: Better debugging, faster root cause identification

7. **TerminalBench-2 Meta-Harness (P1)** — optimizing the harness itself
   - Key paper: wiki/concepts/TerminalMemoryAgents.md
   - Core: Meta-level optimization of the testing harness
   - Expected: Faster iteration, better signal

8. **Reflexion (P2)** — verbal reinforcement from failure traces
   - Key paper: wiki/concepts/Reflexion.md
   - Core: Verbal reflection on failure to improve next attempt
   - Expected: Better recovery from failed attempts

## Automation

To run a full experiment cycle:

```bash
# Pick and implement
python -c "from skills.auto_research_on_prs import pick_next_technique; print(pick_next_technique())"

# Run test suite
python papers/experiment_<name>/technique.py run --prs tests/test_prs.md

# Generate results
python papers/experiment_<name>/technique.py evaluate
```

## Wiki Integration

After each experiment:
1. Update `wiki/syntheses/` with findings
2. If oracle impact found, append to `wiki/log.md`
3. Link new patterns to relevant concept pages
