# Evidence Review Schema — Two-Stage Verification Pipeline

**Bead:** orch-j9e0 (P0 epic)
**Status:** Design
**Date:** 2026-03-16

## Problem

The current evidence gate (condition 6) checks for a comment containing "**PASS**" on the PR. This has three flaws:

1. **No audit trail** — PR comments are editable, deletable, and not git-tracked
2. **No independence** — the coding agent can post "evidence **PASS**" on its own work
3. **No schema** — evidence artifacts (test logs, traces, screenshots) aren't collected in a standard location

Result: condition 6 is the weakest of the 7 merge conditions. An agent can rubber-stamp its own work.

## Design

### Evidence Directory Schema

```
docs/evidence/
  {repo}/                          # e.g., jleechanclaw
    {PR-NNN}/                      # e.g., PR-255
      {YYYYMMDD}_{HHMM}_utc/      # e.g., 20260317_0500_utc (one run)
        claims.md                  # what the agent claims it accomplished
        artifacts/                 # raw proof: test output, logs, screenshots, traces
          pytest_output.txt
          ci_check_runs.json
          coderabbit_review.json
          gateway_traces.jsonl
        self_review.md             # agent's /er output (stage 1)
        independent_review.md      # independent LLM reviewer verdict (stage 2)
        verdict.json               # machine-readable final verdict
```

### `verdict.json` Schema

```json
{
  "pr": 255,
  "repo": "jleechanorg/jleechanclaw",
  "timestamp": "2026-03-16T21:00:00-07:00",
  "stage1": {
    "status": "PASS",
    "reviewer": "self",
    "model": "claude-4.5-sonnet",
    "iterations": 2,
    "claims_verified": 5,
    "claims_failed": 0
  },
  "stage2": {
    "status": "PASS",
    "reviewer": "independent",
    "model": "codex/gpt-5.3-codex",
    "findings": [],
    "confidence": 0.95,
    "independence_verified": true,
    "model_family_differs_from_stage1": true
  },
  "coderabbit": {
    "status": "COMMENTED",
    "critical_findings": 0,
    "major_findings": 0
  },
  "overall": "PASS"
}
```

### Two-Stage Pipeline

```
┌─────────────────────────────────────────────────────────────────┐
│                    STAGE 1: Agent Self-Review                    │
│                                                                  │
│  1. Agent completes coding task                                  │
│  2. Agent runs /er against own work:                            │
│     - Collects pytest output → artifacts/pytest_output.txt       │
│     - Fetches CI status → artifacts/ci_check_runs.json           │
│     - Fetches CR review → artifacts/coderabbit_review.json       │
│     - Maps claims to artifacts in claims.md                      │
│  3. Agent writes self_review.md with verdict                     │
│  4. If FAIL: agent fixes issues and reruns (max 3 iterations)    │
│  5. If PASS: commits evidence bundle, moves to stage 2           │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│              STAGE 2: Independent LLM Verification               │
│                                                                  │
│  Triggered automatically when stage 1 PASS is committed.         │
│  MUST use a different model/context than the coding agent.       │
│                                                                  │
│  1. Read claims.md + artifacts/ (full context)                   │
│  2. Verify: does each claim have supporting evidence?            │
│  3. Check for:                                                   │
│     - Circular citations (claim cites itself)                    │
│     - Empty/missing artifacts                                    │
│     - Statistical weakness                                       │
│     - Unverified assertions                                      │
│  4. Write independent_review.md with PASS/FAIL                  │
│  5. Write verdict.json with both stages                          │
│  6. If PASS: post PR comment with link to evidence bundle        │
│  7. If FAIL: post PR comment with specific failures              │
│                                                                  │
│  Independent reviewers (in priority order):                      │
│  - Codex CLI (codex exec) — preferred, different model family    │
│  - Claude CLI with different system prompt                       │
│  - Gemini CLI as third-party fallback                            │
└─────────────────────────────────────────────────────────────────┘
```

