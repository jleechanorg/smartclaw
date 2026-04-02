# Antigravity Control Plane — Data Contracts

**Bead:** `ORCH-ag1`
**Status:** Design in progress

---

## Job Schema

```python
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

class Priority(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"

class JobState(StrEnum):
    QUEUED = "queued"              # waiting to be dispatched
    DISPATCHED = "dispatched"       # sent to AO worker, awaiting ack
    RUNNING = "running"            # worker confirmed, actively executing
    BLOCKED = "blocked"            # worker hit a blocker requiring human input
    COMPLETED = "completed"        # worker reported success
    FAILED = "failed"              # worker reported failure (exhausted retries or fatal error)
    DEADLETTERED = "deadlettered"  # exhausted all attempts
    CANCELLED = "cancelled"        # control plane cancelled

@dataclass
class Job:
    # Identity
    job_id: str                          # UUID v4
    idempotency_key: str                 # SHA-256 of (repo, objective, priority, correlation_id)

    # Routing
    repo: str                            # "owner/repo" e.g. "jleechanorg/smartclaw"
    worktree_path: str | None             # None = control plane creates fresh worktree
    correlation_id: str | None           # groups related jobs (e.g. parent job id)

    # Payload
    objective: str                       # natural language instruction for the worker
    priority: Priority                    # dispatch priority
    metadata: dict[str, Any] | None      # opaque to control plane; passed through to worker

    # Tracking
    state: JobState = JobState.QUEUED
    requestor: str = ""                  # e.g. "slack:@jleechan" or "cron:nightly"
    max_attempts: int = 3
    attempt: int = 1                     # current attempt number (1-based)

    # Timing
    created_at: datetime = field(default_factory=datetime.utcnow)
    scheduled_at: datetime | None = None  # when to run (None = immediately)
    dispatched_at: datetime | None = None
    started_at: datetime | None = None    # worker reported start
    completed_at: datetime | None = None
    last_heartbeat_at: datetime | None = None

    # Worker assignment
    worker_id: str | None = None         # e.g. "ao:worker:antig-smartclaw-1"

    # Result
    outcome: str | None = None            # opaque worker result
    error: str | None = None             # error message if failed
    blocker: str | None = None           # human-readable blocker if BLOCKED
    result_url: str | None = None        # e.g. PR URL if job created a PR

    # History
    history: list[JobStateTransition] = field(default_factory=list)

    # Ledger
    idempotency_checked_at: datetime | None = None


@dataclass
class JobStateTransition:
    from_state: JobState
    to_state: JobState
    at: datetime = field(default_factory=datetime.utcnow)
    reason: str | None = None
    worker_id: str | None = None
```

---

## Idempotency Key

```python
import hashlib, json

def compute_idempotency_key(
    repo: str,
    objective: str,
    priority: Priority,
    correlation_id: str | None,
) -> str:
    payload = {
        "repo": repo,
        "objective": objective,
        "priority": priority.value,
        "correlation_id": correlation_id,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]
```

**Dedupe rules:**
- On enqueue: if a job with the same idempotency key exists and is in a **terminal state** (`completed`, `failed`, `deadlettered`, `cancelled`), allow re-enqueue (new job_id, same idempotency key)
- On enqueue: if a job with the same idempotency key exists and is in a **non-terminal state** (`queued`, `dispatched`, `running`, `blocked`), return the existing `job_id` with `duplicate=True`
- Idempotency key is computed before validation — if validation fails, idempotency key is NOT recorded

---

## State Machine

