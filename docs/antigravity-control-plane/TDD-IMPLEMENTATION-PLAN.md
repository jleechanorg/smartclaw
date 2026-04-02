# Antigravity Control Plane — TDD Implementation Plan

**Bead:** `ORCH-ag1`
**Status:** Design in progress
**Audience:** Sonnet build workers (follow-up implementation)

---

## How Sonnet Workers Should Consume This Design

1. **Read all 7 design docs first** — do not start coding until you understand the full system
2. **Start with Phase 1 tests** — write tests first, then implement the minimum to pass
3. **One phase at a time** — do not move to Phase N+1 until Phase N is green with real evidence
4. **Post evidence to PR comment** after each phase — evidence bundle with test output
5. **Coordinate via MCP Mail** — dispatch work to Sonnet workers using the dispatcher module

**Handoff checklist** (each worker marks done):
- [ ] Read OVERVIEW.md + ARCHITECTURE.md + DATA-CONTRACTS.md
- [ ] Phase 1 tests: all pass
- [ ] Phase 2 tests: all pass
- [ ] Phase 3 tests: all pass
- [ ] Phase 4 tests: all pass
- [ ] Integration smoke test: antig_control_plane can enqueue + dispatch a ping job
- [ ] LaunchAgent plist installed and verified
- [ ] PR description updated with evidence

---

## Test Naming Convention

Tests live in `src/tests/test_antig_control_plane/`. Naming:

| Test type | File pattern | What it tests |
|---|---|---|
| Unit | `test_scheduler.py` | Scheduler enqueue/dedupe in isolation |
| Unit | `test_global_lock.py` | Lock acquisition/breakage |
| Unit | `test_dispatcher.py` | Dispatch logic, per-repo exclusivity |
| Unit | `test_recovery.py` | Stale detection, re-enqueue logic |
| Unit | `test_data_contracts.py` | Schema validation, state machine transitions |
| Integration | `test_ao_adapter.py` | MCP Mail send/receive with stubbed AO |
| Integration | `test_executor.py` | Peekaboo execution loop (requires Peekaboo installed) |
| E2E | `test_smoke.py` | Full loop: enqueue → dispatch → execute → complete |

**Mock strategy:** For unit tests, mock the `AOAdapter` boundary (mock `send` and `handle_result`) and `sqlite3.connect`. For integration tests, mock Peekaboo CLI (`subprocess.run`). For E2E tests, real everything (requires Antigravity running on macOS). Note: `src/orchestration/mcp_mail.py` is retired; do not reference it.

---

## Phase 1: Foundation (models, state machine, scheduler)

**Goal:** Data contracts and scheduler work in isolation.

### Tests First

```python
# src/tests/test_antig_control_plane/test_data_contracts.py

def test_job_schema_round_trip():
    """Job dataclass serializes and deserializes correctly."""
    job = Job(
        job_id="test-uuid",
        idempotency_key="abc123",
        repo="jleechanorg/smartclaw",
        objective="Test objective",
        priority=Priority.NORMAL,
    )
    # serialize to dict, deserialize, verify equality
    serialized = asdict(job)
    restored = Job(**serialized)
    assert restored.job_id == job.job_id

def test_state_machine_valid_transition():
    """QUEUED -> DISPATCHED is a valid transition."""
    job = Job(...)
    job.state = JobState.QUEUED
    job.state = JobState.DISPATCHED  # must not raise

def test_state_machine_invalid_transition():
    """COMPLETED -> QUEUED is invalid and must raise."""
    job = Job(...)
    job.state = JobState.COMPLETED
    with pytest.raises(InvalidTransitionError):
        job.state = JobState.QUEUED

def test_idempotency_key_deterministic():
    """Same inputs always produce same idempotency key."""
    key1 = compute_idempotency_key("repo", "obj", Priority.HIGH, "corr")
    key2 = compute_idempotency_key("repo", "obj", Priority.HIGH, "corr")
    assert key1 == key2

def test_idempotency_key_different_for_different_repos():
    """Different repos produce different idempotency keys."""
    key1 = compute_idempotency_key("repo1", "obj", Priority.HIGH, None)
    key2 = compute_idempotency_key("repo2", "obj", Priority.HIGH, None)
    assert key1 != key2
```

