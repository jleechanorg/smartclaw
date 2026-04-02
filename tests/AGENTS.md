# tests/ — Unit and Integration Tests

This directory contains unit and integration tests that use mocks, stubs, and monkeypatching.

## How to Run

```bash
cd ${HOME}/project_smartclaw/mctrl
python -m pytest tests/ -v --tb=short
```

`pythonpath = ["src"]` is set in the root `pyproject.toml` — no manual PYTHONPATH needed.

## What Lives Here vs testing_llm/

| Here (`tests/`) | `testing_llm/` |
|---|---|
| Unit and integration tests | Real black-box verification only |
| Mocks, stubs, monkeypatching allowed | No mocks, no stubs, no injection |
| Fast (milliseconds) | Slow (real agent execution, minutes) |
| Proves isolated logic | Proves the full system works end-to-end |

## Suites

| File | What it proves |
|---|---|
| `test_mvp_loopback_e2e.py` | Registry→outbox→drain flow (mocked delivery); real Slack roundtrip |
| `test_worktree_reuse.py` | find_existing_worktree, resolve_worktree_for_branch, dispatch wiring |
| `test_reconciliation.py` | Reconciler logic: in_progress only, task_finished vs needs_human, CAS guard |
| `test_session_registry.py` | JSONL upsert, CAS update, malformed-line skipping, atomic writes |
| `test_openclaw_notifier.py` | Outbox enqueue, drain atomicity, notify_openclaw fallback |
| `test_supervisor.py` | Supervisor env parsing and startup safeguards |

## Not Proved Here

Real agent execution, real Slack delivery, real tmux sessions, real git operations.
Those are proved in `testing_llm/`.

## OpenClaw Config: This Repo IS the Live Config

**This repo is checked out at `~/.smartclaw/` — edits here are live immediately.**

| File | Purpose |
|---|---|
| `SOUL.md` | Agent identity / behavior |
| `TOOLS.md` | Tool allow/deny list |
| `openclaw.json` | Main gateway config |

**After editing, restart the gateway to pick up config changes:**
```bash
launchctl stop gui/$UID/ai.smartclaw.gateway
launchctl start gui/$UID/ai.smartclaw.gateway
```
