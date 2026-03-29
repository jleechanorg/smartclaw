# Webhook Pipeline Operator Runbook

## System overview

The webhook pipeline receives GitHub App webhook deliveries via an HTTP ingress
(port 9100), validates HMAC-SHA256 signatures, deduplicates by `X-GitHub-Delivery`
ID, and persists raw payloads to a SQLite queue (`~/.openclaw/webhook_queue.db`).
A worker normalises events, acquires a per-PR SQLite advisory lock, and dispatches
remediation tasks through Symphony.  A reconciler cron runs every 5 minutes to
reset stuck `IN_PROGRESS` events, recover stale `PENDING` events, and re-enqueue
exhausted `FAILED` events.  Lightweight in-process metrics are collected by
`MetricsCollector` and alerts are evaluated by `check_slo_alerts`.

---

## Normal operation checklist

Run these checks to confirm the pipeline is healthy:

1. **Ingress responding** — confirm the HTTP server is reachable:
   ```bash
   curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:9100/
   # Expected: 405 (Method Not Allowed for GET) — server is up
   ```

2. **Queue not growing** — pending count should be near zero between worker runs:
   ```bash
   PYTHONPATH=src python3 -c "
   from orchestration.webhook_queue import RemediationQueue
   q = RemediationQueue()
   pending = q.dequeue_pending(limit=100)
   print(f'PENDING: {len(pending)}')
   "
   ```

3. **No FAILED events** — should return an empty list under normal load:
   ```bash
   PYTHONPATH=src python3 -c "
   import sqlite3, os
   db = os.path.expanduser('~/.openclaw/webhook_queue.db')
   conn = sqlite3.connect(db)
   rows = conn.execute(\"SELECT event_id, attempt_count FROM remediation_queue WHERE status='FAILED'\").fetchall()
   print(f'FAILED: {len(rows)}')
   for r in rows: print(' ', r)
   "
   ```

4. **No STALE events** — events older than 48 hours should have been expired:
   ```bash
   PYTHONPATH=src python3 -c "
   import sqlite3, os
   db = os.path.expanduser('~/.openclaw/webhook_queue.db')
   conn = sqlite3.connect(db)
   rows = conn.execute(\"SELECT COUNT(*) FROM remediation_queue WHERE status='STALE'\").fetchone()
   print(f'STALE count: {rows[0]}')
   "
   ```

5. **Metrics snapshot** — check current in-process counter values:
   ```bash
   PYTHONPATH=src python3 -c "
   from orchestration.webhook_metrics import get_collector
   import json; print(json.dumps(get_collector().snapshot(), indent=2))
   "
   ```

---

## Alert response procedures

### ALERT: dispatch success rate X% below SLO 95%

**Trigger:** `events_dispatched / events_enqueued < 0.95`

**Likely causes:**
- Symphony worker is not running or crashed.
- Per-PR lock contention (many events for the same PR simultaneously).
- Symphony API / `symphony_daemon` returning errors.

**Response:**
1. Check worker process status:
   ```bash
   ps aux | grep webhook_worker
   ```
2. Inspect `FAILED` events and their attempt counts (see checklist item 3 above).
3. Check Symphony logs for dispatch errors:
   ```bash
   tail -50 ~/.openclaw/logs/gateway.log | grep -i "dispatch\|symphony\|error"
   ```
4. Manually re-enqueue all FAILED events (resets status to PENDING):
   ```bash
   PYTHONPATH=src python3 -c "
   import sqlite3, os
   db = os.path.expanduser('~/.openclaw/webhook_queue.db')
   conn = sqlite3.connect(db)
   n = conn.execute(\"UPDATE remediation_queue SET status='PENDING', attempt_count=0 WHERE status='FAILED'\").rowcount
   conn.commit()
   print(f'Re-enqueued {n} FAILED events')
   "
   ```

---

### ALERT: N events in FAILED state

**Trigger:** `events_failed > 10`

**Likely causes:**
- Repeated dispatch failures due to a bad Symphony config or missing secret.
- Events with `pr_number=None` that cannot acquire a meaningful PR lock.

**Response:**
1. Inspect the specific failed events:
   ```bash
   PYTHONPATH=src python3 -c "
   import sqlite3, os, json
   db = os.path.expanduser('~/.openclaw/webhook_queue.db')
   conn = sqlite3.connect(db)
   rows = conn.execute(
       \"SELECT event_id, trigger_type, repo_full_name, pr_number, attempt_count, enqueued_at \"
       \"FROM remediation_queue WHERE status='FAILED' ORDER BY enqueued_at DESC LIMIT 20\"
   ).fetchall()
   for r in rows: print(r)
   "
   ```
