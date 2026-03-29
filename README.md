# smartclaw

`smartclaw` is a lightweight, public harness repo for running an OpenClaw-based automation setup on macOS.

It includes:
- launchd templates for scheduler/health/lifecycle processes
- reusable OpenClaw skills used by this setup
- reference docs for harness engineering and zero-touch operation

## Dependency: Agent Orchestrator (AO)

This setup relies on **`jleechanorg/agent-orchestrator`**, a fork of AO that provides agent session orchestration and lifecycle management.

- Repo: https://github.com/jleechanorg/agent-orchestrator

## What is in this repo

- `launchd/`
  - `smartclaw.scheduler.plist.template` — runs OpenClaw's scheduler (`gateway run-scheduler`) against `cron/jobs.json`
  - `smartclaw.agento-manager.plist.template` — manages AO lifecycle workers/manager loops
  - `smartclaw.lifecycle-manager.plist.template` — lifecycle service template
  - `smartclaw.health-check.plist.template` — periodic health-check service template
  - `smartclaw.monitor-agent.plist` / `smartclaw.health-check.plist` — concrete examples
- `skills/`
  - `dispatch-task/` — AO dispatch workflow skill
  - `cmux/` — cmux control skill
  - `antigravity-computer-use/` and `claude-code-computer-use/` — computer-use automation pointers/guidance
  - `er.md` — evidence review workflow
- `docs/`
  - `HARNESS_ENGINEERING.md`
  - `ZERO_TOUCH.md`

## Scope

This repository is intentionally focused on **harness/config patterns** (skills, launchd wiring, operational docs), not product application code.

## License

MIT (see `LICENSE`).