```python
# src/tests/test_antig_control_plane/test_scheduler.py

def test_enqueue_creates_job(tmp_path, monkeypatch):
    """EnqueueRequest creates a Job in QUEUED state."""
    # Setup: mock sqlite db in tmp_path
    scheduler = Scheduler(db_path=tmp_path / "jobs.db")
    req = EnqueueRequest(
        repo="jleechanorg/smartclaw",
        objective="Test",
        priority=Priority.NORMAL,
        requestor="test",
    )
    resp = scheduler.enqueue(req)
    assert resp.state == JobState.QUEUED
    assert resp.job_id is not None
    assert resp.duplicate is False

def test_duplicate_enqueue_returns_existing(tmp_path):
    """Same idempotency key returns duplicate=True with existing job_id."""
    scheduler = Scheduler(db_path=tmp_path / "jobs.db")
    req = EnqueueRequest(
        repo="jleechanorg/smartclaw",
        objective="Test",
        priority=Priority.HIGH,
        requestor="test",
    )
    resp1 = scheduler.enqueue(req)
    resp2 = scheduler.enqueue(req)
    assert resp2.duplicate is True
    assert resp2.job_id == resp1.job_id

def test_duplicate_after_completion_creates_new(tmp_path):
    """Same idempotency key after terminal state creates a new job."""
    scheduler = Scheduler(db_path=tmp_path / "jobs.db")
    req = EnqueueRequest(repo="r", objective="o", priority=Priority.LOW, requestor="t")
    resp1 = scheduler.enqueue(req)
    scheduler.update_state(resp1.job_id, JobState.COMPLETED)
    resp2 = scheduler.enqueue(req)
    assert resp2.duplicate is False
    assert resp2.job_id != resp1.job_id
    assert resp2.state == JobState.QUEUED

def test_priority_ordering(tmp_path):
    """Higher priority jobs sort before lower priority jobs."""
    scheduler = Scheduler(db_path=tmp_path / "jobs.db")
    for p in [Priority.LOW, Priority.CRITICAL, Priority.NORMAL, Priority.HIGH]:
        scheduler.enqueue(EnqueueRequest(repo="r", objective="o", priority=p, requestor="t"))
    jobs = scheduler.get_queued_jobs()
    priorities = [j.priority for j in jobs]
    assert priorities == [Priority.CRITICAL, Priority.HIGH, Priority.NORMAL, Priority.LOW]
```

### Implementation Targets

| File | What to implement |
|---|---|
| `src/orchestration/antig_control_plane/models.py` | `Job`, `JobState`, `Priority`, `EnqueueRequest`, `EnqueueResponse`, `compute_idempotency_key`, state machine validation |
| `src/orchestration/antig_control_plane/scheduler.py` | `Scheduler` class with `enqueue`, `get_queued_jobs`, `update_state`, `get_job`, `dedupe_check` |
| `src/orchestration/antig_control_plane/db.py` | SQLite connection, schema creation, migrations |

### Acceptance Criteria

- [ ] All unit tests pass (`PYTHONPATH=src python -m pytest src/tests/test_antig_control_plane/test_data_contracts.py src/tests/test_antig_control_plane/test_scheduler.py -v`)
- [ ] State machine rejects all invalid transitions
- [ ] Idempotency dedupe works for non-terminal and terminal states
- [ ] Priority ordering is correct

### Rollback Plan

If Phase 1 implementation reveals design flaws in the state machine or data contracts:
- Update `DATA-CONTRACTS.md` with corrected schema
- Update test cases to match corrected schema
- Do not proceed to Phase 2 until schema is stable

---

## Phase 2: Dispatcher + AO Adapter (no real AO, mock MCP Mail)

**Goal:** Jobs can be dispatched via MCP Mail using a mock adapter.

### Tests First

