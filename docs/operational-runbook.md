# OpenClaw Operational Runbook

Reference doc extracted from CLAUDE.md to reduce context window consumption.
Sections moved here are operational procedures agents rarely need but must follow when relevant.

## Gateway restart — single-instance mandatory

After ANY gateway restart (deploy, manual, launchd bounce), verify exactly **one** `openclaw-gateway` process is running before declaring success:

```bash
pgrep -x openclaw-gateway | wc -l   # must be 1
```

If count > 1: multiple instances are competing for session locks → lock storm → WS pong starvation → total HTTP unresponsiveness. Fix:

```bash
pkill -x openclaw-gateway           # kill all
# clear stale locks (see session lock section below)
launchctl start gui/$(id -u)/com.smartclaw.gateway
sleep 20 && pgrep -x openclaw-gateway | wc -l   # verify == 1
```

`deploy.sh` now enforces this automatically (Stage 4 orphan kill + single-instance assertion). `staging-canary.sh` check 9 also validates it.

**Root cause of 2026-04-05 outage**: deploy.sh Stage 4 used `launchctl stop` + `launchctl start` without killing orphaned processes first. Three instances spawned, competed for `sessions.json.lock`, and the gateway became completely unresponsive despite HTTP `/health` returning 200.

## WS Churn Root Cause — Restart Is NOT the Fix

When `SlackWebSocket:N > 5` appears in gateway logs, or the canary fails (rc=4) despite HTTP 200, **the root cause is event-loop saturation from LLM calls blocking the Node.js thread**:

- `timeoutSeconds > 600` combined with `maxConcurrent > 3` = WS pong starvation (pong budget = 5000ms)
- **Subagents**: `agents.defaults.subagents.maxConcurrent` must stay within the same spirit (use **≤ 3**). Values like **8** can saturate the gateway event loop even when main `maxConcurrent` is 3.
- **The correct fix**: reduce both in `openclaw.json`, then restart. Restart alone only clears the counter — sessions re-block immediately.
- **Safe bounds**: `timeoutSeconds ≤ 600`, `maxConcurrent ≤ 3`, `subagents.maxConcurrent ≤ 3`
- **Derivation** (so future agents can reason, not just copy numbers):
  - Risk ∝ `timeoutSeconds × maxConcurrent`. Incident was 900×20=18000. Pong starvation threshold empirically ~3000-5000.
  - With `maxConcurrent=3`: `3000/3 = 1000`, use 600 for conservative margin.
  - If `maxConcurrent` changes: `safe_timeout = floor(3000 / maxConcurrent * 0.65)` (e.g. concurrent=8→243≈240, concurrent=3→650→600 with additional margin)
  - **Do NOT change the bound without recalculating from this formula.**

```python
# Surgical fix (never rewrite the whole file)
import json; path="$HOME/.smartclaw/openclaw.json"
with open(path) as f: d=json.load(f)
d['agents']['defaults']['timeoutSeconds'] = 600
d['agents']['defaults']['maxConcurrent'] = 3
with open(path,'w') as f: json.dump(d,f,indent=2)
```

Then: `openclaw gateway restart`

## LLM provider HTTP 2064 (high load)

When the primary model or gateway returns **HTTP 2064** / *server cluster is under high load*, **do not** fan out many simultaneous attach/diagnose or multi-session calls. Retry after a short wait with backoff; if it persists, lower `agents.defaults.maxConcurrent` and subagent concurrency (same event-loop discipline as WS churn above). Bulk "attach to all workers" requests will amplify this failure mode.

**Dropped messages**: Redrive using `SLACK_USER_TOKEN` (jleechan identity), NOT the openclaw bot token (gateway ignores its own messages). Check ${SLACK_CHANNEL_ID} and ${SLACK_CHANNEL_ID} for unanswered jleechan messages in the past 2 hours.

## openclaw.json mutation safety

**NEVER rewrite the entire `openclaw.json` file.** Always use targeted key updates:

