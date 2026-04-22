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

## Install / First-Time Setup
```bash
# 1. Install the mem0 plugin (if not already installed)
openclaw plugins install @mem0/openclaw-mem0

# 2. Ensure backends are running
# Qdrant (via Docker):
docker start openclaw-mem0-qdrant 2>/dev/null || docker run -d --name openclaw-mem0-qdrant \
  -p 6333:6333 -p 6334:6334 \
  -v $HOME/.smartclaw/qdrant_storage:/qdrant/storage \
  qdrant/qdrant

# Ollama (embedder + LLM):
ollama serve  # if not running

# 3. Rebuild native modules after Node version change
cd $HOME/.smartclaw/extensions/openclaw-mem0
npm rebuild better-sqlite3 --build-from-source

# 4. Patch mem0ai to suppress Qdrant version-check warning
# (safe: both are 0.3.0, Qdrant server 1.17.0 vs client 1.13.0 — minor version diff >1)
python3 -c "
content = open('$HOME/.smartclaw/extensions/openclaw-mem0/node_modules/mem0ai/dist/oss/index.js').read()
old = 'this.client = new import_js_client_rest.QdrantClient(params);'
new = 'this.client = new import_js_client_rest.QdrantClient({ ...params, checkCompatibility: false });'
if old in content:
    content = content.replace(old, new)
    open('$HOME/.smartclaw/extensions/openclaw-mem0/node_modules/mem0ai/dist/oss/index.js', 'w').write(content)
    print('Patched: checkCompatibility=false')
else:
    print('Already patched or unexpected content')
"

# 5. Verify
openclaw mem0 stats
# Should show: Total memories: N (no Qdrant version warning, no duplicate plugin warning)
```

## Verify End-to-End
```bash
# No warnings expected after patch:
openclaw mem0 stats

# Real recall test:
openclaw mem0 search "AO spawn session management"
# Should return relevant memories with scores >0.7
```

## Rules
- Do not treat canary (`--agent memqa`) totals as production totals.
- Do not assume 50Q pass rate equals successful historical backfill.
- Use base `openclaw mem0 stats` + targeted recall checks to validate production memory.
- After Node version upgrade: always rebuild `better-sqlite3` native module.
- The `extensions/openclaw-mem0/` bundled copy was removed from smartclaw — use the globally installed npm package instead.
