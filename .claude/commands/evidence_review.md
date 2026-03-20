---
name: evidence_review
aliases:
  - er
description: Run an independent evidence review on a PR using Codex. Reviews evidence bundles against project standards, checks for circular citations, weak statistics, and missing artifacts. Posts verdict (PASS/WARN/FAIL) as a PR comment.
type: git
execution_mode: background
---

## ⚡ EXECUTION INSTRUCTIONS FOR CLAUDE

When this command is invoked, run an evidence review on the specified PR:

### Step 1: Identify the PR

1. If a PR number is provided, use it
2. If no PR number is provided, check the current branch for an associated PR:
   ```bash
   gh pr view --json number,title,url
   ```

### Step 2: Find Evidence Files

1. Look for evidence files in common locations:
   - `docs/evidence/`
   - `evidence/`
   - `docs/pr-validation/`
   - Any `*.evidence.md` files

2. If evidence path is provided as argument, use that directly

### Step 3: Run Evidence Review

1. Create a Codex sub-agent session to perform the review:
   ```
   Use the evidence-reviewer agent prompt to audit the evidence bundle
   ```

2. The review should check:
   - Structure: required files present (evidence.md, metadata.json, methodology.md, artifacts/)
   - Integrity: no self-referential claims, raw artifacts exist
   - Measurement: statistical adequacy (N≥10), variance reported
   - Methodology: warm-up, cache state, connection settings documented

### Step 4: Post Verdict

1. Format the verdict as a PR comment:
   ```
   ## Evidence Review Result

   **Verdict: PASS|WARN|FAIL**

   [Summary of findings]

   ### Details
   - Phase 1 (Structure): PASS|FAIL
   - Phase 2 (Integrity): PASS|FAIL  
   - Phase 3 (Measurement): PASS|WARN|FAIL

   [Violations if any]
   [Recommendations if any]
   ```

2. Post as PR comment:
   ```bash
   gh pr comment <pr_number> --body "<verdict>"
   ```

### Arguments

- `[pr_number]` - Optional PR number (defaults to current branch's PR)
- `[evidence_path]` - Optional path to evidence file/directory

### Examples

```
/er                    # Review current branch's PR
/er 123               # Review PR #123
/er 123 docs/evidence  # Review PR #123 with evidence in docs/evidence/
/er docs/pr-validation/pr-123-evidence.md
```