```python
with open(path) as f: d = json.load(f)
d['some']['nested']['key'] = new_value   # surgical update only
with open(path, 'w') as f: json.dump(d, f, indent=2)
```

Full rewrites silently drop config sections not present in the current Python scope (e.g. `agents.defaults.heartbeat` disappeared when model was updated this way on 2026-03-23). After any `openclaw.json` write, verify critical keys survived: `agents.defaults.heartbeat`, `gateway.auth`, `models.providers`.

### Protected keys — NEVER change these values

These keys have constraints enforced by `doctor.sh` and are validated every monitor run. Changing them breaks the health check and triggers STATUS=PROBLEM alerts. **Treat them as immutable unless Jeffrey explicitly requests a change:**

| Key | Required value | Why |
|-----|---------------|-----|
| `agents.defaults.heartbeat.every` | `"5m"` | Doctor enforces 5m; agents have changed this to 30m twice (2026-04-04) |
| `agents.defaults.heartbeat.target` | `"last"` | Doctor enforces this |
| `agents.defaults.timeoutSeconds` | `≤ 600` | WS pong budget; higher = event-loop starvation |
| `agents.defaults.maxConcurrent` | `≤ 3` | Same WS budget (safe_timeout = floor(3000/n × 0.65)) |
| `agents.defaults.subagents.maxConcurrent` | `≤ 3` | Same event-loop discipline |
| `plugins.slots.memory` | `"openclaw-mem0"` | Without this, gateway defaults to builtin `memory-core` and mem0 plugin is silently disabled even when `plugins.entries.smartclaw-mem0.enabled: true` |

## Gateway Upgrade Safety

**MANDATORY: Run pre-flight before ANY gateway version change, `openclaw doctor --fix`, or plist modification.**

```bash
bash ~/.smartclaw/scripts/gateway-preflight.sh        # check only
bash ~/.smartclaw/scripts/gateway-preflight.sh --fix   # check and auto-repair
```

**Skill**: `~/.claude/skills/gateway-upgrade.md` — full upgrade/rollback runbook.

Key rules:
- **Never run `openclaw doctor --fix` without checking for existing plists first** — it can create duplicates
- **ThrottleInterval in gateway plist must be >= 10** (preferably 30) — values < 10 cause restart storms that burn Slack tokens
- **After ANY upgrade**: verify config JSON valid, critical keys survived, native modules load, Slack connected
- **Backup `openclaw.json` before upgrade**: `cp ~/.smartclaw/openclaw.json ~/.smartclaw/openclaw.json.pre-upgrade-$(date +%s)`
- **MANDATORY before ANY restart**: verify `meta.lastTouchedVersion` in `~/.smartclaw-consensus/openclaw.json` matches running binary version — mismatch causes infinite AJV recursion → `RangeError: Maximum call stack size exceeded` crash (incident 2026-03-30). Run `bash ~/.smartclaw/scripts/gateway-preflight.sh --fix` to auto-correct.

### Staging Bootstrap Broken State ("Bootstrap failed: 5: Input/output error")

**Symptoms:**
- `launchctl bootstrap gui/$UID ~/Library/LaunchAgents/ai.smartclaw.staging.plist` fails with "Bootstrap failed: 5: Input/output error"
- `launchctl print-disabled gui/$UID | grep staging` shows `"ai.smartclaw.staging" => enabled`
- `launchctl print gui/$UID/ai.smartclaw.staging` fails (service not registered)
- Manually-started staging processes receive SIGTERM within seconds of starting

**Root cause:** launchd service database is in a corrupted state — the plist is marked enabled but never successfully bootstrapped.