```python
# src/tests/test_antig_control_plane/test_dispatcher.py

def test_dispatcher_polls_queued_jobs(tmp_path, monkeypatch):
    """Dispatcher picks up queued jobs and sends MCP Mail messages."""
    scheduler = Scheduler(db_path=tmp_path / "jobs.db")
    dispatcher = Dispatcher(scheduler=scheduler, ao_adapter=mock_adapter)

    job_resp = scheduler.enqueue(EnqueueRequest(
        repo="jleechanorg/smartclaw",
        objective="Test",
        priority=Priority.NORMAL,
        requestor="test",
    ))

    dispatcher.poll()  # should dispatch the job

    job = scheduler.get_job(job_resp.job_id)
    assert job.state == JobState.DISPATCHED
    assert job.dispatched_at is not None
    assert job.worker_id is not None
    assert mock_adapter.sent_messages[-1].body.repo == "jleechanorg/smartclaw"

def test_dispatcher_enforces_per_repo_exclusivity(tmp_path, mock_adapter):
    """Two jobs for same repo: first dispatched, second waits."""
    scheduler = Scheduler(db_path=tmp_path / "jobs.db")
    dispatcher = Dispatcher(scheduler=scheduler, ao_adapter=mock_adapter)

    job1_resp = scheduler.enqueue(EnqueueRequest(repo="r", objective="o1", priority=Priority.NORMAL, requestor="t"))
    job2_resp = scheduler.enqueue(EnqueueRequest(repo="r", objective="o2", priority=Priority.HIGH, requestor="t"))

    dispatcher.poll()

    job1 = scheduler.get_job(job1_resp.job_id)
    job2 = scheduler.get_job(job2_resp.job_id)

    # Per ARCHITECTURE.md: "Select highest-priority job for each repo"
    assert job2.state == JobState.DISPATCHED  # HIGH priority first
    assert job1.state == JobState.QUEUED  # NORMAL waits behind higher-priority job

def test_dispatcher_skips_running_jobs(tmp_path, mock_adapter):
    """Dispatcher does not re-dispatch a job that is already RUNNING."""
    scheduler = Scheduler(db_path=tmp_path / "jobs.db")
    dispatcher = Dispatcher(scheduler=scheduler, ao_adapter=mock_adapter)

    resp = scheduler.enqueue(EnqueueRequest(repo="r", objective="o", priority=Priority.NORMAL, requestor="t"))
    scheduler.update_state(resp.job_id, JobState.RUNNING)

    dispatcher.poll()

    job = scheduler.get_job(resp.job_id)
    assert job.state == JobState.RUNNING  # not re-dispatched
    assert mock_adapter.sent_messages == []  # no message sent

def test_ao_adapter_serializes_dispatch_message_correctly():
    """AOAdapter formats MCP Mail message per DATA-CONTRACTS spec."""
    adapter = AOAdapter()
    job = Job(job_id="j1", repo="r", objective="obj", priority=Priority.HIGH, ...)
    msg = adapter.build_dispatch_message(job)
    assert msg.subject == "antigravity_job"
    assert msg.body.job_id == "j1"
    assert msg.body.repo == "r"
    assert msg.body.attempt == 1
```

### Implementation Targets

| File | What to implement |
|---|---|
| `src/orchestration/antig_control_plane/ao_adapter.py` | `AOAdapter` class with `build_dispatch_message`, `send`, `handle_result`, `handle_heartbeat`, mock mode |
| `src/orchestration/antig_control_plane/dispatcher.py` | `Dispatcher` class with `poll`, `select_job`, `enforce_per_repo_exclusivity`, retry logic |

### Acceptance Criteria

- [ ] All dispatcher tests pass
- [ ] AO adapter produces correctly formatted MCP Mail messages
- [ ] Per-repo exclusivity is enforced
- [ ] Mock AO adapter works in test mode (no real MCP Mail needed)

### Rollback Plan

If MCP Mail format differs from spec:
- Update `ao_adapter.py` to match actual AO contract
- Update `DATA-CONTRACTS.md` to reflect the real schema
- Re-run Phase 1 if schema changes affect scheduler

---

## Phase 3: Global Lock + Recovery Loop

**Goal:** Singleton safety and crash recovery work correctly.

### Tests First

```python
# src/tests/test_antig_control_plane/test_global_lock.py

def test_acquire_lock_succeeds_when_not_held(tmp_path):
    """First process acquires lock successfully."""
    lock_path = tmp_path / "lock"
    lock = GlobalLock(lock_path)
    assert lock.acquire() is True

def test_acquire_lock_fails_when_already_held(tmp_path):
    """Second process fails to acquire held lock."""
    lock1 = GlobalLock(tmp_path / "lock")
    lock2 = GlobalLock(tmp_path / "lock")
    lock1.acquire()
    assert lock2.acquire() is False

def test_release_allows_reacquire(tmp_path):
    """Releasing lock allows another process to acquire."""
    lock1 = GlobalLock(tmp_path / "lock")
    lock2 = GlobalLock(tmp_path / "lock")
    lock1.acquire()
    lock1.release()
    assert lock2.acquire() is True

def test_stale_lock_broken_on_startup(tmp_path, monkeypatch):
    """Startup detects and breaks stale lock from dead process."""
    # Simulate stale lockfile (no live process)
    lock_path = tmp_path / "lock"
    lock_path.write_text(json.dumps({"pid": 99999, "hostname": "nonexistent"}))
    lock = GlobalLock(lock_path)
    assert lock.acquire() is True  # stale lock was broken
```

