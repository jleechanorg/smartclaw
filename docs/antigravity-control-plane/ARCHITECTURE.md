# Antigravity Control Plane — Architecture

**Bead:** `ORCH-ag1`
**Status:** Design in progress

---

## High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                    Antigravity Control Plane                        │
│                     (smartclaw, singleton)                       │
│                                                                     │
│  ┌──────────┐  ┌────────────┐  ┌───────────┐  ┌─────────────┐  │
│  │Scheduler │  │ Global Lock │  │Dispatcher │  │Recovery Loop│  │
│  │ (enqueue)│  │ (singleton) │  │(MCP Mail) │  │ (stale jobs)│  │
│  └────┬─────┘  └──────┬──────┘  └─────┬─────┘  └──────┬──────┘  │
│       │                │                │                │         │
│  ┌────▼────────────────▼────────────────▼────────────────▼────┐  │
│  │                    Job Store (SQLite + JSONL)                │  │
│  └─────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
                               │ MCP Mail (async, durable)
                               ▼
┌──────────────────────────────────────────────────────────────────────┐
│                    AO Workers (stateless, N × Sonnet)                │
│                                                                       │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐                 │
│  │  Worker 1   │  │  Worker 2   │  │  Worker N   │                 │
│  │(repo: foo)  │  │(repo: bar)  │  │(repo: baz)  │                 │
│  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘                 │
│         │                  │                  │                      │
│         ▼                  ▼                  ▼                      │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │           Antigravity IDE (macOS, controlled via Peekaboo)   │   │
│  │  Manager Window ──► per-repo workspace ──► Claude session    │   │
│  └──────────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────────┘
                               │
                               │ Outcomes reported via MCP Mail reply
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     Reporter + Slack Notifier                        │
│               (outcome ledger → Slack channel notification)          │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Components

### 1. Scheduler

**File:** `src/orchestration/antig_control_plane/scheduler.py`

Receives job enqueue requests (CLI, API, or webhook) and inserts them into the job store. Responsibilities:

- Validate job payload against `DataContracts` schema
- Assign idempotency key (hash of repo + objective + priority + correlation_id)
- Dedupe: reject if idempotency key already exists and job is not terminal
- Enqueue with state `queued` and `scheduled_at` timestamp
- Support priority queue ordering (CRITICAL > HIGH > NORMAL > LOW)
- Emit `job_enqueued` event to the dispatcher

**API surface:**
```python
@dataclass
class EnqueueRequest:
    repo: str                          # e.g. "jleechanorg/smartclaw"
    worktree_path: str | None         # None = create fresh worktree
    objective: str                    # natural language instruction
    priority: Priority                # CRITICAL | HIGH | NORMAL | LOW
    correlation_id: str | None        # for grouping related jobs
    metadata: dict[str, Any] | None   # opaque to control plane
    requestor: str                    # e.g. "slack:@jleechan" or "cron:nightly"
    max_attempts: int = 3

@dataclass
class EnqueueResponse:
    job_id: str                       # UUID
    idempotency_key: str
    state: JobState
    enqueued_at: datetime
    duplicate: bool                   # True if deduped
```

### 2. Global Lock (Singleton Safety)

**File:** `src/orchestration/antig_control_plane/global_lock.py`

Enforces that only one instance of the control plane runs at a time. Uses OS-level file locking + a heartbeat lease.

**Mechanism:**
1. Acquire `flock(LOCKFILE, LOCK_EX | LOCK_NB)` on `$OPENCLAW_STATE_DIR/antig_control_plane.lock`
2. Write `{pid, hostname, started_at}` to lockfile
3. Heartbeat thread updates `locked_at` every 30 seconds
4. On startup: if lockfile exists and process is alive, refuse to start
5. On startup: if lockfile exists and process is dead, break the stale lock
6. On shutdown: release lock + update lockfile with `stopped_at`

**Safety property:** Even if the Python process is SIGKILL'd, the OS releases the flock within seconds. A stale lock only survives if the machine crashes or the lockfile is corrupted.

**Recovery loop bypass:** The recovery loop also checks the global lock. If the control plane is not running (lockfile stale), recovery actions are skipped.

### 3. Dispatcher

**File:** `src/orchestration/antig_control_plane/dispatcher.py`

Reads `queued` jobs from the store and dispatches them to AO workers via MCP Mail. Responsibilities:

- Poll for `queued` jobs every 10 seconds (configurable)
- Select highest-priority job for each repo (one active job per repo at a time)
- Format job as MCP Mail message with full job payload
- Send to AO worker via `mcp__mcp-agent-mail__send_message` (or REST fallback)
- Update job state to `dispatched`, record `dispatched_at` and `worker_id`
- Handle MCP Mail delivery failures with retry (exponential backoff, max 5 attempts)
- Track `dispatched_at` per attempt