```
                  ┌──────────────────────────────────────────────┐
                  │                                              │
                  ▼                                              │
┌─────────┐  ┌────────────┐  ┌───────────┐  ┌────────────┐  ┌──────▼─────┐
│ QUEUED  │─►│ DISPATCHED │─►│  RUNNING  │─►│  COMPLETED │  │ FAILED     │
└─────────┘  └────────────┘  └───────────┘  └────────────┘  └────────────┘
     │              │                │
     │              │                ├──────► BLOCKED ──► QUEUED (retry)
     │              │                │
     │              │                └──────► FAILED ──► DEADLETTERED
     │              │
     │              └──────────────────────────────► DEADLETTERED
     │
     └────────────────────────────────────────────────► CANCELLED
                                                        QUEUED ──► DEADLETTERED
```

**Valid transitions:**

| From | To | Trigger |
|---|---|---|
| `queued` | `dispatched` | Dispatcher sends job to AO worker |
| `queued` | `cancelled` | Control plane receives cancel request |
| `queued` | `deadlettered` | Recovery loop: max_attempts=0 on enqueue |
| `dispatched` | `running` | Worker acks job receipt |
| `dispatched` | `queued` | Worker NACK or delivery timeout (recovery) |
| `dispatched` | `deadlettered` | Delivery timeout after max_attempts exhausted |
| `running` | `completed` | Worker reports success |
| `running` | `failed` | Worker reports fatal error (terminal) |
| `running` | `blocked` | Worker reports human-input required |
| `running` | `deadlettered` | Recovery loop: heartbeat timeout + max_attempts exhausted |
| `blocked` | `queued` | Human resolves blocker and re-queues |
| `blocked` | `cancelled` | Human cancels |
| `failed` | `deadlettered` | Human or automation explicitly deadletters after review |

**Terminal states:** `completed`, `failed`, `deadlettered`, `cancelled` — no automatic transitions out. `failed` is included because the recovery loop handles exhaustion explicitly (worker reports `failed`; recovery loop marks `deadlettered` only after `attempt >= max_attempts`, not automatically).

---

## Lease / Heartbeat Format

Executors (AO workers) send heartbeats to the control plane every **10 minutes** via MCP Mail reply or direct HTTP callback.

```python
@dataclass
class Heartbeat:
    job_id: str
    worker_id: str
    state: JobState.RUNNING | JobState.BLOCKED
    progress: str | None          # e.g. "step 3/10: creating PR"
    screenshot_path: str | None    # path to last Antigravity screenshot
    at: datetime = field(default_factory=datetime.utcnow)
```

**Staleness threshold:** A job is stale if `last_heartbeat_at` is older than **30 minutes** from now.

**Heartbeat storage:** Latest heartbeat per job stored in `job_heartbeats.jsonl` (append-only, periodically compacted).

---

## MCP Mail Message Schemas

### Dispatch (Control Plane → AO Worker)

```python
@dataclass
class DispatchMessage:
    to: str              # "ao:worker:antig-{repo_slug}"
    subject: str = "antigravity_job"
    body: DispatchBody

@dataclass
class DispatchBody:
    job_id: str
    idempotency_key: str
    repo: str
    repo_url: str              # "https://github.com/owner/repo"
    worktree_path: str | None  # None = create fresh; worker decides path
    worktree_branch: str | None # None = use default naming convention
    objective: str
    priority: Priority
    correlation_id: str | None
    metadata: dict[str, Any] | None
    attempt: int
    max_attempts: int
    timeout_minutes: int = 120   # hard timeout
    dispatched_at: datetime
    antig_skill_path: str       # path to antigravity skill on worker
    peekaboo_bridge_socket: str # path to PeekabooBridge.sock
```

### Result (AO Worker → Control Plane)

```python
@dataclass
class ResultMessage:
    job_id: str
    idempotency_key: str
    outcome: Literal["completed", "failed", "blocked"]
    state: JobState             # the terminal or intermediate state
    result: dict[str, Any] | None   # opaque to control plane
    error: str | None
    blocker: str | None          # human-readable blocker description
    result_url: str | None       # e.g. PR URL
    screenshot_paths: list[str] | None  # evidence screenshots
    completed_at: datetime
    worker_id: str
    attempt: int
    execution_log: str | None    # execution transcript (truncated to 100KB)
```

