# Zero-Touch-by-Operator Definition

## What it means

A PR is **zero-touch** when AO (Agent Orchestrator) brought it from creation to merge without any manual intervention from a Claude Code terminal session or human operator.

Specifically:
1. AO spawned the worker session autonomously (via `ao spawn`, lifecycle-worker, or poller)
2. The worker drove the PR to **N-green** (see below) without external nudges
3. `skeptic-cron.yml` (or equivalent auto-merge gate) merged the PR

## N-green criteria

The merge gate requires all **applicable** conditions to pass. Not all PRs need all 7:

| # | Condition | Always required? |
|---|-----------|-----------------|
| 1 | CI passing | Yes |
| 2 | No merge conflicts | Yes |
| 3 | CodeRabbit APPROVED | Yes (unless CR is disabled for the repo) |
| 4 | Bugbot clean | Yes (unless Bugbot is not configured) |
| 5 | Inline comments resolved | Yes |
| 6 | Evidence review pass | Skippable for docs-only, config-only, or chore PRs |
| 7 | Skeptic PASS | Skippable for docs-only, config-only, or chore PRs |

A docs-only change that passes conditions 1-5 but skips 6-7 is still zero-touch if AO handled it autonomously. The key is that **all applicable conditions** were met without manual help.

## What breaks zero-touch

Any of these disqualify a PR from zero-touch status:

- A Claude Code terminal session (human-operated) pushed fixes to the PR branch
- A human or terminal session posted review comments, approvals, or merge clicks on the PR
- A human dismissed a bot review to unblock merge
- A terminal session ran `@coderabbitai approve` or similar to bypass a gate
- A human manually triggered CI re-runs to get past flaky tests

## What does NOT break zero-touch

- Jeffrey asking questions about the PR in Slack (observation, not intervention)
- Bot-to-bot interactions (CR, Bugbot, Skeptic, evidence-review-bot)
- AO workers posting `@coderabbitai approve` or `@coderabbitai all good?` (that's the worker doing its job)
- Jeffrey approving the AO spawn itself (dispatch is expected; execution must be autonomous)

## Measurement

**GitHub actor audit**: A PR is zero-touch if the only GitHub actors on it (commits, reviews, comments, merges) are:
- `${GITHUB_USER}` (AO agent GitHub identity)
- `github-actions[bot]` (CI, skeptic-cron)
- `coderabbitai[bot]` (code review)
- `cursor[bot]` (Bugbot)

If `jleechan` (Jeffrey's personal account) or any other human/terminal identity appears as a PR actor, it's operator-assisted.

**Labeling**: `skeptic-cron.yml` should label merged PRs as `zero-touch` or `operator-assisted` based on this actor audit. (Not yet implemented — tracked in beads.)

## KPI

The **zero-touch rate** for a time window is:

```
zero_touch_rate = (zero-touch merged PRs) / (total merged PRs) * 100
```

Measured weekly. Target: increasing trend. Current baseline: TBD.
