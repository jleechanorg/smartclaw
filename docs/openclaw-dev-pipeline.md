# OpenClaw Development Pipeline

Automated 3-stage pipeline: feature worktrees → Docker staging gateway → production `~/.openclaw/`.

**Principle:** No human should need to touch `~/.openclaw/` directly. All changes flow through the pipeline.

---

## Architecture

```
feature/N worktree
       │ Python integration tests
       │ (no full gateway needed)
       ▼
  PR on jleechanclaw
       │ CodeRabbit review + skeptic-cron merge
       ▼
staging worktree ──git─── ~/.openclaw-staging/ ──mount─── Docker container
       │                                              │
       │          launchd: restart on push            │
       │                                              ▼
       │                              Full gateway integration test
       │                              (health + Slack + memory + cron)
       ▼
  PR on jleechanclaw
       │ Auto-promote if green
       ▼
main worktree ──git─── ~/.openclaw/
       │
       │ launchd: restart on push
       ▼
  ~/.openclaw/ (production gateway)
```

---

## Stage 1 — Feature Worktrees + Integration Tests

### Creating a feature worktree

```bash
# Create a feature branch on origin
git checkout -b feat/my-feature
git push -u origin feat/my-feature

# Attach a worktree (worktrees share the git object store, have isolated working directories)
git worktree add ~/.openclaw-worktrees/feat-my-feature main
```

Now edit files in `~/.openclaw-worktrees/feat-my-feature/`. The `.openclaw/` config, scripts, skills, and tests are all there.

### Running integration tests

```bash
# Unit/fast tests (no gateway needed)
cd ~/.openclaw
python3 -m pytest tests/test_backup_scripts.py -v

# All Python tests
cd ~/.openclaw
python3 -m pytest tests/ -v --tb=short

# Config validation
python3 -c "
import json
with open('~/.openclaw-worktrees/feat-my-feature/openclaw.json') as f:
    c = json.load(f)
    assert 'gateway' in c
    assert 'channels' in c
print('Config valid')
"
```

### When tests pass

```bash
git add . && git commit && git push
# → PR created on GitHub
# → CodeRabbit reviews
# → skeptic-cron merges when green
# → staging worktree updated (see Stage 2 trigger)
```

---

## Stage 2 — Staging Docker Gateway

### Architecture

- **Location:** `~/.openclaw-staging/` — a git worktree on the `staging` branch
- **Gateway:** Docker container (not native Node)
- **Image:** `ghcr.io/openclaw/openclaw:latest`
- **Health:** `curl http://127.0.0.1:18810/health`
- **Logs:** `docker compose -f ~/openclaw-docker-staging/docker-compose.staging.yml logs -f`

### What happens at merge to staging

1. Git push to `staging` branch
2. Launchd watch job detects change
3. `docker compose restart openclaw-gateway`
4. Wait for healthy → run integration tests against Docker gateway
5. On pass: open PR to `main`
6. On fail: post failure to Slack `#jleechanclaw`

### Staging Docker setup

See `openclaw-docker-staging-setup.md`.

### Integration tests (full gateway)

```bash
# 1. Gateway health
curl -s http://127.0.0.1:18810/health
# Expected: {"ok":true,"status":"live"}

# 2. Slack channel reachable
curl -s -H "Authorization: Bearer $STAGING_GATEWAY_TOKEN" \
  http://127.0.0.1:18810/v1/channels/slack/status

# 3. Memory plugin responsive
curl -s -H "Authorization: Bearer $STAGING_GATEWAY_TOKEN" \
  http://127.0.0.1:18810/v1/memory/status

# 4. Python test suite
python3 -m pytest tests/ -v --tb=short
```

### Promotion criteria

All of:
- Gateway health returns `{"ok":true}`
- All Python tests pass
- No new errors in Docker logs

---

## Stage 3 — Production

### Architecture

- **Location:** `~/.openclaw/` — a git worktree on `main`
- **Gateway:** Native Node.js via launchd (`com.openclaw.gateway.plist`)
- **Health:** `curl http://127.0.0.1:18789/health`

### What happens at merge to main

1. Git push to `main` branch
2. Launchd watch job detects change
3. `launchctl restart com.openclaw.gateway` (or restart the service)
4. Verify health
5. Post to Slack `#jleechanclaw` that prod is updated

### Promotion from staging to main

Auto-promotion via PR (created in Stage 2). skeptic-cron merges when 7-green.

---

## Worktree Layout

```
~/.openclaw/                   ← worktree on main (production)
~/.openclaw-staging/           ← worktree on staging (Docker gateway)
~/.openclaw-worktrees/         ← parent dir for ephemeral feature worktrees
  feat-my-feature/             ← one worktree per feature
  feat-another-feature/
```