**Per-repo exclusivity:** A repo has at most one `running` or `dispatched` job at a time. This prevents two workers from operating on the same worktree simultaneously.

### 4. AO Adapter

**File:** `src/orchestration/antig_control_plane/ao_adapter.py`

Thin translation layer between the control plane and AO MCP Mail interface. Responsibilities:

- Serialize job payload to MCP Mail `send_message` format
- Deserialize AO worker response from MCP Mail reply
- Handle AO-specific errors (worker not registered, queue full, etc.)
- Map AO `worker_id` to control plane `worker_id`
- Stub/mock interface for local testing without AO

**MCP Mail contract** (see also `roadmap/AGENT_ORCHESTRATOR_MCP_MAIL_INTEGRATION.md`):

```python
# Dispatch message (control plane → AO worker)
{
    "to": "ao:worker:antig-{repo_slug}",
    "subject": "antigravity_job",
    "body": {
        "job_id": "uuid",
        "idempotency_key": "sha256...",
        "repo": "jleechanorg/smartclaw",
        "worktree_path": "/tmp/smartclaw-ag-worktree",
        "objective": "Implement feature X in repo Y",
        "priority": "HIGH",
        "correlation_id": "parent-job-uuid",
        "attempt": 1,
        "max_attempts": 3,
        "dispatched_at": "2026-03-24T12:00:00Z"
    }
}

# Result message (AO worker → control plane)
{
    "job_id": "uuid",
    "outcome": "completed" | "failed" | "blocked",
    "result": { ... },  # opaque to control plane
    "completed_at": "2026-03-24T12:30:00Z",
    "worker_id": "ao:worker:antig-smartclaw-1",
    "error": str | None,
    "attempts": 1
}
```

### 5. Executor (Per-Worker)

**File:** `src/orchestration/antig_control_plane/executor.py`

Runs inside each AO worker (or as a standalone script invoked by AO). The executor is the **Peekaboo client** that implements the Antigravity skill's screenshot-decide-act loop. Responsibilities:

- Receive job payload from dispatcher (via MCP Mail or direct invocation)
- Setup: acquire worktree (create or use existing), launch Antigravity workspace
- Loop: screenshot → decide → act → repeat (until done or blocked)
- Completion criteria: goal evidence visible in screenshot, or blocked with exact blocker
- Record per-step evidence to job's JSONL log
- Report outcome to dispatcher/reporter on completion

**Executor is stateless** with respect to the job store — it reads the job, executes, and reports. It does not poll the job store or make routing decisions.

### 6. Recovery Loop

**File:** `src/orchestration/antig_control_plane/recovery_loop.py`

Detects stale jobs and recovers them. Runs on a configurable interval (default: 5 minutes). Responsibilities:

- Find all `running` jobs where `last_heartbeat_at` is older than `max_heartbeat_age` (default: 30 minutes)
- Find all `dispatched` jobs where no worker has reported within a dispatch timeout (default: 5 minutes) — worker died before ack or ack was lost; these permanently block the per-repo slot
- For each stale job:
  - If `attempts < max_attempts`: re-enqueue as `queued` (increment `attempt`)
  - If `attempts >= max_attempts`: mark as `deadlettered`, emit alert
- Detect crashed control plane: if global lock is stale, recovery loop is a no-op
- Log all recovery actions to `recovery_log.jsonl`

**Heartbeat contract:** Executors (AO workers) must send heartbeat updates to the control plane every 10 minutes. Absence of heartbeat = stale job.

### 7. Reporter

**File:** `src/orchestration/antig_control_plane/reporter.py`

Records job outcomes and sends Slack notifications. Responsibilities:

- On `completed`: record outcome, emit Slack success notification
- On `failed`: record outcome with error, emit Slack failure notification
- On `deadlettered`: record outcome, emit Slack alert (pager-worthy)
- Maintain `outcome_ledger.jsonl` with full outcome records
- Support configurable Slack channel per job priority

**Slack integration:** Uses the same pattern as `src/orchestration/slack_util.py` (not `slack_sdk` directly). See existing Slack integration patterns in the codebase for the correct approach.

**Slack message format:**
```
[Antigravity] JOB COMPLETED
Repo: jleechanorg/smartclaw
Job: Implement feature X
Duration: 28m 14s
Attempts: 1
Worker: ao:worker:antig-smartclaw-1
Result: https://github.com/jleechanorg/smartclaw/pull/XXX
```

