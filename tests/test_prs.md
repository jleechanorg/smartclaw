# Test PRs for Self-Critique + Verification Loop Experiments

Curated set of 10 PRs from smartclaw history for testing coding technique improvements. Selected to cover diverse PR types, difficulty levels, and failure modes.

## Small Fixes (1-5 files, <100 lines)

### TEST-PR-001: c84398bbb — Staging Slack Require-Mention Fix
- **Commit**: `c84398bbb`
- **Type**: small fix, configuration hardening
- **Files**: 2 (install-launchagents.sh, test_openclaw_configs.py)
- **Lines**: +73, -14
- **Description**: Add `@mention` requirement on all Slack channels in staging. Prevents staging config from bleeding through to prod on reinstall.
- **Test scenario**: Generate the install-launchagents.sh change and corresponding test.
- **Why hard**: Subtle config interaction between staging and prod layers; test must assert live staging config behavior.
- **Category**: Config edge case

### TEST-PR-002: dba9523df — Doctor Staging Launchd State Detection
- **Commit**: `dba9523df`
- **Type**: tiny fix, 1 file
- **Files**: 1 (scripts/doctor.sh)
- **Lines**: +17
- **Description**: Detect "enabled-but-not-bootstrapped" broken state in staging launchd.
- **Test scenario**: Generate the doctor.sh addition that detects this state.
- **Why hard**: Narrow edge case that only manifests in a specific deployment state.
- **Category**: Error detection / diagnosis

### TEST-PR-003: 893c65469 — Skeptic Dispatch and Beads Prefix Fix
- **Commit**: `893c65469`
- **Type**: small-medium fix, orchestration
- **Files**: 3 (bootstrap.sh, webhook.py, skeptic-gate.yml)
- **Lines**: +74, -5
- **Description**: Separate skeptic trigger from VERDICT to prevent bypass. Fix beads prefix mismatch.
- **Test scenario**: Generate the webhook.py changes and the CI workflow change.
- **Why hard**: Two concerns interact (dispatch timing + data prefix); correct fix requires understanding the full orchestration flow.
- **Category**: Concurrency / orchestration logic

## Medium Fixes (4-8 files, 100-500 lines)

### TEST-PR-004: 4134d0882 — Bug-Hunt AO One-Shot Fix
- **Commit**: `4134d0882`
- **Type**: medium fix, script repair
- **Files**: 4 (.beads/issues.jsonl, bug-hunt-daily.sh, thread-reply-nudge.sh, test_bug_hunt_daily_script.py)
- **Lines**: +293, -47
- **Description**: Replace invalid `ao --task` usage in bug-hunt script with openclaw agent one-shot. Align bug-hunt prompt with JSON extraction format.
- **Test scenario**: Given the bug-hunt-daily.sh script as context, generate the corrected version.
- **Why hard**: The original script had a valid-looking but semantically wrong `ao` invocation pattern. Fix required understanding the difference between fire-and-forget and one-shot AO modes.
- **Category**: Script repair / API usage correction
- **Post-merge note**: This was a post-merge hotfix — the bug was merged before being caught.

### TEST-PR-005: 6a93527c4 — AO Browserclaw Repo Mapping
- **Commit**: `6a93527c4`
- **Type**: medium feature, AO integration
- **Files**: 4 (.github/workflows/skeptic-gate.yml, lib/github-intake-lib.sh, scripts/ao-progress-reporter.sh, src/tests/test_github_intake.py)
- **Lines**: +39, -14
- **Description**: Register browserclaw AO repo mapping. Wire browserclaw into AO repo mapping. Harden skeptic gate output quoting.
- **Test scenario**: Generate the YAML workflow change and the shell library addition.
- **Why hard**: Required understanding of AO dispatch routing and repo mapping tables.
- **Category**: Integration / configuration wiring

### TEST-PR-006: 897372987 — AO Dashboard Opt-In and Health Probes
- **Commit**: `897372987`
- **Type**: medium fix, harness hardening
- **Files**: 8 (monitor-agent.sh, install-launchagents.sh, test_openclaw_configs.py, workspace/MEMORY.md, etc.)
- **Lines**: +108, -12
- **Description**: Add opt-in AO dashboard. Harden gateway health probes. Fix launchctl enable after dashboard opt-in install.
- **Test scenario**: Generate the monitor-agent.sh and install-launchagents.sh changes.
- **Why hard**: Two separate concerns (dashboard opt-in + health probe bounds) landed in the same PR.
- **Category**: Harness hardening / multi-concern PR

