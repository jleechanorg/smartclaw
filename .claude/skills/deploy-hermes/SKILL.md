---
name: deploy-hermes
description: Hermes deploy pipeline — sync, test, and prevent drift between staging and prod
owner: jleechan
created: 2026-04-21
tags: [hermes, deploy, sync, staging-prod]
---

# Deploy Hermes Skill

## When to invoke

Invoke this skill when:
- Running `bash scripts/deploy.sh` for Hermes
- Editing `scripts/deploy.sh` (any change to `hermes_sync_config()`)
- Adding a new top-level tracked file to `~/.hermes/`
- Investigating drift between `~/.hermes/` (staging) and `~/.hermes_prod/` (prod)
- Fixing a deploy sync gap

## What this skill enforces

### Sync Completeness Rule

**Any top-level file tracked in `git ls-files` must be in exactly one of:**
1. The `hermes_sync_config()` function in `deploy.sh` (synced to prod)
2. The `INTENTIONALLY_SKIPPED` or `INTENTIONALLY_SKIPPED_DIRS` set in `tests/test_deploy_sync_completeness.py` (with a comment explaining why)

**Why this matters**: If a file is tracked in git (so it survives re-clone) but is NOT synced to prod, the prod runtime will run stale code/config indefinitely after a deploy, because the deploy script doesn't touch it.

**The specific failure this prevents**: `agent-orchestrator.yaml` was added to git but not added to `hermes_sync_config()` → prod ran an older version of the AO config while staging had the new one → drift undetected for weeks.

### Policy files loop (line ~411)

`hermes_sync_config()` syncs these individual files via a `for policy_file in ...` loop:

```
SOUL.md AGENTS.md TOOLS.md HEARTBEAT.md prefill.json agent-orchestrator.yaml
```

**When adding a new policy/config file**:
1. Add it to this loop
2. If it should NOT sync (e.g., prod has its own version), add it to `INTENTIONALLY_SKIPPED` in the test
3. If it IS synced, the test `test_policy_files_loop_uses_complete_enumeration()` verifies it appears in the loop

### Config sync with prod-native overrides

`config.yaml` is synced but then patched for known prod-specific overrides:
- `slack.require_mention` → `False` (prod allows bot-free messaging)
- `platforms.api_server.extra.port` → `8642` (prod native port)

**When adding a new prod-native override**: add it to the `OVERRIDES` list in `hermes_sync_config()` — do NOT hardcode it separately.

### skills/ directory

Synced via `rsync --delete` with `__pycache__` and `*.pyc` excluded. The sync test verifies every skill tracked in git appears in the destination.

## Test suite

Run the completeness test before and after any deploy.sh edit:

```bash
python -m pytest tests/test_deploy_sync_completeness.py -v
```

## Gap detection

If the test fails with "Top-level git-tracked files missing from hermes_sync_config()", you must either:
1. Add the missing file/dir to the sync loop in `deploy.sh`, OR
2. Add it to `INTENTIONALLY_SKIPPED` / `INTENTIONALLY_SKIPPED_DIRS` in the test with a comment

Not adding it to EITHER place is a harness regression.