```python
# src/tests/test_antig_control_plane/test_recovery.py

def test_recovery_detects_stale_running_job(tmp_path):
    """Recovery loop finds jobs with stale heartbeats."""
    scheduler = Scheduler(db_path=tmp_path / "jobs.db")
    recovery = RecoveryLoop(scheduler=scheduler)

    resp = scheduler.enqueue(EnqueueRequest(repo="r", objective="o", priority=Priority.NORMAL, requestor="t"))
    scheduler.update_state(resp.job_id, JobState.RUNNING, worker_id="w1")
    # Manually set stale heartbeat (30+ minutes ago)
    scheduler.db.execute(
        "UPDATE jobs SET last_heartbeat_at = ? WHERE job_id = ?",
        [(datetime.utcnow() - timedelta(minutes=31)).isoformat(), resp.job_id]
    )

    stale_jobs = recovery.find_stale_jobs(max_age_minutes=30)
    assert len(stale_jobs) == 1
    assert stale_jobs[0].job_id == resp.job_id

def test_recovery_reenqueues_within_attempt_limit(tmp_path):
    """Stale job within max_attempts is re-enqueued."""
    scheduler = Scheduler(db_path=tmp_path / "jobs.db")
    recovery = RecoveryLoop(scheduler=scheduler)

    resp = scheduler.enqueue(EnqueueRequest(
        repo="r", objective="o", priority=Priority.NORMAL, requestor="t", max_attempts=3
    ))
    scheduler.update_state(resp.job_id, JobState.RUNNING, worker_id="w1")
    scheduler.set_stale_heartbeat(resp.job_id, minutes_ago=31)

    recovery.run()  # should re-enqueue

    job = scheduler.get_job(resp.job_id)
    assert job.state == JobState.QUEUED
    assert job.attempt == 2  # attempt incremented

def test_recovery_deadletters_after_max_attempts(tmp_path):
    """Stale job at max_attempts is deadlettered."""
    scheduler = Scheduler(db_path=tmp_path / "jobs.db")
    recovery = RecoveryLoop(scheduler=scheduler)

    resp = scheduler.enqueue(EnqueueRequest(
        repo="r", objective="o", priority=Priority.NORMAL, requestor="t", max_attempts=3
    ))
    scheduler.update_state(resp.job_id, JobState.RUNNING, worker_id="w1", attempt=3)
    scheduler.set_stale_heartbeat(resp.job_id, minutes_ago=31)

    recovery.run()  # should deadletter

    job = scheduler.get_job(resp.job_id)
    assert job.state == JobState.DEADLETTERED
```

### Implementation Targets

| File | What to implement |
|---|---|
| `src/orchestration/antig_control_plane/global_lock.py` | `GlobalLock` class with `acquire`, `release`, `heartbeat`, stale detection, context manager |
| `src/orchestration/antig_control_plane/recovery_loop.py` | `RecoveryLoop` class with `find_stale_jobs`, `run`, ` reenqueue`, `deadletter` |

### Acceptance Criteria

- [ ] All global lock tests pass
- [ ] All recovery loop tests pass
- [ ] Stale lock is broken within 1 second of startup
- [ ] Jobs are correctly re-enqueued or deadlettered based on attempt count

---

## Phase 4: Reporter + Outcome Ledger

**Goal:** Job outcomes are recorded and Slack notifications are sent.

### Tests First

```python
# src/tests/test_antig_control_plane/test_reporter.py

def test_reporter_records_completed_job(tmp_path):
    """Completed job is recorded in outcome ledger."""
    reporter = Reporter(outcome_ledger_path=tmp_path / "ledger.jsonl")
    job = Job(job_id="j1", repo="r", objective="o", state=JobState.COMPLETED,
              attempt=1, completed_at=datetime.utcnow(), ...)
    reporter.record(job)
    records = list(reporter.iter_records())
    assert len(records) == 1
    assert records[0].job_id == "j1"

def test_reporter_records_deadlettered_job(tmp_path, monkeypatch):
    """Deadlettered job triggers alert (mocked Slack)."""
    slack_messages = []
    def mock_send(message, channel):
        slack_messages.append({"message": message, "channel": channel})
        return True
    monkeypatch.setattr("src.orchestration.slack_util.CurlSlackNotifier.send_dm", mock_send)
    reporter = Reporter(outcome_ledger_path=tmp_path / "ledger.jsonl", slack_channel="#alerts")
    job = Job(job_id="j1", repo="r", objective="o", state=JobState.DEADLETTERED,
              attempt=3, error="Peekaboo permissions revoked", ...)
    reporter.record(job)
    assert len(slack_messages) == 1
    assert "deadlettered" in slack_messages[0]["message"].lower()
```

