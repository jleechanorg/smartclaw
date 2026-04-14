---
name: smartclaw-portability-export
description: Analyze repo portability, regenerate smartclaw export map, run export sync PR, and report portable vs non-portable scope
type: workflow
---

# SmartClaw Portability Export

## Purpose

Keep `smartclaw` -> `smartclaw` export current by:
- classifying portable vs non-portable files,
- regenerating `scripts/smartclaw-export-map.tsv`,
- updating audit report `docs/SMARTCLAW_PORTABILITY_AUDIT.md`,
- opening a sync PR against `jleechanorg/smartclaw`.

## Runbook

1. Refresh portability map + report:

```bash
bash scripts/update-smartclaw-export-map.sh
```

2. Sanity check the generated map:

```bash
sed -n '1,120p' scripts/smartclaw-export-map.tsv
```

3. Run export + PR flow:

```bash
bash scripts/sync-to-smartclaw.sh
```

4. Return all evidence:
- Export map diff (`scripts/smartclaw-export-map.tsv`)
- Audit report (`docs/SMARTCLAW_PORTABILITY_AUDIT.md`)
- smartclaw PR URL
- Key include/exclude rationale

## Guardrails

- Never export `openclaw.json` or runtime state (logs, DBs, memory, credentials).
- Keep launchd templates renamed to `smartclaw.*` when source is `ai.smartclaw.*`.
- Keep sanitization in `scripts/sync-to-smartclaw.sh` enabled.
