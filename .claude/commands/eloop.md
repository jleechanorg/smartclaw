---
name: evolve_loop
description: 12-hour autonomous evolution loop — observe, measure, diagnose, fix, repeat every 10min. Drives zero-touch rate up by finding friction and dispatching fixes.
type: skill
---

## Purpose

Autonomous self-improving loop that observes the AO ecosystem, measures zero-touch rate, diagnoses friction, creates beads for gaps, dispatches fixes via /claw, and records everything. Runs via `/loop 10m` for max 12 hours.

---

## Adaptive Behavior — NOT Every Phase Every Cycle

This loop is problem-driven. If the system is healthy:
- **Healthy cycle** (~30s): Observe → Measure → Recap "all good, waiting"
- **Problem cycle** (~5min): Observe → Measure → Diagnose → Plan → Record → Fix → Recap

Decision tree after Phase 2 (Measure):
- Zero-touch rate unchanged AND no new friction AND all workers alive AND no system gaps from Phase 1e → SKIP to Phase 7 (Recap)
- New dead worker or new PR failure → run Phase 3-6
- Worker stuck (same output 3 checks) → kill + respawn, skip full diagnosis
- Build broken on main → fix immediately, skip /harness
- **System gaps found in Phase 1e → run Phase 3a-system + full Phase 3-6 even if PR-level metrics look healthy**

---

## Loop Body (executed every 10 minutes)

### Phase 1: OBSERVE — System State Snapshot

**1a. Run /auton** — get autonomy diagnostic across all repos.

**1b. Check AO workers** — for each repo AO manages:
- `jleechanorg/agent-orchestrator` (primary)
- `jleechanorg/worldarchitect` (worldai — GitHub repo is `worldarchitect`, not `worldai_claw`)
- `jleechanorg/smartclaw` (orchestration — deprecated but may have active workers)
- Antigravity orchestrator (special — check launchd daemon status)

```bash
# List active AO sessions
tmux list-sessions 2>/dev/null | grep -E '(ao|jc|wa|cc|ra|wc)-[0-9]+'

# Per-repo open PRs — use GitHub repo names, not AO project slugs
for gh_repo in jleechanorg/agent-orchestrator jleechanorg/worldarchitect jleechanorg/smartclaw; do
  gh api "repos/$gh_repo/pulls?state=open&per_page=20" \
    --jq '.[]|"\(.number) \(.head.ref) \(.mergeable_state)"' 2>/dev/null
done
```

**1c. Read worker tmux conversations** — capture last 30 lines from each active ao-* worker. Look for:
- Stuck patterns (same output for 10+ min)
- Error loops (repeated failures)
- Waiting for input
- Context exhaustion (>80%)

```bash
for sess in $(tmux list-sessions -F '#{session_name}' 2>/dev/null | grep -E '^(ao|jc|wa|cc|ra|wc)-[0-9]+$'); do
  echo "=== $sess ==="
  tmux capture-pane -t "$sess" -p 2>/dev/null | tail -30
done
```

**1d. Read fiction/novel entries** — workers write friction narratives in `novel/` and `docs/novel/`:
```bash
# Check for recent novel entries (last 24h)
[ -f /tmp/evolve_loop_last_run ] || touch -t 197001010000 /tmp/evolve_loop_last_run
find novel/ docs/novel/ -name '*.md' -newer /tmp/evolve_loop_last_run 2>/dev/null
# Read any new entries for friction signals
```

**1e. System harness audit** — check for structural gaps beyond PR health:

1. **Dispatch delivery verification** — detect `/claw` dispatches that died without producing a PR.
   ⚠️ **Known limitation (no-op until /claw is updated)**: `/claw` deletes `.claw-spawn-*` files on every exit
   path (success, failure, send-error, error). Phase 1e cannot detect orphaned dispatches because the files
   it would need to read are always gone by the time this loop runs.
   **Required /claw fix**: write a persistent `/tmp/openclaw/.claw-track-<ISSUE_ID>` file (containing
   `SESSION_NAME`, `PROJECT_ID`, `SPAWNED_AT`) before deleting the spawn output on any exit path.
   **Proxy until that fix lands**: rely on Phase 1b (tmux session listing) and Phase 1c (conversation capture)
   to surface dead workers — a session that disappears between cycles indicates a dispatch that died.

