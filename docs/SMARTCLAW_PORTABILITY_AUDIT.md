# SmartClaw Portability Audit

Generated: 2026-05-02 16:25:56 PDT

## Summary

- Candidate files evaluated: 1017
- Included in export map: 993
- Excluded as non-portable/private/runtime: 24

## Selection Rules

Portable candidates are selected from:
- `.github/workflows/`, `.claude/commands/`, `.claude/skills/`, `docs/`, `launchd/`, `scripts/`, `skills/`, `tests/`
- Plus selected top-level operator files (README/install/health/monitor/doctor/startup/AO config)

Non-portable paths are excluded when they contain:
- Secrets, credentials, personal runtime state, local DB/log/cache artifacts
- OpenClaw live config files and backups (`openclaw.json*`)
- Internal-only context snapshots and generated local audit artifacts

## Included Files (sample)

- `.claude/commands/agento_report.md`
- `.claude/commands/agentor.md`
- `.claude/commands/checkpoint.md`
- `.claude/commands/claw.md`
- `.claude/commands/coderabbit.md`
- `.claude/commands/cr.md`
- `.claude/commands/debug.md`
- `.claude/commands/eloop.md`
- `.claude/commands/er.md`
- `.claude/commands/evidence_review.md`
- `.claude/commands/harness.md`
- `.claude/commands/history.md`
- `.claude/commands/learn.md`
- `.claude/commands/nextsteps.md`
- `.claude/commands/r.md`
- `.claude/commands/research.md`
- `.claude/commands/roadmap.md`
- `.claude/commands/smartclaw-export.md`
- `.claude/skills/agento_report.md`
- `.claude/skills/deploy-hermes/SKILL.md`
- `.claude/skills/evolve_loop/SKILL.md`
- `.claude/skills/smartclaw-eloop/SKILL.md`
- `.claude/skills/mem0-memory-operations.md`
- `.claude/skills/nextsteps.md`
- `.claude/skills/openclaw-harness/SKILL.md`
- `.claude/skills/openclaw-models.md`
- `.claude/skills/second-opinion-mcp-auth.md`
- `.claude/skills/smartclaw-portability-export.md`
- `.claude/skills/staging-prod-pipeline/SKILL.md`
- `.github/workflows/coderabbit-ping-on-push.yml`
- `.github/workflows/green-gate.yml`
- `.github/workflows/skeptic-cron.yml`
- `.github/workflows/staging-canary-full.yml`
- `.github/workflows/staging-canary-gate.yml`
- `AUTO_START_GUIDE.md`
- `BACKUP_AND_RESTORE.md`
- `README.md`
- `SETUP.md`
- `SLACK_SETUP_GUIDE.md`
- `agent-orchestrator.yaml`
- `docs/AO_EXHAUSTIVE_AUDIT_FINDINGS.md`
- `docs/CRON_MIGRATION.html`
- `docs/CRON_MIGRATION.md`
- `docs/GENESIS_DESIGN.md`
- `docs/HARNESS_ENGINEERING.md`
- `docs/HUMAN_CHANNEL_BRIDGE.html`
- `docs/HUMAN_CHANNEL_BRIDGE.md`
- `docs/INCIDENT_OPENCLAW_2026328_WS_STREAM.html`
- `docs/INCIDENT_OPENCLAW_2026328_WS_STREAM.md`
- `docs/ORCHESTRATION_RESEARCH_2026.md`
- `docs/ORCHESTRATION_SYSTEM_DESIGN.html`
- `docs/ORCHESTRATION_SYSTEM_DESIGN.md`
- `docs/POSTMORTEM_2026-03-19_SMARTCLAW_ROUTING.md`
- `docs/SMARTCLAW_PORTABILITY_AUDIT.html`
- `docs/SMARTCLAW_PORTABILITY_AUDIT.md`
- `docs/STAGING_PIPELINE.html`
- `docs/STAGING_PIPELINE.md`
- `docs/SWITCH_TO_HERMES.html`
- `docs/SWITCH_TO_HERMES.md`
- `docs/ZOE_AGENT_SWARM_REFERENCE.md`
- _...truncated; see `scripts/smartclaw-export-map.tsv` for full list._

## Excluded Files (sample)

- `docs/context/CRON_JOBS_BACKUP.html`
- `docs/context/CRON_JOBS_BACKUP.json`
- `docs/context/CRON_JOBS_BACKUP.md`
- `docs/context/DOC_GAPS.md`
- `docs/context/FILE_MAP.md`
- `docs/context/LEARNINGS.html`
- `docs/context/LEARNINGS.md`
- `docs/context/PRODUCT.md`
- `docs/context/PROMPTING_GUIDES.md`
- `docs/context/SYSTEM_SNAPSHOT.html`
- `docs/context/SYSTEM_SNAPSHOT.md`
- `docs/context/WORKFLOWS.md`
- `docs/superpowers/plans/2026-03-26-orch-k0e-pr-coverage-audit.md`
- `docs/superpowers/plans/2026-03-28-ao-runner-implementation.md`
- `docs/superpowers/plans/2026-03-28-sync-to-smartclaw.html`
- `docs/superpowers/plans/2026-03-28-sync-to-smartclaw.md`
- `docs/superpowers/specs/2026-03-28-self-hosted-runner-pypi-design.md`
- `launchd/ai.agento.dashboard.plist`
- `launchd/ai.smartclaw.gateway.plist`
- `launchd/ai.smartclaw.github-intake.plist`
- `launchd/ai.smartclaw.health-check.plist`
- `launchd/ai.smartclaw.monitor-agent.plist`
- `launchd/ai.smartclaw.schedule.bug-hunt-9am.plist`
- `launchd/com.jleechan.ai-reviewer-stress-test.plist`