**Fix:**
```bash
# Step 1: Fully remove from launchd database
launchctl unload -w ~/Library/LaunchAgents/ai.smartclaw.staging.plist 2>/dev/null || true

# Step 2: Verify disabled
launchctl print-disabled gui/$UID | grep staging
# Should show: "ai.smartclaw.staging" => disabled

# Step 3: Re-bootstrap
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/ai.smartclaw.staging.plist

# Step 4: Start
launchctl start gui/$UID/ai.smartclaw.staging
sleep 5
curl -fsS -m 5 http://127.0.0.1:18810/health
```

If bootstrap still fails after step 1-4, run `bash ~/.smartclaw/scripts/install-launchagents.sh` to regenerate the plist.

## Worktree Isolation — Edit Your Copy, Not ~/.smartclaw/ Directly

`~/.smartclaw/` is the **staging** environment (the repo checkout). `~/.smartclaw_prod/` is **production** (separate dir with symlinks). Direct edits to `~/.smartclaw/` affect staging immediately but NOT production — production only updates when `scripts/deploy.sh` syncs validated config.

**Rule (ALL agent sessions): Do NOT directly edit files in `~/.smartclaw/` — use a PR.**

All changes to `~/.smartclaw/` files MUST go through: edit in worktree → commit → PR → merge → `git pull` in `~/.smartclaw/`. Then `scripts/deploy.sh` to promote to prod. Direct edits bypass code review AND the staging canary gate (`scripts/staging-canary.sh`).

**Permitted exceptions (only these three):**
1. `openclaw.json` — surgical key updates only (see "openclaw.json mutation safety" above)
2. `cron/jobs.json` — live job management, documented exception
3. Emergency hot-fixes explicitly authorized by the user in the current session — must be followed by a cleanup PR

**Correct flow for any file:**
1. Edit the file in your worktree (`<worktree>/some-file`)
2. Commit and push PR
3. After PR merges to main: `git pull` in `~/.smartclaw/` picks up the change automatically

**`agent-orchestrator.yaml` — rendered for runtime, not used directly as the live deploy file:**
`scripts/bootstrap.sh` renders `~/.agent-orchestrator.yaml` from `~/.smartclaw/agent-orchestrator.yaml` so shell placeholders become concrete values before AO/launchd parse the YAML. `~/agent-orchestrator.yaml` is a compatibility symlink to the rendered runtime file.

Canonical tracked path: `~/.smartclaw/agent-orchestrator.yaml`. In worktrees, edit `<worktree>/agent-orchestrator.yaml`.

## Isolated Gateway Testing

PRs that touch gateway-loaded files MUST be tested against an isolated openclaw gateway instance running from the PR worktree — not the live `~/.smartclaw/` gateway on port 18789.

**Required when PR touches any of:**
- `SOUL.md`, `TOOLS.md`, `HEARTBEAT.md` (policy files read at gateway startup)
- `.claude/commands/` or `skills/` (slash commands and skills loaded by agents)
- `agents/` (model configs, auth profiles)
- `launchd/` (plist templates)
- `health-check.sh`, `monitor-agent.sh`, `startup-check.sh` (operational scripts)
- `cron/` (scheduled job definitions)
- `agent-orchestrator.yaml` (AO dispatch config)

**Not required for:** `src/`, `tests/`, `docs/`, `roadmap/`, `.beads/`, non-operational `scripts/`

**How:** Use `openclaw --profile <name>` on a different port, symlink policy files from the PR worktree.
See `.claude/commands/evidence_review.md` section "Isolated Gateway Testing" for the full procedure.

## Config-First Principle

**Before writing Python code, check if the goal can be achieved by editing config files at the repo root.**

openclaw has rich built-in capabilities. Use them:

| Want to change | Edit this |
|---|---|
| smartclaw behavior / decision-making | `SOUL.md` (at repo root = `~/.smartclaw/SOUL.md`) |
| Tool allow/deny list | `TOOLS.md` or `openclaw.json` |
| Memory, history, compaction settings | `openclaw.json` (memorySearch, dmHistoryLimit, compaction) |
| Cron / scheduled tasks (Slack, backup, memory) | `cron/` (at repo root) |
| **PR automation** jobs (pr-monitor, fixpr, etc.) | `~/.smartclaw/cron/jobs.json` directly — **exception**, not tracked in repo |
| AO project config / reactions / notifiers | `<worktree>/agent-orchestrator.yaml` → PR → merge (tracked source: `~/.smartclaw/agent-orchestrator.yaml`, rendered runtime copy: `~/.agent-orchestrator.yaml`, compatibility symlink: `~/agent-orchestrator.yaml`) |
| New Python orchestration logic | `src/orchestration/` — **only if config cannot express it** |

New Python code in `src/` is for capabilities that genuinely don't exist in openclaw's config surface. Everything else is config. See `roadmap/NATURAL_LANGUAGE_DISPATCH.md` for the rationale.

## Slack — Reading threads and incident questions

**COMMIT (non-negotiable):** If the user pastes a `https://*.slack.com/archives/...` link, asks whether the **bot/gateway/service is down**, wants **thread or channel context**, or asks what was said in Slack — **call Slack MCP read APIs first** (e.g. `conversations_history`, `conversations_replies`, or the MCP tools your client exposes with the same semantics — in Claude Code this is often `mcp__slack__conversations_history` / `mcp__slack__conversations_replies`). **Do not** answer from guesswork, memory, or curl-only health checks alone, and **do not** say you "cannot access Slack" while Slack MCP is available.

- Parse **channel id** from the URL path segment after `/archives/` (starts with `C` or `G`).
- Use **`thread_ts`** from the query string when present; for permalink URLs with `p1775...`, derive `thread_ts` per Slack's format if the tool requires it.
- If MCP fails, report the **exact error**, then fall back only as policy allows (e.g. `SLACK_USER_TOKEN` for private reads).

**Why this exists:** Operators paste thread links to ask "are we down?" — answering from local `curl /health` without reading the thread **misses the question** and repeats the same mistake.

## Slack — Posting

**USE THE SLACK MCP FIRST:** `mcp__slack__conversations_add_message(channel_id="${SLACK_CHANNEL_ID}", text="...")`. Posts as openclaw bot. No token setup needed.

- Channel IDs: `#ai-slack-test` → `$SLACK_TEST_CHANNEL`, `#all-jleechan-ai` → `${SLACK_CHANNEL_ID}`, jleechan DM → `$JLEECHAN_DM_CHANNEL`
- All channel/user IDs are env vars in `~/.bashrc`
- **Fallback**: curl with `$SLACK_BOT_TOKEN` from `~/.bashrc`
- `SLACK_USER_TOKEN` (xoxp-...) in `~/.profile` is valid — required when you need OpenClaw to react (gateway ignores its own bot messages)

**For agento dispatch** — MUST post as jleechan, not bot:
```bash
# SLACK_USER_TOKEN is exported from ~/.bashrc/~/.zshrc (see README env-var block) or ~/.profile
source ~/.profile && curl -s -X POST "https://slack.com/api/chat.postMessage" \
  -H "Authorization: Bearer $SLACK_USER_TOKEN" -H "Content-Type: application/json" \
  -d "{\"channel\": \"$AGENTO_CHANNEL\", \"text\": \"agento <task>\"}"
```
Do NOT use Slack MCP for agento triggers — MCP posts as bot, which gateway silently ignores.

## Durable Behavior Goal (Not Incident-Only)

Primary intent: OpenClaw should behave consistently for repeated user requests, not require one-off fixes per thread/PR.

Execution rules:
1. Treat behavior bugs as system bugs first (config/policy/workflow contract), not isolated incidents.
2. Prefer reusable guardrails in config files at repo root (`~/.smartclaw/`) and shared automation templates over ad-hoc local patches.
3. Enforce explicit routing and target resolution for external actions (repo, endpoint, channel) before mutation.
4. Add fail-closed checks (tests/CI/policy validators) so the same class of error cannot silently recur.
5. Validate fixes by replaying the same request style in multiple contexts.
