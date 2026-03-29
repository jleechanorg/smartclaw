# Agent Reviewer Loop — Two-Agent PR Review via MCP Mail

**Status:** Phase 2 Complete
**Priority:** P0 (Critical)
**Created:** 2026-03-18
**Beads:** orch-tlkk (epic), orch-tlkk.1 (reviewer core), orch-tlkk.2 (AO integration), orch-tlkk.3 (deprecate evidence pipeline)
**Replaces:** Evidence pipeline (evidence_bundle.py + stage2_reviewer.py + verdict.json)

## Problem

The Python evidence pipeline (PR #265) works but is brittle:
- Custom verdict.json format that nothing else understands
- CLI dispatch fragility (codex/gemini/claude binary availability)
- Evidence bundles committed as files in the repo (noise)
- No feedback loop — stage 2 fails silently, no coder fix cycle
- Nothing forces the coder agent to generate evidence before declaring done

## Solution: Two-Agent Review Loop

Replace the evidence pipeline with a **reviewer agent** that:
1. Is dispatched by AO when a PR is ready for review
2. Checks all 6 green conditions via merge_gate.py
3. Reviews the diff independently (different model family than coder)
4. Communicates findings to the coder via MCP mail
5. The coder fixes issues and mails back
6. Reviewer re-checks, posts GitHub APPROVED review when satisfied

### Architecture

```
CODER AGENT (AO-dispatched)          REVIEWER AGENT (AO-dispatched)
────────────────────────────          ──────────────────────────────
1. Write code, push PR
2. MCP mail → reviewer:
   "PR #N ready for review"
                                      3. Receives mail
                                      4. Runs merge_gate (6 conditions)
                                      5. Reviews diff (different model)
                                      6. If issues → mail findings to coder
7. Reads mail, fixes issues     ←──
8. Pushes fix, mails "fixed"    ──→
                                      9. Re-runs merge_gate + re-reviews
                                      10. Posts GitHub APPROVED review
                                      11. Mails coder: "approved"
12. Posts "PR is green ✅"
```

### What stays

- **merge_gate.py** — the 6-condition checker (solid, tested, keep as-is)
- **.coderabbit.yaml** — CR APPROVED config
- **MCP mail infrastructure** — already exists, agents can register and communicate
- **AO dispatch** — already spawns agents in worktrees

### What changes

- **Condition 6 (evidence)** → replaced by reviewer agent's GitHub APPROVED review
  - merge_gate checks for a review from a known reviewer bot/agent identity
  - No more verdict.json, evidence bundles, or file-based evidence
- **agent-orchestrator.yaml** — new reaction to dispatch reviewer when coder signals ready
- **Coder agentRules** — updated to mail reviewer instead of running evidence_bundle

### What's new

- `src/orchestration/reviewer_agent.py` — reviewer logic:
  - Register as MCP mail agent
  - Run merge_gate to check 6 conditions
  - Review PR diff (call gh pr diff, analyze changes)
  - Post findings via MCP mail to coder
  - Post GitHub PR review (APPROVED or REQUEST_CHANGES)
  - Loop until satisfied
- AO reaction config for reviewer dispatch

## Implementation Plan

### Phase 1: Reviewer agent core (PR #268 — DONE)
- [x] `reviewer_agent.py` — checks merge_gate, reviews diff, posts GitHub review
- [x] Tests for reviewer logic (28 tests)
- [x] StrEnum for severity/verdict, _is_test_path heuristic

### Phase 2: MCP mail + AO integration (this PR — DONE)
- [x] `mcp_mail.py` — thin wrapper for send/receive via mcporter CLI
- [x] `reviewer_agent.py` — wired MCP mail send_mail to coder_agent
- [x] AO reaction `review-requested` to dispatch reviewer
- [x] merge_gate condition 6 — accepts reviewer agent's GitHub APPROVED as alternative to verdict.json
- [x] Tests for mcp_mail (9 tests) and reviewer APPROVED gate (3 tests)

### Phase 3: Deprecate evidence pipeline (deferred)
- [ ] Remove evidence_bundle.py, stage2_reviewer.py
- [ ] Remove verdict.json handling from merge_gate
- [ ] Clean up docs/evidence/ artifacts
- Note: verdict.json path remains in merge_gate as fallback — safe to keep until Phase 3

## Design Decisions

1. **Reviewer posts real GitHub PR review** — not a comment, not a file. This means
   merge_gate can check it the same way it checks CR (reviews API).

2. **MCP mail for coordination** — not Slack, not comments. MCP mail is agent-native,
   structured, and doesn't pollute the PR comment thread.

3. **Different model family enforced by AO config** — the reviewer agent is configured
   to use a different model than the coder (e.g., coder=claude, reviewer=codex/gemini).
   No runtime check needed — it's a deployment constraint.

4. **merge_gate stays Python** — it's well-tested infrastructure. The reviewer agent
   uses it as a library, not replaces it.