2. **Stale worktree accumulation** — detect before they reach cascade-poison levels:
```bash
# Count AO session worktrees (ao-*, jc-*, etc.) via git worktree list
# to avoid false counts from non-worktree directories matching the prefix pattern.
_worktree_dir="${HOME}/.worktrees"
if [ -d "$_worktree_dir/.git" ]; then
  ao_worktree_count=$(git -C "$_worktree_dir" worktree list --porcelain 2>/dev/null | \
    grep '^worktree ' | awk '{print $2}' | while read p; do
      basename "$p"
    done | grep -E '^(ao|jc|wa|cc|ra|wc)-[0-9]+' | wc -l)
else
  # Fallback: scan all subdirs and verify each is a git worktree (has .git file/dir)
  ao_worktree_count=$(ls -d "$_worktree_dir"/*/ 2>/dev/null | while read d; do
    [ -f "$d/.git" ] || [ -d "$d/.git" ] || continue
    basename "$d"
  done | grep -E '^(ao|jc|wa|cc|ra|wc)-[0-9]+' | wc -l)
fi
# Alert if >10 worktrees exist (healthy system has <5)
if [ "${ao_worktree_count:-0}" -gt 10 ]; then
  echo "[SYSTEM GAP] $ao_worktree_count AO worktrees exist — potential cascade-poison risk"
fi
```

3. **Lifecycle-worker health** — verify managed by launchd, not orphaned:
```bash
lw_count=$(ps aux | grep -v grep | grep -c 'lifecycle-worker' 2>/dev/null || echo 0)
if command -v launchctl >/dev/null 2>&1; then
  launchd_state=$(launchctl print gui/$(id -u)/com.agentorchestrator.lifecycle-agent-orchestrator 2>&1 | grep 'state =' || echo "not found")
else
  launchd_state="unsupported-platform"
fi
if [ "$lw_count" -gt 3 ]; then
  echo "[SYSTEM GAP] $lw_count lifecycle-workers running (expected <=3) — stale processes"
fi
if echo "$launchd_state" | grep -q "not found"; then
  echo "[SYSTEM GAP] lifecycle-worker not managed by launchd — will die on shell exit"
fi
```

4. **Rate limit budget** — proactive alerting before exhaustion:
```bash
gql_remaining=$(gh api rate_limit --jq '.resources.graphql.remaining' 2>/dev/null || echo 0)
if [ "$gql_remaining" -lt 500 ]; then
  echo "[SYSTEM GAP] GraphQL budget low: $gql_remaining remaining — switch to REST-first mode"
fi
```

### Phase 2: MEASURE — Zero-Touch Rate

Calculate the [agento] zero-touch rate per the SOUL.md convention:

```bash
# Merged PRs in last 24h with [agento] tag analysis
_yesterday=$(python3 -c 'from datetime import datetime, timedelta; print((datetime.utcnow()-timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))' 2>/dev/null || date -v-1d +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || date -d yesterday +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null)
gh api 'repos/jleechanorg/agent-orchestrator/pulls?state=closed&per_page=30&sort=updated&direction=desc' \
  --jq ".[] | select(.merged_at != null and .merged_at > \"$_yesterday\") |
    {number, title: .title[:70], agento: (.title | test(\"^\\\\[agento\\\\]\"))}"
```

For each **non-[agento]** merged PR, determine WHY:
- Missing prefix (tagging gap) → bead if no existing bead
- Operator had to fix code directly → bead for the root cause
- Manual conflict resolution → bead for the conflict pattern
- CR/review required operator action → bead for review automation gap

### Phase 3: DIAGNOSE — Root Cause Analysis

**3a. Run /harness** on each new friction point found. /harness asks:
- Is this a harness gap (tests, CI, hooks, instructions)?
- What systemic fix prevents recurrence?
- 5 Whys on both the technical failure AND why the agent went down that path