### Merge Gate Integration

The `check_evidence_pass()` function in `src/orchestration/merge_gate.py`:
- Checks for `docs/evidence/{repo}/PR-{N}/*/verdict.json` in the PR's changed files
- Requires `overall == "PASS"` AND `stage2.status == "PASS"` with independence verified
- Legacy PASS comment fallback has been **removed** — all code PRs require a full two-stage evidence bundle

> **Implementation note:** The gate logic lives in `src/orchestration/merge_gate.py`,
> not in the shell scripts (`scripts/ao-pr-poller.sh`, `scripts/ao-backfill.sh`).
> The shell scripts call into the Python gate via `python -m orchestration.merge_gate`.

```python
def check_evidence_pass(owner, repo, pr_number):
    files = get_pr_files(owner, repo, pr_number)  # paginated

    # Only require evidence for PRs touching code paths
    code_files = [f for f in files if is_code_path(f)]
    if not code_files:
        return PASS  # docs-only PRs skip evidence

    # Scope to THIS repo + PR only — prevents stale/unrelated verdicts
    pr_evidence_prefix = f"docs/evidence/{repo}/PR-{pr_number}/"
    evidence_files = [f for f in files if f.startswith(pr_evidence_prefix)]
    if not evidence_files:
        return FAIL  # code PR with no evidence bundle

    verdict_files = [f for f in evidence_files if f.endswith("verdict.json")]
    if not verdict_files:
        return FAIL  # evidence dir exists but no verdict

    # Sort by timestamp in filename to get the latest run (newest first)
    verdict_files = sorted(verdict_files, reverse=True)

    # Read the latest verdict.json from the PR head (ref-pinned, not default branch)
    verdict = read_verdict_from_pr(verdict_files[0], ref=pr_head_sha)

    # Stage 1: must pass (fail-closed if missing)
    if verdict.get("stage1", {}).get("status") != "PASS":
        return FAIL  # stage 1 self-review failed

    if verdict["overall"] != "PASS" or verdict["stage2"]["status"] != "PASS":
        return FAIL  # independent reviewer didn't sign off

    # Verify independence: the GATE checks the dispatcher's signed metadata,
    # not the stage-2 actor's self-attestation. The dispatcher writes these
    # fields based on which model was actually invoked (see orch-j9e0.4).
    if not verdict.get("stage2", {}).get("independence_verified", False):
        return FAIL  # dispatcher did not verify independence
    if not verdict.get("stage2", {}).get("model_family_differs_from_stage1", False):
        return FAIL  # same model family as stage1

    return PASS
```

> **Design note on independence fields:** `independence_verified` and
> `model_family_differs_from_stage1` are written by the **dispatcher** (the
> orchestration layer that invokes the stage-2 reviewer), NOT by the stage-2
> model itself. The dispatcher knows which model it invoked and can
> cryptographically attest this. A stage-2 reviewer cannot forge these fields
> because it never writes `verdict.json` directly — the dispatcher merges
> stage-2 results into the existing verdict. See orch-j9e0.4 for implementation.

### Which PRs Require Evidence?

Evidence bundles are required when the PR changes files in:
- `src/orchestration/` — core automation logic
- `scripts/` — shell automation
- `lib/` — shared libraries
- `SOUL.md`, `TOOLS.md` — policy files

Evidence bundles are NOT required for:
- `docs/` or `roadmap/` only changes
- `.beads/` only changes
- `launchd/` plist templates
- `agents/` model configs
- Test-only changes (`src/tests/`, `tests/`)

### Agent Self-Review Protocol (Stage 1)

Added to `AGENT_BASE_RULES.md`:

```markdown
## Evidence Generation

After completing your task, before declaring done:

1. Create evidence directory: `docs/evidence/{repo}/PR-{N}/{date}_{time}_utc/`
2. Write `claims.md`: list each change and what it accomplishes
3. Collect artifacts:
   - `python -m pytest ... > artifacts/pytest_output.txt 2>&1`
   - `gh api repos/{owner}/{repo}/commits/{sha}/check-runs > artifacts/ci_check_runs.json`
   - `gh api repos/{owner}/{repo}/pulls/{N}/reviews > artifacts/coderabbit_review.json`
4. Run /er and save output to `self_review.md`
5. If /er returns FAIL: fix issues, recollect artifacts, rerun (max 3 times)
6. If /er returns PASS: write `verdict.json` with stage1 PASS, commit bundle
7. Stage 2 (independent review) runs automatically after your commit
```

### Independent Reviewer Dispatch (`stage2_reviewer.py`)

Implemented in `src/orchestration/stage2_reviewer.py`. Can run standalone or via `evidence_bundle.py --stage2`.

**Reviewer priority order** (must be different model family than stage 1):
1. **Codex CLI** (`codex exec --yolo`) — OpenAI family, preferred
2. **Gemini CLI** (`gemini`) — Google family, second choice
3. **Claude CLI** (`claude -p`) — Anthropic family, last resort (only if stage 1 used a different family)

**How it works:**

1. Reads `claims.md` + all `artifacts/` from the evidence bundle
2. Builds a structured prompt asking the reviewer to verify each claim against artifacts
3. Dispatches to the first available CLI from a different model family
4. Parses PASS/FAIL verdict and findings from the reviewer's markdown output
5. **Dispatcher** (this module) writes independence attestation fields — the reviewer never touches these:
   - `stage2.independence_verified: true` — set by dispatcher, not reviewer
   - `stage2.model_family_differs_from_stage1: true` — dispatcher compares families
6. Writes `independent_review.md` to the bundle directory
7. Updates `verdict.json` with stage 2 results and overall verdict

**Usage:**
```bash
# Standalone stage 2
python -m orchestration.stage2_reviewer docs/evidence/repo/PR-265/20260317_0916_utc/verdict.json

# Combined stage 1 + stage 2
python -m orchestration.evidence_bundle owner repo 265 --stage2 --push

# Force specific reviewer
python -m orchestration.stage2_reviewer verdict.json --model gemini
```

**Independence guarantee:** The merge gate checks `independence_verified` and
`model_family_differs_from_stage1` — both written by the dispatcher, not the
reviewer. A compromised reviewer cannot forge these fields because it only
produces `independent_review.md` text. The dispatcher parses that text and
controls what goes into `verdict.json`.

## Implementation Plan

| Phase | Task | Bead | Status |
|-------|------|------|--------|
| 1 | Create evidence directory schema + verdict.json spec | orch-j9e0.1 | **Done** (PR #265) |
| 2 | Update AGENT_BASE_RULES.md with evidence generation protocol | orch-j9e0.2 | **Done** (PR #265) |
| 3 | Build evidence_bundle.py generator + commit_and_push() | orch-j9e0.3 | **Done** (PR #265) |
| 4 | Build independent reviewer dispatcher (codex → gemini → claude fallback) | orch-j9e0.4 | **Done** (PR #265) |
| 5 | Update merge_gate.py check_evidence_pass() to read verdict.json | orch-j9e0.5 | **Done** (PR #265) |
| 6 | Remove legacy PASS comment fallback — enforce full two-stage pipeline | orch-j9e0.6 | **Done** (PR #265) |
| 7 | Backtest: run pipeline on 5 recent PRs, verify verdicts | orch-j9e0.7 | Pending |

## Success Criteria

- Every code PR has a `docs/evidence/` bundle before merge
- Stage 2 reviewer catches at least 1 issue that stage 1 missed (in backtest)
- No agent can rubber-stamp its own work — stage 2 is always a different model/context
- Evidence bundles are git-tracked and auditable
- Docs-only PRs skip evidence (no unnecessary overhead)