### Branch protection

| Branch | Who can push | Auto-merge |
|--------|--------------|------------|
| `main` | CI only (via PR) | skeptic-cron |
| `staging` | CI only (via PR from feature) | CI automation |
| `feat/*` | anyone | skeptic-cron after CR approved |

---

## Automation Components

### 1. Staging watch (launchd + git hook)

Trigger: push to `staging` branch.
Action: restart Docker gateway, run integration tests, open PR to `main`.

```bash
# In ~/.openclaw-staging/.git/hooks/post-commit (staging worktree)
#!/bin/bash
# Detects that staging branch was updated
# Triggers: docker compose restart + test + PR to main
~/scripts/staging-promote.sh
```

### 2. Staging promote script (`~/.openclaw/scripts/staging-promote.sh`)

```bash
#!/bin/bash
set -euo pipefail

STAGING_DIR="$HOME/.openclaw-staging"
STAGING_BRANCH="staging"
MAIN_BRANCH="main"
TOKEN_FILE="$STAGING_DIR/.gateway-token"  # not in git

# Restart Docker gateway
docker compose -f ~/openclaw-docker-staging/docker-compose.staging.yml \
  restart openclaw-gateway

# Wait for healthy
for i in $(seq 1 20); do
  HEALTH=$(curl -s http://127.0.0.1:18810/health)
  if echo "$HEALTH" | grep -q '"ok":true'; then
    echo "Gateway healthy"
    break
  fi
  sleep 3
done

# Run integration tests
cd ~/.openclaw
python3 -m pytest tests/ -v --tb=short || {
  echo "Tests failed — not promoting"
  exit 1
}

# Open PR: staging → main
gh pr create \
  --repo jleechanorg/jleechanclaw \
  --base main \
  --head staging \
  --title "chore: promote staging to production" \
  --body "Auto-promote from staging branch after green integration tests.

Tests: all passed
Gateway: healthy at http://127.0.0.1:18810
Automation: staging-promote.sh" \
  || echo "PR may already exist"
```

### 3. Prod watch (launchd + git hook)

Trigger: push to `main` branch.
Action: restart native prod gateway.

```bash
# In ~/.openclaw/.git/hooks/post-commit (prod worktree)
launchctl restart com.openclaw.gateway
```

---

## Key Rules

1. **Never edit `~/.openclaw/` directly.** All changes go through feature worktree → PR → staging → PR → main.
2. **Staging branch = integration gate.** If it doesn't pass in staging, it doesn't go to prod.
3. **PRs are the only promotion mechanism.** No force-push to `main` or `staging`.
4. **Tests must be green before promotion.** Both Python test suite and Docker gateway health.
5. **Docker gateway is staging-only.** Production always uses native Node.js via launchd.

---

## Getting Started

### One-time setup

```bash
# 1. Create staging worktree on staging branch
git worktree add ~/.openclaw-staging origin/staging

# 2. Confirm staging worktree
git -C ~/.openclaw-staging branch  # should show "(detached at ...)"
git -C ~/.openclaw-staging checkout staging

# 3. Install staging Docker gateway
# See openclaw-docker-staging-setup.md

# 4. Create staging branch if it doesn't exist
git checkout -b staging
git push -u origin staging

# 5. Create production worktree if it doesn't exist
git worktree add ~/.openclaw main

# 6. Install git hooks for auto-restart
cat >> ~/.openclaw/.git/hooks/post-commit << 'EOF'
#!/bin/bash
launchctl restart com.openclaw.gateway
EOF
chmod +x ~/.openclaw/.git/hooks/post-commit
```

### Daily dev workflow

```bash
# 1. Create feature worktree
git worktree add ~/.openclaw-worktrees/feat-my-feature main

# 2. Edit files in the worktree
cd ~/.openclaw-worktrees/feat-my-feature
$EDITOR openclaw.json  # or any file

# 3. Run tests
python3 -m pytest ~/tests/ -v

# 4. Commit + push → PR auto-created
git add . && git commit -m "feat: ..."
git push -u origin feat-my-feature

# 5. After PR merged to staging:
#    - Docker staging gateway auto-restarts
#    - Tests run
#    - PR to main opens
# 6. After PR merged to main:
#    - Native gateway auto-restarts
```

---

## Verification Commands

```bash
# Check which worktrees exist
git worktree list

# Check current branch in any worktree
git -C ~/.openclaw-staging branch

# Check staging Docker gateway
curl http://127.0.0.1:18810/health
docker compose -f ~/openclaw-docker-staging/docker-compose.staging.yml ps

# Check prod gateway
curl http://127.0.0.1:18789/health
launchctl list | grep openclaw

# Check all Python tests
cd ~/.openclaw && python3 -m pytest tests/ --tb=short
```
