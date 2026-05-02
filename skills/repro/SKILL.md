---
name: repro
description: Run canonical Firestore-direct repro for worldarchitect.ai bugs (level-up, rewards, stale flags). Delegates to repro-twin-clone-evidence canonical skill.
triggers:
  - "/repro"
  - "run repro"
  - "repro the bug"
  - "reproduce the issue"
---

# Hermes `/repro` skill

## What this does

Runs a Firestore-direct repro for worldarchitect.ai bugs — **no browser, no game turns, no turn-limit ceiling**.

Reads game state directly from Firestore via the canonical `repro-twin-clone-evidence` skill in the worldarchitect worktree.

## Canonical skill

Full documentation lives in:
```
~/.worktrees/worldarchitect/wa-6429/.claude/skills/repro-twin-clone-evidence/SKILL.md
```

This Hermes skill is a thin pointer + runtime guide. Always read the canonical skill for detailed procedure.

## Quick start

### 1. Find the right worktree

```bash
# Preferred: the canonical evidence worktree
REPRO_ROOT="$HOME/.worktrees/worldarchitect/wa-6429"

# Fallback: any worldarchitect worktree
REPRO_ROOT=$(find ~/.worktrees/worldarchitect -maxdepth 2 -name ".git" -type d 2>/dev/null | head -1 | sed 's|/.git||')
```

### 2. Set required env

```bash
export WORLDAI_DEV_MODE=true
export TESTING_AUTH_BYPASS=true
export ALLOW_TEST_AUTH_BYPASS=true
export MCP_TEST_MODE=real
export MOCK_SERVICES_MODE=false
export PYTHONPATH="$REPRO_ROOT:$REPRO_ROOT/mvp_site"
export GOOGLE_APPLICATION_CREDENTIALS="$HOME/serviceAccountKey.json"
export MCP_TEST_PARALLEL_USER_ID=0wf6sCREyLcgynidU5LjyZEfm7D2
```

### 3. Run the five-class suite

```bash
cd "$REPRO_ROOT"
# Use system python3 — wa-6429 has no venv/bin/python
LEVEL_UP_REPRO_PYTHON=python3 ./scripts/run_level_up_class_repro_suite.sh 2>&1 | tee /tmp/worldarchitect.ai/level_up_5_class_repro.log
```

### 4. Or run a single class

```bash
cd "$REPRO_ROOT"
# Set env first, then run directly with python3
export WORLDAI_DEV_MODE=true
export MCP_TEST_MODE=real
export MOCK_SERVICES_MODE=false
export PYTHONPATH="$REPRO_ROOT:$REPRO_ROOT/mvp_site"
export GOOGLE_APPLICATION_CREDENTIALS="$HOME/serviceAccountKey.json"
export MCP_TEST_PARALLEL_USER_ID=0wf6sCREyLcgynidU5LjyZEfm7D2

python3 testing_mcp/test_level_up_class_2_story_entry_projection_strip.py
# Individual class scripts accept --campaign-id via LEVEL_UP_SOURCE_CLASS2_ID env var:
#   LEVEL_UP_SOURCE_CLASS2_ID=<CAMPAIGN_ID> python3 testing_mcp/...
```

## Common tasks

### Repro a specific campaign ID (e.g. the "visage of the siren" bug)

```bash
# Clone it to jleechantest
cd "$REPRO_ROOT"
export $(grep -E '^(WORLDAI_DEV_MODE|MCP_TEST_MODE|MOCK_SERVICES_MODE|PYTHONPATH|GOOGLE_APPLICATION_CREDENTIALS|MCP_TEST_PARALLEL_USER_ID)=' << 'EOF'
WORLDAI_DEV_MODE=true
MCP_TEST_MODE=real
MOCK_SERVICES_MODE=false
PYTHONPATH=${HOME}/.worktrees/worldarchitect/wa-6429:${HOME}/.worktrees/worldarchitect/wa-6429/mvp_site
GOOGLE_APPLICATION_CREDENTIALS=$HOME/serviceAccountKey.json
MCP_TEST_PARALLEL_USER_ID=0wf6sCREyLcgynidU5LjyZEfm7D2
EOF

# Copy campaign
./scripts/copy_campaign.py \
  --find-by-id <SOURCE_CAMPAIGN_ID> \
  --dest-user-id 0wf6sCREyLcgynidU5LjyZEfm7D2 \
  --suffix " (repro-test)"

# Then run the class-2 harness on the cloned campaign
```

### Twin clone (baseline + test subject)

```bash
# Baseline (read-only, never replayed)
./scripts/copy_campaign.py \
  --find-by-id <CAMPAIGN_ID> \
  --dest-user-id 0wf6sCREyLcgynidU5LjyZEfm7D2 \
  --suffix " (repro-baseline)"

# Test subject (receives process_action, /repro, etc.)
./scripts/copy_campaign.py \
  --find-by-id <CAMPAIGN_ID> \
  --dest-user-id 0wf6sCREyLcgynidU5LjyZEfm7D2 \
  --suffix " (repro-test-subject)"
```

## Reading results

- **`REPRODUCED`** in errors → bug confirmed
- **`INCONCLUSIVE`** → contract not exercised, rerun or adjust
- **`Passed`** (harness) → no bad state detected at that step
- Exit code 1 from a class script is **expected** when collecting RED evidence (not a crash)

Open `scenario_results_checkpoint.json` in the evidence bundle for the full errors list.

## Key env variables summary

| Variable | Value | Purpose |
|----------|-------|---------|
| `WORLDAI_DEV_MODE` | `true` | Required for `testing_mcp` import |
| `MCP_TEST_MODE` | `real` | No mocks |
| `MOCK_SERVICES_MODE` | `false` | No mocks |
| `MCP_TEST_PARALLEL_USER_ID` | `0wf6sCREyLcgynidU5LjyZEfm7D2` | jleechantest clones |
| `GOOGLE_APPLICATION_CREDENTIALS` | `~/serviceAccountKey.json` | Firestore access |

## Pitfalls

- **Without `WORLDAI_DEV_MODE=true`**: `python -c "import testing_mcp"` fails on clock-skew validation
- **Without `MCP_TEST_MODE=real`**: harness uses mocks, not real Firestore — not a valid repro
- **Turn limit errors in game UI** → you're using the wrong path; use this skill instead
- **Synthetic user IDs** (when `MCP_TEST_PARALLEL_USER_ID` unset) are fine for smoke but not valid evidence