2. If root cause is fixed, re-enqueue as shown above.
3. If events are genuinely unrecoverable (e.g. PR already merged), mark them STALE:
   ```bash
   PYTHONPATH=src python3 -c "
   import sqlite3, os
   db = os.path.expanduser('~/.openclaw/webhook_queue.db')
   event_id = 'REPLACE_WITH_EVENT_ID'
   conn = sqlite3.connect(db)
   conn.execute(\"UPDATE remediation_queue SET status='STALE' WHERE event_id=?\", (event_id,))
   conn.commit()
   print('Marked STALE')
   "
   ```

---

### ALERT: N events marked STALE

**Trigger:** `events_stale > 20`

**Likely causes:**
- Worker has been stopped for an extended period (events aged out).
- GitHub PRs closed before remediation could complete.
- Reconciler is not running (stuck events never recovered).

**Response:**
1. Confirm the reconciler is running:
   ```bash
   ps aux | grep webhook_reconciler
   ```
2. Run a manual reconciler pass to clean up:
   ```bash
   PYTHONPATH=src python3 -c "
   from orchestration.webhook_queue import RemediationQueue
   from orchestration.webhook_reconciler import WebhookReconciler
   q = RemediationQueue()
   q.init_schema()
   r = WebhookReconciler(q)
   stats = r.run_once()
   print(f'stuck_reset={stats.stuck_reset} stale_reset={stats.stale_reset} failed_requeued={stats.failed_requeued}')
   "
   ```
3. If STALE count is expected (e.g. after a maintenance window), reset the
   in-process counter:
   ```bash
   PYTHONPATH=src python3 -c "
   from orchestration.webhook_metrics import get_collector
   get_collector().reset()
   print('Counters reset')
   "
   ```

---

## Manual queue inspection commands

**List all events with their statuses:**
```bash
PYTHONPATH=src python3 -c "
import sqlite3, os
db = os.path.expanduser('~/.openclaw/webhook_queue.db')
conn = sqlite3.connect(db)
rows = conn.execute(
    'SELECT status, COUNT(*) FROM remediation_queue GROUP BY status'
).fetchall()
for status, count in rows:
    print(f'{status}: {count}')
"
```

**Inspect a specific event by ID:**
```bash
PYTHONPATH=src python3 -c "
import sqlite3, os
db = os.path.expanduser('~/.openclaw/webhook_queue.db')
event_id = 'REPLACE_WITH_EVENT_ID'
conn = sqlite3.connect(db)
row = conn.execute(
    'SELECT * FROM remediation_queue WHERE event_id=?', (event_id,)
).fetchone()
print(row)
"
```

**Force-expire events older than 1 hour:**
```bash
PYTHONPATH=src python3 -c "
from orchestration.webhook_queue import RemediationQueue
q = RemediationQueue()
n = q.mark_stale(max_age_hours=1)
print(f'Marked {n} events STALE')
"
```

**Clear all pr_locks (use if worker appears deadlocked):**
```bash
PYTHONPATH=src python3 -c "
import sqlite3, os
db = os.path.expanduser('~/.openclaw/webhook_queue.db')
conn = sqlite3.connect(db)
n = conn.execute('DELETE FROM pr_locks').rowcount
conn.commit()
print(f'Cleared {n} locks')
"
```

---

## Escalation path

| Severity | Condition | Action |
|----------|-----------|--------|
| Warning  | 1–10 FAILED events | Inspect and re-enqueue; monitor for 1 reconciler cycle (5 min) |
| Warning  | 11–20 STALE events | Run manual reconciler pass; check if worker is alive |
| Alert    | dispatch rate < 95% | Page on-call; check Symphony and worker logs immediately |
| Alert    | > 10 FAILED events  | Page on-call; investigate dispatch layer within 30 min |
| Alert    | > 20 STALE events   | Page on-call; worker likely stopped; restart and backfill |
| Critical | DB file missing / corrupt | Restore from `~/.openclaw/` backup; replay raw deliveries from GitHub App delivery log |

GitHub App delivery log (for replay): https://github.com/organizations/YOUR_ORG/settings/apps/YOUR_APP/advanced
