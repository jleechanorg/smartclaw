# PR & Merge Protocols — Full Reference

- **NEVER make PRs to openclaw/openclaw** — this is a personal backup repo
- **NEVER merge without explicit user approval**
- **ALL CI checks must pass before merge** — `mergeable: "MERGEABLE"` only means no conflicts
- Always check `statusCheckRollup` for failures before declaring PR ready

## 7-Point Green Criteria (ALL must hold before merge — matches skeptic-cron.yml)

| # | Condition | How to check |
|---|-----------|-------------|
| 1 | CI green — all GH Actions checks pass | `gh api repos/.../commits/SHA/check-runs --jq '.check_runs[] \| "\(.name): \(.conclusion)"'` |
| 2 | No conflicts — mergeable=true | `gh api repos/.../pulls/NUM --jq '{mergeable, mergeable_state}'` |
| 3 | CR APPROVED — coderabbitai[bot] latest review is APPROVED | `gh api .../pulls/NUM/reviews --jq '[.[] \| select(.user.login=="coderabbitai[bot]")] \| last \| .state'` |
| 4 | Bugbot clean — cursor[bot] zero error-severity comments | `gh api .../pulls/NUM/comments --jq '[.[] \| select(.user.login=="cursor[bot]" and (.body \| test("error";"i")))] \| length'` |
| 5 | Comments resolved — zero unresolved non-nit inline review comments | `gh api .../pulls/NUM/comments --jq '[.[] \| select(.in_reply_to_id==null) \| select(.body \| test("^(nit:\|nitpick)";"i") \| not)] \| length'` |
| 6 | Evidence pass — evidence-review-bot APPROVED or evidence-gate CI passed | `gh api .../pulls/NUM/reviews --jq '[.[] \| select(.user.login=="evidence-review-bot" and .state=="APPROVED")] \| length'` |
| 7 | Skeptic PASS — github-actions[bot] posted VERDICT: PASS | `gh api .../issues/NUM/comments --jq '[.[] \| select(.user.login=="github-actions[bot]" and (.body \| test("VERDICT: PASS";"i")))] \| length'` |

**Evidence PASS (criterion 6) requires ALL of:**
- Agent self-review (you read the diff and confirm no obvious issues)
- CodeRabbit review (must post APPROVED — `.coderabbit.yaml` has `approve: true`; COMMENTED alone does not satisfy condition 3)
- Independent code review via `/er` or codex — at least one of CR OR codex must PASS

## Evidence Media Requirements (Repo Scope)

For evidence-bearing PRs:
- Non-trivial changes require a **tmux/terminal video**.
- **User-facing changes require a browser/UI video** (mandatory; no small-change exception).
- **Both videos must include captions** (burned-in preferred; `.vtt`/`.srt` acceptable when linked).
- Upload videos directly in PR description/comments using GitHub native attachments (`https://github.com/user-attachments/assets/...`).

**Policy-definition and documentation-only PRs are exempt from these media requirements until the policy is in effect.**

PR description must include:
- Test output summary (pass/fail counts + key checks)
- Video attachment links
- Caption references for each video
- A self-contained **secret/unlisted** gist URL with sanitized logs/artifacts; **never** include tokens, credentials, API keys, cookies, secrets, or other sensitive content in the gist, even if "sanitized"

Machine-specific absolute paths must be redacted from published output (`/Users/<name>/...`, host-specific temp roots, etc.).

**NEVER run `gh pr merge` yourself — skeptic-cron.yml (every 30 min) merges when all 7 criteria are met.**

## PR Green Loop — Stabilize-Then-Verify (MANDATORY)

When driving a PR to green, follow this policy strictly. **Never push during active bot review.**

1. **Collect** — Read ALL bot comments/findings (CR, Bugbot, Copilot) before making any changes
2. **Batch** — Fix everything in one commit, not one fix per finding
3. **Push once** — Single push with all fixes
4. **Freeze** — Do NOT push again until ALL bots have finished:
   - Bugbot: `status: COMPLETED` (not IN_PROGRESS)
   - CR: new review posted (check `.submitted_at` is after your push)
   - Copilot: check completed
5. **Reconcile** — Only after all bots settle, evaluate all 7 conditions in one pass
6. **If more fixes needed** — go back to step 1. Never fix-push-fix-push reactively.

**Anti-patterns (BANNED):**
- Pushing while Bugbot/CR is still running
- Triggering `@coderabbitai all good?` before all checks are settled
- Resolving review threads to trigger auto-approve (empty-body APPROVED is not real)
- Polling CI status in a loop — wait for bots to finish, then check once
- Sleep-polling after a push — after pushing, EXIT the task; let the monitoring loop handle CI waits. If a bash timeout occurs mid-sleep, do NOT retry — exit immediately.

## CodeRabbit Review Protocol
After pushing to remote, post exactly: `@coderabbitai all good?`

## Commit Guidelines

- Use `git add <specific files>` — never `git add -A` blindly
- Concise, action-oriented messages (e.g., `fix: remove redundant try/except in heartbeat poller`)
- Group related changes; don't bundle unrelated refactors
