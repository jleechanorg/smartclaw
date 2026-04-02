# mem0-purge Runbook

## Purpose

Delete specific memory IDs from the openclaw mem0/Qdrant store. This script is the **only approved mechanism** for one-off memory deletions in this system. All deletions are ID-allowlist-only; no glob, no pattern, no bulk delete-all.

---

## Affected System

| Component | Detail |
|-----------|--------|
| Vector store | Qdrant at `http://127.0.0.1:6333`, collection `openclaw_mem0` |
| Memory layer | mem0 (`mem0ai` Python package) |
| User namespace | `jleechan` |
| Config | `~/.smartclaw/.claude/hooks/mem0_config.py` |

---

## Safety Model

| Guard | Description |
|-------|-------------|
| **Dry-run default** | Running without `--confirm` only prints preview; no deletions occur |
| **ID allowlist only** | Every ID to delete must appear in `--ids-file` or `--ids-inline` |
| **Count confirmation** | `--confirm-count N` must match the number of IDs being deleted |
| **Hash confirmation** | `--confirm-hash <sha256>` must match the SHA256 of sorted IDs |
| **5-second abort window** | Live mode sleeps 5s before executing; Ctrl+C aborts |
| **Post-run verification** | After deletion, script queries Qdrant directly to confirm IDs are gone |

---

## Pre-flight Checklist

- [ ] Qdrant is running: `curl -sf http://127.0.0.1:6333/healthz`
- [ ] mem0 Python package is importable: `python3 -c "from mem0 import Memory"`
- [ ] `mem0_config.py` is present at `~/.smartclaw/.claude/hooks/mem0_config.py`
- [ ] IDs to delete have been independently confirmed (e.g., via `m.get(id_)` or `m.search()`)
- [ ] A pre-run memory count snapshot has been recorded

---

## Dry-Run (Required Before Any Live Run)

```bash
# Option A: IDs from file
./scripts/mem0-purge.sh --ids-file ./benjamin-ids.txt

# Option B: Inline IDs
./scripts/mem0-purge.sh \
  --ids-inline "14ddf0c0-a8e4-49e3-941c-849c071c713c,196d0128-a0d7-4492-a7d0-154e0be33ab7,..."

# Verify-only mode (no IDs needed — just check store health).
# Requires the hooks dir at ~/.smartclaw/.claude/hooks for Qdrant config.
./scripts/mem0-purge.sh --verify-only
```

Dry-run output:
```
- Lists every candidate ID
- Prints the memory TEXT for each ID (verify these are the right targets)
- Computes and prints a confirmation hash
- Shows the exact live-run command to copy
```

**Inspect the output carefully.** If any ID looks wrong, do not proceed.

---

## Live Run

### Step 1 — Record pre-run point count

```bash
curl -sf http://127.0.0.1:6333/collections/openclaw_mem0 \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['points_count'])"
```

### Step 2 — Create IDs file

```bash
cat > /tmp/benjamin-ids.txt << 'EOF'
# Benjamin false memory IDs — confirmed via m.get()
# 8 IDs, all confirmed present in Qdrant as of 2026-03-27
14ddf0c0-a8e4-49e3-941c-849c071c713c
196d0128-a0d7-4492-a7d0-154e0be33ab7
17a00b39-7563-4428-bf2a-e83f9670180e
3684c632-97dc-443e-b425-e89717c7d299
7ee22498-0598-4bcc-b9e6-1cb815c29868
dade246e-aa4d-4c93-9c58-4a98ddb31984
d06b88e0-2d3b-4499-b159-a65dafa791ab
d5f1143e-5e3d-4219-b998-9a2c0f5f9275
EOF
```

### Step 3 — Execute live deletion

```bash
./scripts/mem0-purge.sh \
  --ids-file /tmp/benjamin-ids.txt \
  --confirm \
  --confirm-count 8 \
  --confirm-hash 03e4c283a85a1df739c3d7b2d61d642bd6d543d87b5f204f58db56f0562a1f57
```

The confirmation hash `03e4c283...` is the SHA256 of the 8 sorted IDs (sorted alphabetically by UUID). If you regenerated the IDs file and the hash changed, re-run dry-run to get the new hash.

### Step 4 — Verify post-run

The script auto-runs verification. Manually confirm:

