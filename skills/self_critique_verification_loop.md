# Self-Critique + Verification Loop — Karpathy Auto-Research Edition

Apply structured self-critique + revision loops to code generation, using Karpathy's auto-research technique (paper → implement → test → iterate) tested against your own real PR history.

**Inspired by**: ReVeal (2026), Self-Refine (Madaan et al., NeurIPS 2023), and Karpathy's method of implementing papers from scratch and iterating on real code.

## The Core Loop

```
Paper/Technique → Implement → Test on Real PRs → Measure → Iterate → Repeat
```

This is a **closed research loop**: you feed a new technique in, test it against your actual production harness using your own historical PRs, measure delta, and iterate.

## Phase 1 — Paper/Technique Ingest

Read the paper or technique description thoroughly. Form a hypothesis:

> "If I apply this technique to my code generation pipeline, I expect [specific improvement] on [specific PR type]."

Store the hypothesis in `papers/experiment_<name>/hypothesis.md`.

## Phase 2 — Implement

Generate the initial implementation of the technique.

**Karpathy-style**: Don't just read the paper and agree with it. Re-implement the core idea from scratch with your own code. This forces deep understanding and often surfaces edge cases the paper glosses over.

For each technique, create:
- `papers/experiment_<name>/technique.py` — the core technique implementation
- `papers/experiment_<name>/prompt_template.md` — how to invoke it in the generation loop
- `papers/experiment_<name>/baseline.py` — what the baseline (no technique) looks like

## Phase 3 — Test on Real PRs

Run the technique against a set of real PRs from your history (see `test_prs.md`).

**Measurement**:
```
For each PR:
  1. Run baseline generation (no self-critique)
  2. Run technique generation (with self-critique)
  3. Compare: pass rate, iterations, token usage, time to first correct output
```

Capture:
- `papers/experiment_<name>/results.jsonl` — structured results per PR
- `papers/experiment_<name>/before.txt` — baseline output
- `papers/experiment_<name>/after.txt` — technique output
- `papers/experiment_<name>/diff.md` — what changed and why

## Phase 4 — Self-Critique + Revision

Critique the technique's performance on your PRs.

Prompt:
```
You ran technique X on N real PRs. Analyze:

1. Where did the technique HELP? (specific PRs, specific improvements)
2. Where did the technique HURT? (regression, wasted iterations, false positives)
3. Where did it NOT HELP but you expected it to?
4. What does this tell you about the technique's适用范围 (scope)?
5. What would you change about the technique implementation?

Evidence standards (ReVeal 2026):
- Correctness: Did it solve the stated problem?
- Edge cases: Did it handle boundary conditions?
- Security: Any new vulnerabilities introduced?
- Performance: Token/time overhead vs. improvement?
- Style: Does it fit the codebase conventions?
```

## Phase 5 — Iterate or Abandon

**If technique shows measurable improvement (>10% on pass rate OR significant quality gain)**:
- Integrate into harness production prompt
- Document the improvement in `papers/experiment_<name>/integration.md`
- Add to beads tracking as a new pattern

**If technique shows no improvement or regression**:
- Document why in `papers/experiment_<name>/abandoned.md`
- Note which PR types it failed on (for future reference)
- Do not integrate

## Technique Inventory (2025-2026 Papers to Test)

| # | Technique | Paper/Source | Priority |
|---|---|---|---|
| 1 | Self-Refine / Self-Critique | Madaan et al. NeurIPS 2023 + ReVeal 2026 | P0 |
| 2 | Test-Time Compute / Extended Reasoning | o3/o4, DeepSeek-R1, GLM-4.7 (2026) | P0 |
| 3 | Process Reward Models | Inside RL, PRM paper | P1 |
| 4 | SWE-bench Verified Harness | SWE-bench 2026 | P1 |
| 5 | Formal Verification (Lean/Coq/Z3) | COBALT pipeline, Z3 | P1 |
| 6 | ReTool (Reasoning + Tool Interleaving) | agentic reasoning survey 2024 | P2 |
| 7 | TerminalBench-2 Harness Optimization | Meta-Harness paper | P1 |
| 8 | Reflexion (verbal reinforcement) | Shinn et al. | P2 |

## Test PR Set

See `test_prs.md` for the curated list of PRs to use as the test harness. The PRs are selected to cover:
- Small fixes (1-5 files, <50 lines changed)
- Medium features (5-15 files, moderate complexity)
- Complex integrations (threading, security, data integrity)
- Post-merge regressions or hotfixes

## Exit Criteria

The loop terminates when:
- All 5 evidence standards PASS (ReVeal criteria), OR
- Max 4 iterations reached, OR
- Technique is clearly abandoned (no measurable improvement)

## Integration with Existing Harness

| What You Have | How It Connects |
|---|---|
| Skeptic agent | External verifier after self-critique loop |
| Beads tracking | Patterns that fail self-critique get flagged for bead creation |
| Evidence review gate | Skeptic + self-critique = full verification stack |
| CI test harness | Tests from Phase 2 can be added to CI as regression coverage |
| Reasoning budget (MetaHarness) | Extended thinking tokens as a technique variant |