### TEST-PR-007: 65d76a073 — Deploy DM E2E Disable + Upgrade-Safe Fix
- **Commit**: `65d76a073`
- **Type**: medium fix, deploy pipeline
- **Files**: 4 (deploy.sh, openclaw-upgrade-safe.sh, and config files)
- **Lines**: +9, -43 (net negative — removed code)
- **Description**: Disable DM e2e in staging+prod monitor. Fix upgrade-safe to use launchd label. Fix mem0 log message.
- **Test scenario**: Generate the openclaw-upgrade-safe.sh changes (remove bad code, add correct launchd label usage).
- **Why hard**: Required understanding the launchd lifecycle and upgrade safety invariants. Net negative diff is a clue — the original approach was wrong.
- **Category**: Bug repair / infrastructure

## Complex (10+ files, 500+ lines)

### TEST-PR-008: 5881a2505 — Slack E2E Matrix + Skeptic Webhook
- **Commit**: `5881a2505`
- **Type**: complex, multi-component
- **Files**: 11
- **Lines**: +389, -34
- **Description**: Add skeptic webhook trigger and coverage diagnostics. Fix Slack E2E matrix harness. Repair staging config overlay.
- **Test scenario**: Given the prior Slack matrix test, generate the staging overlay fix + skeptic webhook integration.
- **Why hard**: Multiple concurrent issues: staging config bleeding, skeptic dispatch timing, test harness flakiness. Required understanding the full monitor stack.
- **Category**: Multi-component integration / test harness
- **Post-merge note**: Multiple subsequent fixes to this area in the weeks after merge.

### TEST-PR-009: b22747b9c — Monitor Slack Matrix + Staging Overlay
- **Commit**: `b22747b9c`
- **Type**: very complex, test infrastructure
- **Files**: 6 (tests/test_monitor_slack_e2e_matrix.sh, tests/test_openclaw_configs.py, install-launchagents.sh, and more)
- **Lines**: +1402, -156
- **Description**: Major rewrite/expansion of Slack E2E matrix test harness. Fix staging overlay harness behavior.
- **Test scenario**: Generate the expanded test_monitor_slack_e2e_matrix.sh and the corresponding test_openclaw_configs.py additions.
- **Why hard**: 1400+ line test file. The tests themselves are the specification — generating correct test logic for this complex monitoring scenario is non-trivial.
- **Category**: Test generation / large-scale harness work

### TEST-PR-010: 74aaec65f — SOUL.md + AO Workspace + Docs
- **Commit**: `74aaec65f`
- **Type**: complex, multi-faceted
- **Files**: 15
- **Lines**: +429, -53
- **Description**: Commit pending docs (gateway-reference.md, pr-merge-protocols.md), staging lifecycle helpers, upgrade safety improvements. Full AO workspace integration.
- **Test scenario**: Generate docs/gateway-reference.md given the operational context, or generate scripts/openclaw-staging-start.sh and stop.sh lifecycle helpers.
- **Why hard**: Required understanding the full OpenClaw operational model, gateway protocols, and lifecycle state machine. Documentation must be precise enough to serve as a runbook.
- **Category**: Documentation + operational scripts / domain knowledge heavy

## How to Use These PRs

### Baseline Run (no technique)
For each PR:
1. Provide the PR description and context
2. Ask the model to generate the fix/solution
3. Measure: did it match the actual diff? (exact match, partial, or wrong direction)

### Technique Run (with self-critique loop)
Same as above, but run through the 4-phase self-critique + verification loop:
1. Generate with step-by-step thinking
2. Generate test cases
3. Self-critique against 5 evidence standards
4. Revise and re-verify

### Measurement
```
PR: TEST-PR-XXX
Technique: <name>
Baseline: <match/no-match/partial>
With-Technique: <match/no-match/partial>
Delta: <improvement/regression>
Token overhead: <tokens used>
Issues caught by self-critique: <list>
```