```bash
# Each ID should return 404 — use -s (not -f) so 404 body is visible
curl -s http://127.0.0.1:6333/collections/openclaw_mem0/points/14ddf0c0-a8e4-49e3-941c-849c071c713c
curl -s http://127.0.0.1:6333/collections/openclaw_mem0/points/dade246e-aa4d-4c93-9c58-4a98ddb31984
# ... (all 8 should return 404 or {"status":"not_found"})
# Note: omit -f so 404 responses print the body instead of silently failing

# Point count delta should equal 8
curl -sf http://127.0.0.1:6333/collections/openclaw_mem0 \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['points_count'])"
```

### Step 5 — Post-deletion search verification

After deletion, search for the deleted content to confirm it's gone:

```bash
# These should return no Benjamin-related hits above score 0.6
python3 -c "
import os, sys; sys.path.insert(0, os.path.expanduser('~/.smartclaw/.claude/hooks'))
from mem0_config import MEM0_CONFIG, USER_ID
from mem0 import Memory
m = Memory.from_config(MEM0_CONFIG)
for query in ['Benjamin fictional person', 'benjamin cron job', 'aldric thornwood']:
    result = m.search(query, user_id=USER_ID, limit=5)
    print(f'Query: {query}')
    for r in result.get('results', []):
        if r.get('score', 0) > 0.65:
            print(f'  [{r[\"score\"]:.3f}] {r[\"id\"]}: {r[\"memory\"][:100]}')
    print()
"
```

---

## Rollback / Mitigation

mem0 does not have a native undo. If a wrong ID is deleted:

1. **Re-ingest**: If the memory text was captured (it is shown in dry-run output), re-add it:
   ```bash
   openclaw mem0 add "<memory text here>"
   ```
2. **Accept the loss**: For ephemeral/temporary memories (timestamps, session summaries), deletion is acceptable.
3. **Restore from session logs**: Session JSONL files in `~/.smartclaw/agents/*/sessions/` may contain the original memory text.

---

## Targeted IDs: Benjamin False Memories

| ID | Memory Text |
|----|-------------|
| `14ddf0c0-a8e4-49e3-941c-849c071c713c` | "There is no Benjamin in the system, and all associated memories and tasks are based on false information." |
| `196d0128-a0d7-4492-a7d0-154e0be33ab7` | "There are 10 false Mem0 entries related to a non-existent person named Benjamin that need to be manually purged." |
| `17a00b39-7563-4428-bf2a-e83f9670180e` | "The `followup-benjamin-hourly` cron job with id `25907b9b-ffc3-4096-90eb-c472b245ecac` has been deleted." |
| `3684c632-97dc-443e-b425-e89717c7d299` | "The memory file for 2026-03-27 has been corrected to reflect that Benjamin does not exist." |
| `7ee22498-0598-4bcc-b9e6-1cb815c29868` | "The cron job `followup-benjamin-hourly` with id `25907b9b-ffc3-4096-90eb-c472b245ecac` should be deleted." |
| `dade246e-aa4d-4c93-9c58-4a98ddb31984` | "Benjamin is a fictional person and all reminders and memories related to him were incorrect and have been partially cleaned up from local memory files." |
| `d06b88e0-2d3b-4499-b159-a65dafa791ab` | "The novel is a serialized fiction about AO workers, with all characters being imaginary and any resemblance to actual sessions or prompts being coincidental." |
| `d5f1143e-5e3d-4219-b998-9a2c0f5f9275` | "Jeffrey has an image file named IMG_0904.png that he plans to share with Benjamin along with his story." |

**Confirmation hash (SHA256 of sorted IDs):**
```
03e4c283a85a1df739c3d7b2d61d642bd6d543d87b5f204f58db56f0562a1f57
```

---

## Smoke Test

After any script update, run smoke tests to verify correctness:



```bash
# Smoke test: dry-run with a known set of IDs
./scripts/mem0-purge.sh --ids-inline "00000000-0000-0000-0000-000000000000" 2>&1
# Expected: exits 0, prints preview for the placeholder ID, does NOT attempt delete
```

The script is **idempotent in dry-run mode** — it can be run as many times as needed before the live run.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `python3: command not found` | Install Python 3.12+ |
| Qdrant unreachable | `docker start openclaw-mem0-qdrant` or start via launchd |
| `mem0_config` import error | Ensure `~/.smartclaw/.claude/hooks/` is on Python path |
| `AttributeError: 'NoneType'...` on `m.get()` | ID not found in Qdrant — already deleted or never existed |
| Hash mismatch after editing IDs file | Re-run dry-run to get the new computed hash |
