# smartclaw

`smartclaw` is **not an OpenClaw fork**.

It is a copy of the settings/configuration Jeffrey uses, plus integration wiring for Agent Orchestrator.

## Required dependency

This setup explicitly **needs** the `jleechanorg/agent-orchestrator` repository:

- https://github.com/jleechanorg/agent-orchestrator

`smartclaw` expects AO-style lifecycle/session orchestration behavior from that repo.

## Quick install

Use the included installer:

```bash
./install.sh
```

Or run manually:

1. Clone this repo
2. Clone `jleechanorg/agent-orchestrator`
3. Wire launchd templates / skills from this repo into your local setup

## What is in this repo

- `launchd/` — scheduler/health/lifecycle plist templates and examples
- `skills/` — reusable skill definitions for this setup
- `docs/` — harness engineering + zero-touch notes

## Scope

This repository is intentionally focused on local harness/config patterns, not product application code.

## License

MIT (see `LICENSE`).