---

## Single-Controller Safety Model

The singleton controller enforces that **only one Antigravity control plane instance runs at a time**. This is critical because:

1. Antigravity's Manager window is single-threaded from the user's perspective — concurrent workers would fight for window focus.
2. Worktree operations (create, delete, reset) are not safe for concurrent access.
3. Global lock prevents split-brain scenarios where two control planes route the same job to different workers.

**Enforcement layers:**
1. **OS flock** — prevents two processes from acquiring the lock file simultaneously
2. **Startup check** — on start, verify no other instance is running
3. **Heartbeat** — if the control plane stops emitting heartbeats, AO workers stop accepting new jobs from it

---

## Multi-Repo Routing Model

```
                 Control Plane (singleton)
                            │
          ┌─────────────────┼─────────────────┐
          │                 │                  │
       Repo A            Repo B             Repo C
    (smartclaw)   (agent-orchestrator)  (other)
          │                 │                  │
    Worker: antig-a    Worker: antig-b    Worker: antig-c
    Worktree: /tmp/   Worktree: /tmp/    Worktree: /tmp/
    Max: 1 concurrent  Max: 1 concurrent  Max: 1 concurrent
```

**Per-repo job slots:** Each repo has a maximum of 1 running or dispatched job at a time. This is enforced by the dispatcher. A job for repo A is only dispatched if no other job for repo A is in `running` or `dispatched` state.

**Cross-repo parallelism (design assumption):** The intent is for jobs for different repos to be runnable in parallel, contingent on the Antigravity skill reliably handling multiple workspaces simultaneously. However, `UI-CAPABILITIES-INVENTORY.md` currently marks parallel workspace control as **untested/missing**, so this is a design assumption rather than a guaranteed capability. Before enabling or relying on cross-repo parallelism in production, introduce and enforce an automated gate — an `antigravity_parallel_workspaces` integration test suite — that validates stable behavior under multiple concurrent workspaces (see `UI-CAPABILITIES-INVENTORY.md`).

---

## MCP Mail Contract

The MCP Mail contract defines the message schema for control plane ↔ AO worker communication. See `DATA-CONTRACTS.md` for full schema.

**Key contract properties:**
- **Durability:** MCP Mail delivers with at-least-once semantics; idempotency keys prevent double-execution
- **Routing:** Worker selection is by repo (each repo has a dedicated worker pool)
- **Timeout:** Jobs have a configurable `timeout` field; executor must report before timeout or job is considered stale
- **Cancellation:** Control plane can send a `cancel` message to a worker; worker must acknowledge and stop

---

## Failure Handling

| Failure | Detection | Response |
|---|---|---|
| AO worker dies mid-job | Recovery loop (missing heartbeat) | Re-enqueue job, increment attempt |
| MCP Mail delivery fails | Dispatcher (retry exhaustion) | Exponential backoff, then fail job |
| Antigravity UI changes | Peekaboo selector mismatch | Alert + deadletter job; requires human fix |
| Control plane crashes | Global lock goes stale | Recovery loop refuses to act; human intervenes |
| Worktree corruption | AO worker reports error | Deadletter job; alert human |
| Peekaboo permissions revoked | Preflight check failure | Block all new jobs; alert via Slack |
| Duplicate job submitted | Idempotency key check | Reject silently (return existing job_id) |

---

## Crash Recovery

When the control plane restarts after a crash:

1. **Acquire global lock** — if stale, break it and proceed
2. **Scan job store** for `running` jobs — these are orphaned (crashed workers); also scan `dispatched` jobs for those where the worker died before reporting back (orphaned dispatches)
3. **For each orphaned job**: if `attempts < max_attempts`, re-enqueue; else deadletter
4. **Resume dispatcher loop** — continue polling for queued jobs
5. **Resume recovery loop** — continue stale detection

**Guarantee:** No job is lost. Either it completes, deadletters, or is re-enqueued.

---

## Launchd Integration

The control plane runs as a LaunchAgent (consistent with existing smartclaw patterns):

```
launchd/
  ai.smartclaw.antig-control-plane.plist   ← NEW
```

Config:
- `KeepAlive`: false (singleton — if it crashes, LaunchControl can restart)
- `RunAtLoad`: false (started manually or by orchestrator)
- `StandardOutPath`: `@HOME@/.smartclaw/logs/antig-control-plane.log` (uses install-time placeholder, consistent with existing `launchd/` plist pattern in this repo)
- `StandardErrorPath`: `@HOME@/.smartclaw/logs/antig-control-plane.err.log`
