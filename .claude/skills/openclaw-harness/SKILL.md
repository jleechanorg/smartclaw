---
name: openclaw-harness
description: smartclaw / OpenClaw-specific harness — gateway, canary, deploy, lane backlog, staging vs prod. Use with user-scope harness-engineering.
---

# OpenClaw harness (repository-local)

**Scope:** This skill applies to **`~/.smartclaw`** (the [smartclaw](https://github.com/jleechanorg/smartclaw) harness repo) and production/staging gateway operations. It **does not** replace the general protocol in **`~/.claude/skills/harness-engineering/SKILL.md`** — it **extends** it for OpenClaw-specific failure modes.

## When to use

- Gateway “down” for Slack while **`curl /health` is 200**
- Deploy, canary, or launchd questions
- **`lane wait exceeded`** / **`queueAhead`** in `gateway.err.log`
- Staging vs prod confusion (`~/.smartclaw/` vs `~/.smartclaw_prod/`)

## Read first (mandatory)

1. **User-scope (general):** `~/.claude/skills/harness-engineering/SKILL.md` and `~/.claude/commands/harness.md`
2. **Repo root:** `CLAUDE.md` — **Gateway (Local Machine)**, WS churn, session locks, lane backlog
3. **This repo:** `agent-orchestrator.yaml` — reactions, evolve loop references

## OpenClaw-specific failure classes

| Symptom | Where to look | Harness fix layer |
|--------|----------------|-------------------|
| Slack silent, `/health` OK | `gateway.err.log`: `lane wait exceeded`, `queueAhead` | `CLAUDE.md`, `doctor.sh` + `staging-canary.sh` lane probe |
| `session file locked` | Stale `.jsonl.lock` in `$OPENCLAW_STATE_DIR/agents/main/sessions/` | Lock cleanup + restart (see `CLAUDE.md`) |
| Staging missing, prod OK | `launchctl print gui/$UID/ai.smartclaw.staging` | Bootstrap staging plist |
| Multi-gateway orphan | `pgrep -x openclaw-gateway` > expected | `deploy.sh` / `CLAUDE.md` single-instance |
| WS churn / pong starvation | `SlackWebSocket:N` high | Lower `timeoutSeconds × maxConcurrent` per `CLAUDE.md` |

## Verification checklist (after any harness change)

1. `bash scripts/doctor.sh` (or `OPENCLAW_DOCTOR_SKIP_INFERENCE=1` when appropriate)
2. `bash scripts/staging-canary.sh --port 18789` for prod config path when testing prod (includes `gateway.err.log` lane-backlog probe; parity with doctor)
3. **Required** when diagnosing Slack connectivity or “silent gateway”: send a **real Slack test message** — HTTP `/health` alone cannot prove Slack delivery; combine with steps 1–2.

## Relationship to `/harness`

- **Global `/harness`** (`~/.claude/commands/harness.md`) — 5 Whys, failure classes, audit mode — **any repo**.
- **Repo-local `/harness`** (`.claude/commands/harness.md` **in this workspace**) — short pointer + OpenClaw gates; **wins** for project-specific detail when both exist.

## Anti-patterns

- Declaring “fixed” after **`curl /health`** without checking **`gateway.err.log`** tail
- Editing **`~/.smartclaw/`** directly in production without deploy flow (see `CLAUDE.md` worktree rule) — harness fixes still go through **PR** unless emergency exception
