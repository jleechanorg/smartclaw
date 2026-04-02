# OpenClaw Development Pipeline

Automated 3-stage pipeline: feature worktrees → Docker staging gateway → production `~/.openclaw/`.

**Principle:** No human should need to touch `~/.openclaw/` directly. All changes flow through the pipeline.

---

## Architecture

```text
feature/N worktree
       │ Python integration tests
       │ (no full gateway needed)
       ▼
  PR on smartclaw
       │ CodeRabbit review + skeptic-cron merge
       ▼
staging worktree ──git─── ~/.openclaw-staging/ ──mount─── Docker container
       │                                              │
       │          launchd: restart on push            │
       │                                              ▼
       │                              Full gateway integration test
       │                              (health + Slack + memory + cron)
       ▼
  PR on smartclaw
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

# Attach a worktree checked out on that branch (worktrees share the git object store)
git worktree add ~/.openclaw-worktrees/feat-my-feature feat/my-feature
```

Now edit files in `~/.openclaw-worktrees/feat-my-feature/`. The `.openclaw/` config, scripts, skills, and tests are all there.

### Running integration tests

Run pytest from the **feature worktree root** so you validate the same tree you edit (not a different checkout):

```bash
cd ~/.openclaw-worktrees/feat-my-feature

# Unit/fast tests (no gateway needed)
python3 -m pytest tests/test_backup_scripts.py -v

# All Python tests
python3 -m pytest tests/ -v --tb=short

# Config validation
python3 -c "
import json
import os
# os.path.expanduser is required: Python does not expand ~ in string literals
path = os.path.expanduser('~/.openclaw-worktrees/feat-my-feature/openclaw.json')
with open(path) as f:
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

See [`openclaw-docker-staging-setup.md`](openclaw-docker-staging-setup.md) for Docker Compose staging; [`openclaw-staging-setup.md`](openclaw-staging-setup.md) covers launchd + related staging setup in this repo.

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

# 4. Python test suite (from staging worktree)
cd ~/.openclaw-staging
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
- **Gateway:** Native Node.js via launchd (`ai.openclaw.gateway.plist` → `~/Library/LaunchAgents/ai.openclaw.gateway.plist`)
- **Health:** `curl http://127.0.0.1:18789/health`

### What happens at merge to main

1. Git push to `main` branch
2. Launchd watch job detects change
3. `launchctl kickstart -k "gui/$(id -u)/ai.openclaw.gateway"` (restart the gateway service)
4. Verify health
5. Post to Slack `#jleechanclaw` that prod is updated

### Promotion from staging to main

Auto-promotion via PR (created in Stage 2). skeptic-cron merges when 7-green.

---

## Worktree Layout

```text
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

**Intended effect:** after the `staging` branch updates, restart the Docker gateway, run tests, and open a PR to `main`.

**How this is usually wired:**

- **CI / server-side (push-triggered):** a workflow or post-receive hook on `staging` runs the same steps as `~/.openclaw/scripts/staging-promote.sh` when a push lands. That matches the “push to `staging`” wording above.
- **Local-only (commit-triggered):** a `post-commit` hook runs `~/.openclaw/scripts/staging-promote.sh` for commits in **`~/.openclaw-staging/`**, and restarts the prod gateway for commits in **`~/.openclaw/`** (see shared hook below). That fires on **local commit**, not on `git push`; use it only if you accept that timing, or mirror the script in CI for true push automation.
- **Fast-forward pulls:** `post-commit` does not run when `git pull --ff-only` moves `HEAD` without creating a local commit. After installing the shared hook below, copy it to `post-merge` (and optionally `post-checkout`) so the same path-based logic runs when the staging/prod worktree updates via pull.

Hooks must be installed in the **shared Git hooks directory**, not by appending to `~/.openclaw/.git/hooks/...` (in a linked worktree, `.git` is often a *file* pointing at the real metadata). Use `git rev-parse --git-common-dir`, or the concrete path `git -C ~/.openclaw rev-parse --git-path hooks/post-commit`, or set `core.hooksPath` (Git 2.9+). Use **one** hook body so you do not overwrite the same file twice:

```bash
GIT_COMMON="$(git -C "$HOME/.openclaw" rev-parse --git-common-dir)"
HOOKS="$GIT_COMMON/hooks"
cat > "$HOOKS/post-commit" << 'EOF'
#!/bin/bash
set -euo pipefail
# Path-based dispatch works with detached HEAD (common when main is checked out elsewhere).
root=$(git rev-parse --show-toplevel)
case "$root" in
  "$HOME/.openclaw-staging")
    "$HOME/.openclaw/scripts/staging-promote.sh"
    ;;
  "$HOME/.openclaw")
    launchctl kickstart -k "gui/$(id -u)/ai.openclaw.gateway"
    ;;
esac
EOF
chmod +x "$HOOKS/post-commit"