**3a-system. Run /harness on system gaps** — for each gap found in Phase 1e:
- Dispatch delivery failures → /harness on why /claw has no delivery verification (or verify Step A11 is working)
- Stale worktree accumulation → /harness on why pruneStaleWorktrees() doesn't clean branches
- Lifecycle-worker drift → /harness on why launchd isn't managing the process
- Rate limit exhaustion → switch all Phase 6 dispatches to REST-first mode for this cycle

**3b. Check existing beads** — don't duplicate:
```bash
br list --open 2>/dev/null | head -30
```

### Phase 4: PLAN — Next Steps

**4a. Run /nextsteps** — situational assessment and roadmap update.

**4b. Prioritize** — rank fixes by impact on zero-touch rate:
- P0: Fixes that unblock multiple stalled PRs
- P1: Fixes that prevent recurring friction patterns
- P2: Nice-to-have improvements

### Phase 5: RECORD — Beads + Roadmap + Push

**5a. Create/update beads** for each new friction point:
```bash
br create --priority 2 --title "..." --body "..." 2>/dev/null
```

**5b. Update roadmap doc** — append findings to `roadmap/evolve-loop-findings.md`:
```markdown
## YYYY-MM-DD HH:MM cycle

### Zero-touch rate: X% (N/M)
### New friction points: [list]
### Fixes dispatched: [list]
### Beads created: [list]
```

**5c. Push to origin main**:
```bash
git add roadmap/evolve-loop-findings.md .beads/issues.jsonl
git commit -m "docs(evolve): cycle YYYY-MM-DD HH:MM — zero-touch X%, N friction points"
git push origin main
```

### Phase 6: FIX — Dispatch Workers

**6a. Use /claw** for each actionable bead:
```bash
# /claw dispatches to ao spawn or manual worktree+claude
/claw "Fix bd-XXX: <description>. After completing, run /learn."
```

**6b. If /claw fails** (GraphQL exhausted, session cap, etc.):
- Fall back to manual worktree + `claude --dangerously-skip-permissions` in tmux
- Or create PR directly if the fix is small (config change, agentRules edit)
- Record the /claw failure in a bead

**6c. REST API fallback** — always prefer REST over GraphQL:
```bash
# Merge via REST
gh api repos/OWNER/REPO/pulls/NUM/merge --method PUT -f merge_method=squash

# Create PR via REST
gh api repos/OWNER/REPO/pulls --method POST -f title="..." -f head="BRANCH" -f base="main" -f body="..."

# Post comment via REST
gh api repos/OWNER/REPO/issues/NUM/comments --method POST -f body="..."
```

### Phase 7: RECAP — Cycle Summary

Output a concise cycle summary:
```
## Evolve Loop Cycle — HH:MM
- Zero-touch rate: X% (trend: ↑/↓/→)
- Workers: N alive, N dead, N stuck
- PRs: N open, N merged since last cycle
- Friction: N new points found
- Fixes: N dispatched via /claw, N direct
- Beads: N created, N updated
- Roadmap: pushed to main
```

Touch the timestamp file for next cycle:
```bash
touch /tmp/evolve_loop_last_run
```

---

## Invocation

```bash
# Start the loop (via /loop skill)
/loop 10m /eloop

# Or manually for one cycle
/eloop
```

The `/loop` wrapper handles the 12-hour max and 10-minute interval. Each `/eloop` invocation runs one complete cycle.

## Anti-Stall Rules

- If GraphQL is exhausted, switch to REST immediately — never sleep-retry
- If session cap is hit (>30), do not spawn — report and defer
- If a worker is stuck (same output 3 consecutive checks), kill and respawn
- If /claw fails twice on the same bead, create PR directly
- If main repo is on wrong branch, fix it silently (git checkout main)
- If build is broken on main, fix it before dispatching workers
- If Phase 1e detects system gaps, run Phase 3 even if Phase 2 shows healthy PR metrics

## Key Files

- `roadmap/evolve-loop-findings.md` — cumulative findings log
- `.beads/issues.jsonl` — bead tracker
- `~/.smartclaw/SOUL.md` — zero-touch convention ([agento] prefix)
- `~/.smartclaw/agent-orchestrator.yaml` — agentRules config
- `novel/` — worker friction narratives
