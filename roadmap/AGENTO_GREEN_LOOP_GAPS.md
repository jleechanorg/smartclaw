# Agento Green Loop Gaps

**Problem:** Agents declare a PR "done" before it is truly green, requiring human intervention to catch post-APPROVE blockers and restart work.

## Root Causes

### Gap 1 — Agent exits after APPROVE without checking post-APPROVE inlines (`orch-5fhp`)

**What happened:** jc-82 fixed CR's REQUEST_CHANGES, pushed, received APPROVE, and stopped. CR then posted a Critical inline comment 3 minutes later. The agent never saw it.

**Why:** The agent task loop is: push → `@coderabbitai all good?` → wait for verdict → if APPROVE, declare done. It does not loop back to check inline comments after APPROVE.

**Fix options (config → plugin → core):**
1. **Config (done):** Add rule to `agentRules`: after APPROVE, run `gh api repos/{owner}/{repo}/pulls/{pull_number}/comments` and check for Major/Critical inlines posted after the APPROVE timestamp. If any exist, treat as REQUEST_CHANGES and continue.
2. **agentRules enhancement:** Encode the exact shell command and timestamp comparison so the agent can self-check without human prompting.
3. **AO reaction (new):** Add a `post-approval-inline-check` reaction that polls inline comments after an APPROVE event and re-sends the agent if blockers exist.

---

### Gap 2 — `approved-and-green` reaction is `auto: false` (`orch-sgks`)

**What happened:** AO detected the APPROVE state but only sent a notification — it did not re-trigger the agent to verify the full green checklist.

**Current config:**
```yaml
approved-and-green:
  auto: false
  action: notify
  priority: action
```

**Fix:** Change to `auto: true` with `action: send-to-agent` and a message that instructs the agent to verify all 4 green conditions (including post-APPROVE inline check) before stopping.

```yaml
approved-and-green:
  auto: true
  action: send-to-agent
  message: |
    CodeRabbit has approved. Before declaring done, verify ALL 4 green conditions:
    1. CI: gh pr view <PR> --json statusCheckRollup --jq '[.statusCheckRollup[]|select(.conclusion=="FAILURE")]'
    2. Mergeable: gh pr view <PR> --json mergeable --jq '.mergeable'
    3. Post-APPROVE inline blockers: gh api repos/<OWNER>/<REPO>/pulls/<PR>/comments \
         --jq '[.[] | select(.user.login=="coderabbitai[bot]") | select(.created_at > "<APPROVE_TS>") | select(.body | test("Major|Critical"))] | length'
    4. No other unresolved human/bot comments.
    If all 4 pass, post a PR comment summarizing: "PR is green — CI pass, MERGEABLE, no unresolved comments, CR APPROVE."
    If any fail, fix and push again.
```

---

### Gap 3 — `agent-stuck` is too blunt for "done-but-not-green" (`orch-szd5`)

**What happened:** jc-82 was idle (not stuck) after declaring done. `agent-stuck` fires after 10 min of inactivity, but the message it sends is generic ("you appear to be stuck"). It doesn't tell the agent to re-verify green conditions.

**Fix:** Update the `agent-stuck` message to include the full green verification checklist, so any idle agent automatically re-checks before truly stopping.

```yaml
agent-stuck:
  auto: true
  action: send-to-agent
  threshold: 10m
  message: |
    You appear to be idle. Before stopping, verify your PR is truly green:
    1. Run: gh pr view <PR_NUMBER> --repo <REPO> --json mergeable,statusCheckRollup
    2. Check for post-APPROVE CR inline blockers: gh api repos/<REPO>/pulls/<PR_NUMBER>/comments \
         --jq '[.[] | select(.user.login=="coderabbitai[bot]") | select(.body | test("Major|Critical"))] | length'
    3. If any issues found, fix and push. If truly done, post a PR comment: "PR is green."
  escalateAfter: 20m
```

---

## Recommended Implementation Order

| Priority | Gap | Effort | Impact |
|---|---|---|---|
| 1 | `approved-and-green: auto: true` with full green checklist message | Config only | High — closes the loop autonomously |
| 2 | Update `agent-stuck` message with green verification steps | Config only | Medium — catches idle agents that declared done prematurely |
| 3 | Post-APPROVE inline check in `agentRules` (already done) | Config (done) | Medium — agents self-check when they remember |
| 4 | New `post-approval-inline-check` AO reaction | New plugin | High — deterministic, not LLM-dependent |

Items 1 and 2 are pure config changes to `~/agent-orchestrator.yaml` — no code required.

---

### Gap 4 — Review threads not auto-resolved after fix push (`orch-1roe`)

**What happens:** Agent pushes a fix commit addressing a review comment. The thread on GitHub stays unresolved. `mergeStateStatus` remains `UNSTABLE`. The merge gate never opens.

**Why:** Nothing in the fixpr/copilot/agento loop calls the `resolveReviewThread` GraphQL mutation after a fix lands. Resolving is a separate manual click.

**Fix:** After a fix is pushed, the agent should:
1. Match which threads were addressed by the commit (by path/line or comment body match)
2. Call `resolveReviewThread(input: {threadId: "PRRT_..."})` via `gh api graphql`
3. Re-check `mergeStateStatus` — if now `CLEAN`, proceed to merge gate

```bash
gh api graphql -f query='
mutation($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) {
    thread { isResolved }
  }
}' -f threadId="PRRT_kwDO..."
```

---

### Gap 5 — No approval path — `reviewDecision` always empty (`orch-uq4z`)

**What happens:** Even when CI passes, threads are resolved, and PR is `MERGEABLE`, `reviewDecision` is empty. The auto-merge gate (`orch-xa12`) requires an approval to proceed.

**Why:** No CODEOWNERS rule or bot is configured to approve PRs automatically for bot/agent-only changes.

**Fix options:**
1. **CODEOWNERS auto-approval:** Configure a GitHub App or bot account in CODEOWNERS with auto-approve for files only agents touch (e.g., `scripts/`, `.claude/skills/`)
2. **Agent self-review workaround:** Use `gh pr review --approve` from a secondary GitHub account designated as the "bot reviewer" — triggered when all other 3 green conditions pass
3. **Branch protection relaxation:** Remove required review for branches matching `fix/*` pattern — acceptable if CI is required

---

## Bugs Fixed (2026-03-16)

| Bead | Bug | Status |
|------|-----|--------|
| `orch-xqke` | `ao-backfill.sh`: `sed -i` fallback never ran — `pokedAt` not written on first poke | ✅ Fixed (commit 5bba16d90) |
| `orch-62m0` | `agento-report`: `cr_approved == "1"` fails for count > 1 | ✅ Fixed (commit 5bba16d90) |
| `orch-06xo` | `agento-report`: `unstable` mislabeled as merge conflict | ✅ Fixed (commit 5bba16d90) |

---

## Beads

- `orch-5fhp` — agent declares done without verifying post-APPROVE inline comments
- `orch-sgks` — approved-and-green reaction is auto:false
- `orch-szd5` — agent-stuck threshold too blunt for done-but-not-green sessions
- `orch-1roe` — review threads not auto-resolved after fix push (**new Gap 4**)
- `orch-uq4z` — no approval path — reviewDecision always empty (**new Gap 5**)