# Optional: same dispatcher for fast-forward pulls (no local commit)
cp "$HOOKS/post-commit" "$HOOKS/post-merge"
chmod +x "$HOOKS/post-merge"
```

### 2. Staging promote script (`~/.openclaw/scripts/staging-promote.sh`)

```bash
#!/bin/bash
set -euo pipefail

REPO_SLUG="jleechanorg/smartclaw"
STAGING_DIR="$HOME/.openclaw-staging"
# Secrets: keep `.gateway-token` outside git — add to ~/.openclaw-staging/.git/info/exclude
# or rely on repo `.gitignore` pattern `.gateway-token` so `git add .` never commits it.
TOKEN_FILE="$STAGING_DIR/.gateway-token"
if [[ -f "$TOKEN_FILE" ]]; then
  export STAGING_GATEWAY_TOKEN="$(cat "$TOKEN_FILE")"
fi

# Restart Docker gateway
docker compose -f ~/openclaw-docker-staging/docker-compose.staging.yml \
  restart openclaw-gateway

# Wait for healthy (fail fast if never healthy)
# Note: curl may fail with connection refused while the gateway starts; `|| true` keeps `set -e` from aborting the retry loop.
GATEWAY_HEALTHY=false
for i in $(seq 1 20); do
  HEALTH=$(curl -sS --max-time 5 --connect-timeout 2 "http://127.0.0.1:18810/health" 2>/dev/null || true)
  if echo "$HEALTH" | grep -q '"ok":true'; then
    echo "Gateway healthy"
    GATEWAY_HEALTHY=true
    break
  fi
  sleep 3
done
if [[ "$GATEWAY_HEALTHY" != true ]]; then
  echo "Gateway health check timed out — not promoting"
  exit 1
fi

# Run integration tests from staging worktree (same checkout as Docker mount)
cd "$STAGING_DIR"
python3 -m pytest tests/ -v --tb=short || {
  echo "Tests failed — not promoting"
  exit 1
}

# Open PR: staging → main (do not blanket-ignore failures — only treat "already open" as OK)
if ! gh pr create \
  --repo "$REPO_SLUG" \
  --base main \
  --head staging \
  --title "chore: promote staging to production" \
  --body "Auto-promote from staging branch after green integration tests.

Tests: all passed
Gateway: healthy at http://127.0.0.1:18810
Automation: ~/.openclaw/scripts/staging-promote.sh"; then
  if [[ $(gh pr list --repo "$REPO_SLUG" --head staging --base main --json number --jq 'length') -ge 1 ]]; then
    echo "Promotion PR already open — skipping create"
  else
    echo "gh pr create failed and no staging→main PR exists — aborting" >&2
    exit 1
  fi
fi
```

### 3. Production gateway restart (launchd + same hook)

**Intended effect:** after `main` updates, restart the native prod gateway.

**Push-triggered automation** should run `launchctl kickstart … ai.openclaw.gateway` on the server or via CI when `main` moves. For **local post-commit**, the shared hook above runs when `git rev-parse --show-toplevel` is `~/.openclaw` (the production worktree path).

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
# 1. Ensure remote branch staging exists (required before adding that worktree)
git fetch origin
if ! git show-ref --verify --quiet refs/remotes/origin/staging; then
  # Always branch staging from main — avoid creating staging from whatever HEAD was checked out
  git checkout -B staging origin/main
  git push -u origin staging
  git checkout -
fi

# 2. Create staging worktree
git worktree add ~/.openclaw-staging origin/staging

# 3. Confirm staging worktree
git -C ~/.openclaw-staging branch  # may show "(detached at ...)" first
git -C ~/.openclaw-staging checkout staging

# 4. Install staging Docker gateway
# See openclaw-docker-staging-setup.md (and openclaw-staging-setup.md for launchd)

# 5. Production worktree — Git allows only one checkout of branch `main` at a time.
#    If `git worktree add ~/.openclaw main` fails because `main` is checked out elsewhere,
#    free it in the other clone/worktree first, or use a detached tree at main's tip:
git fetch origin
git worktree add ~/.openclaw origin/main
git -C ~/.openclaw checkout --detach
# When you can attach `main` here instead (no other worktree holds main):
# git -C ~/.openclaw switch -C main

# 6. Install shared post-commit / post-merge hooks — use the **single** fenced
#    bash block under [Automation Components](#automation-components) →
#    "Staging watch" (path-based dispatcher + optional post-merge copy).
#    Do not maintain a second copy of that script in this doc.
```

### Daily dev workflow

```bash
# 1. Create feature worktree (branch must exist — see "Creating a feature worktree")
git worktree add ~/.openclaw-worktrees/feat-my-feature feat/my-feature

# 2. Edit files in the worktree
cd ~/.openclaw-worktrees/feat-my-feature
$EDITOR openclaw.json  # or any file

# 3. Run tests from that worktree
python3 -m pytest tests/ -v

# 4. Commit + push → PR auto-created
git add . && git commit -m "feat: ..."
git push -u origin feat/my-feature

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

# Check all Python tests (from prod worktree checkout)
cd ~/.openclaw && python3 -m pytest tests/ --tb=short
```
