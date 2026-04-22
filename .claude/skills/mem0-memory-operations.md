---
name: mem0-memory-operations
description: Operate and troubleshoot OpenClaw mem0 memory usage (stats, search, writes, ingest resume, namespace checks)
type: reference
scope: project
---

# Mem0 Memory Operations

## Purpose
Use this runbook to safely operate memory retrieval and ingest without mixing benchmark namespaces with production memory.

## Namespaces
- Base namespace (`jleechan`): production memory scope.
- Canary namespace (`jleechan:agent:memqa`): 50Q benchmark scope.

## Health Checks
```bash
# Qdrant
curl -sf http://127.0.0.1:6333/healthz

# Base memory count
openclaw mem0 stats

# Canary memory count
openclaw mem0 stats --agent memqa

# Raw point count in vector store
curl -sS http://127.0.0.1:6333/collections/openclaw_mem0 | jq '.result.points_count'
```

## Read Memories
```bash
# Base namespace search
openclaw mem0 search "<query>"

# Canary namespace search
openclaw mem0 search "<query>" --agent memqa

# Scope-specific search
openclaw mem0 search "<query>" --scope long-term
openclaw mem0 search "<query>" --scope session
```

## Write Memories
```bash
# Base namespace
openclaw mem0 add "<fact>"

# Canary namespace
openclaw mem0 add "<fact>" --agent memqa
```

## Resume Bulk Ingest
```bash
# Ensure backend is up
docker start openclaw-mem0-qdrant >/dev/null 2>&1 || true

# Resume from checkpointed state
cd "$(pwd)"
nohup node "$HOME/.smartclaw/scripts/mem0_ingest_all4_careful.mjs" >> "$HOME/.smartclaw/mem0-ingest-all4/progress.log" 2>&1 &
```

## Progress Snapshot
```bash
node -e 'const s=require(process.env.HOME+"/.smartclaw/mem0-ingest-all4/state.json"); const left=s.files.length-s.cursor; const pct=(s.cursor/s.files.length*100).toFixed(2); console.log({cursor:s.cursor,total:s.files.length,left,pct,ingested:s.totals.ingested,skipped:s.totals.skipped,errors:s.totals.errors});'
```

## Storage Paths
- Ingest state: `$HOME/.smartclaw/mem0-ingest-all4/state.json`
- Ingest log: `$HOME/.smartclaw/mem0-ingest-all4/progress.log`
- Ingest script: `$HOME/.smartclaw/scripts/mem0_ingest_all4_careful.mjs`
- Qdrant storage: `$HOME/.smartclaw/qdrant_storage`

## Rules
- Do not treat canary (`--agent memqa`) totals as production totals.
- Do not assume 50Q pass rate equals successful historical backfill.
- Use base `openclaw mem0 stats` + targeted recall checks to validate production memory.