### Implementation Targets

| File | What to implement |
|---|---|
| `src/orchestration/antig_control_plane/reporter.py` | `Reporter` class with `record`, `iter_records`, `notify_slack` |
| Slack integration using `CurlSlackNotifier` (curl-based, existing pattern in `src/orchestration/slack_util.py`; do NOT add `slack_sdk` as a dependency) |

### Acceptance Criteria

- [ ] All reporter tests pass
- [ ] Outcome ledger is append-only
- [ ] Slack notification sent for `deadlettered` and `failed` jobs

---

## Integration Smoke Test (Post Phase 4)

**This test requires real SQLite and mocked AO.**

```python
# src/tests/test_antig_control_plane/test_smoke.py

def test_full_loop_enqueue_to_dispatch(tmp_path, mock_ao_adapter):
    """Enqueue → poll → dispatch without executing Antigravity."""
    db_path = tmp_path / "jobs.db"
    scheduler = Scheduler(db_path=db_path)
    dispatcher = Dispatcher(scheduler=scheduler, ao_adapter=mock_ao_adapter)

    # Enqueue
    resp = scheduler.enqueue(EnqueueRequest(
        repo="jleechanorg/smartclaw",
        objective="Ping: confirm Antigravity control plane is working",
        priority=Priority.CRITICAL,
        requestor="smoke-test",
        metadata={"smoke_test": True},
    ))
    assert resp.state == JobState.QUEUED

    # Dispatch
    dispatcher.poll()

    # Verify
    job = scheduler.get_job(resp.job_id)
    assert job.state == JobState.DISPATCHED
    assert job.worker_id is not None
    assert mock_ao_adapter.sent_messages[-1].body.objective == "Ping: confirm..."
```

---

## CLI / API Boundaries

```bash
# Enqueue a job (CLI)
PYTHONPATH=src python -m orchestration.antig_control_plane.cli enqueue \
    --repo jleechanorg/smartclaw \
    --objective "Run antigravity smoke test" \
    --priority high \
    --requestor "cli:jleechan"

# List jobs
PYTHONPATH=src python -m orchestration.antig_control_plane.cli list \
    --state queued \
    --repo jleechanorg/smartclaw

# Cancel a job
PYTHONPATH=src python -m orchestration.antig_control_plane.cli cancel <job_id>

# Start control plane daemon
PYTHONPATH=src python -m orchestration.antig_control_plane.daemon
```

---

## Mock Strategy for Antigravity UI Calls

For Phase 2-4 unit/integration tests (no real Antigravity):

```python
# src/tests/test_antig_control_plane/fixtures.py

class MockAOAdapter(AOAdapter):
    """AO adapter that doesn't send real MCP Mail."""
    def __init__(self):
        self.sent_messages: list[DispatchMessage] = []
        self.results: list[ResultMessage] = []

    def send(self, msg: DispatchMessage) -> None:
        self.sent_messages.append(msg)
        # Simulate immediate ack: worker starts and completes immediately
        # In real tests, you'd use a thread to simulate async completion
        self._simulate_completion(msg)

    def _simulate_completion(self, msg: DispatchMessage):
        """For smoke tests only: simulates a successful job completion."""
        # NOT called in unit tests; called only in integration smoke tests
        pass
```

For executor tests (optional hardening phase, real Antigravity):

For executor tests (real Antigravity, optional hardening phase):
- Use `peekaboo screenshot` to capture real Antigravity Manager window
- Use `subprocess.run(["peekaboo", ...], capture_output=True)` with real Peekaboo installed
- These tests should be marked `@pytest.mark.integration` and skipped in unit runs

---

## Acceptance Criteria Summary

| Phase | Criterion |
|---|---|
| Phase 1 | All 5 state machine tests pass; all 4 scheduler tests pass |
| Phase 2 | All 4 dispatcher tests pass; AO adapter produces valid MCP Mail messages |
| Phase 3 | All 4 global lock tests pass; all 3 recovery tests pass |
| Phase 4 | All 2 reporter tests pass; deadletter → Slack alert verified |
| Smoke | Full enqueue → dispatch loop works with mock AO adapter |
| LaunchAgent | `ai.smartclaw.antig-control-plane.plist` loads and keeps process alive |