### Cancel (Control Plane → AO Worker)

```python
@dataclass
class CancelMessage:
    to: str
    subject: str = "antigravity_cancel"
    body: CancelBody

@dataclass
class CancelBody:
    job_id: str
    reason: str
    cancelled_at: datetime
```

### Heartbeat (AO Worker → Control Plane)

```python
@dataclass
class HeartbeatMessage:
    to: str               # "ao:control-plane"
    subject: str = "antigravity_heartbeat"
    body: HeartbeatBody

@dataclass
class HeartbeatBody:
    job_id: str
    worker_id: str
    state: Literal["running", "blocked"]
    progress: str | None
    last_screenshot_path: str | None
    at: datetime
```

---

## Database Schema (SQLite)

```sql
CREATE TABLE jobs (
    job_id              TEXT PRIMARY KEY,
    idempotency_key     TEXT NOT NULL,
    repo                TEXT NOT NULL,
    worktree_path       TEXT,
    correlation_id      TEXT,
    objective           TEXT NOT NULL,
    priority            TEXT NOT NULL,   -- Priority enum value
    metadata            TEXT,            -- JSON
    state               TEXT NOT NULL,   -- JobState enum value
    requestor           TEXT,
    max_attempts        INTEGER NOT NULL DEFAULT 3,
    attempt             INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT NOT NULL,   -- ISO-8601
    scheduled_at        TEXT,
    dispatched_at        TEXT,
    started_at          TEXT,
    completed_at        TEXT,
    last_heartbeat_at   TEXT,
    worker_id           TEXT,
    outcome             TEXT,
    error               TEXT,
    blocker             TEXT,
    result_url          TEXT
);

CREATE UNIQUE INDEX idx_idempotency ON jobs(idempotency_key) WHERE state NOT IN ('completed','failed','deadlettered','cancelled');
-- Note: 'failed' is included in the exclusion because failed jobs are terminal (worker-reported exhaustion or fatal error).
-- Recovery logic (recovery_loop.py) moves failed jobs to deadlettered after max_attempts review.
CREATE INDEX idx_state ON jobs(state);
CREATE INDEX idx_repo_state ON jobs(repo, state);
CREATE INDEX idx_created ON jobs(created_at);
```

---

## JSONL Event Log

Each job also emits a structured event log to `antig_jobs/{job_id}/events.jsonl`:

```jsonl
{"ts": "2026-03-24T12:00:00Z", "event": "created", "state": "queued"}
{"ts": "2026-03-24T12:00:10Z", "event": "dispatched", "state": "dispatched", "worker_id": "ao:worker:antig-smartclaw-1"}
{"ts": "2026-03-24T12:00:15Z", "event": "started", "state": "running"}
{"ts": "2026-03-24T12:45:00Z", "event": "completed", "state": "completed", "result_url": "https://github.com/jleechanorg/smartclaw/pull/383"}
```

---

## Worktree Naming Convention

When `worktree_path` is None, the control plane creates a worktree using:

```
/tmp/{REPO_OWNER}-{REPO_NAME}-ag-{SHORT_JOB_ID}
```

e.g. `/tmp/jleechanorg-smartclaw-ag-a4f2b1`

Branch naming: `antigravity/job-{job_id}` (truncated to 50 chars + sanitized)

---

## Outcome Ledger Schema

```python
@dataclass
class OutcomeRecord:
    job_id: str
    repo: str
    objective: str
    priority: Priority
    requestor: str
    state: JobState
    attempts: int
    duration_seconds: int | None
    result_url: str | None
    error: str | None
    blocker: str | None
    created_at: datetime
    completed_at: datetime | None
    worker_id: str | None
```

Stored in `antig_outcome_ledger.jsonl` (append-only, same pattern as `outcome_ledger.jsonl` in `roadmap/OUTCOME_LEDGER_DESIGN.md`).
