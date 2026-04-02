# Antigravity Control Plane — Architecture Decision Records

**Bead:** `ORCH-ag1`
**Status:** Design in progress

---

## ADR-1: Language — Python (not TypeScript)

**Decision:** The control plane is implemented in Python, not TypeScript.

**Context:** smartclaw's orchestration layer is Python-first (`src/orchestration/`). TypeScript is used in `agent-orchestrator` (AO core), not in the harness. Python aligns with existing patterns: dataclasses, SQLite, pytest, and existing Slack integration (`src/orchestration/slack_util.py`).

**Alternatives considered:**
- **TypeScript**: Consistent with AO core. But smartclaw has no TypeScript build pipeline, no `package.json`, no tsconfig. Adding these would be a large infrastructure change for the harness.
- **Go**: Fast, single binary. But less familiar to smartclaw patterns, no existing test infrastructure.

**Consequence:** The AO adapter must translate between Python control plane and TypeScript AO workers. The MCP Mail JSON contract is the language-neutral boundary.

**Review:** If AO core moves to Python or a polyglot model, revisit this decision.

---

## ADR-2: Location in Repository — `docs/` for design, `src/orchestration/` for code

**Decision:** Design artifacts go in `docs/antigravity-control-plane/`. Runtime code goes in `src/orchestration/antig_control_plane/`.

**Context:** smartclaw uses `docs/` for design documents and `src/orchestration/` for Python runtime code. This is already established.

**Alternatives considered:**
- `roadmap/`: Overcrowded (38 files), mixed status. `docs/` is cleaner for new design initiatives.
- `src/antig_control_plane/`: Would require adding `src/` to Python path in a non-standard way. `src/orchestration/` is already in `PYTHONPATH`.

**Consequence:** Sonnet workers must know to check `docs/antigravity-control-plane/` first, then `src/orchestration/antig_control_plane/` for implementation.

**Review:** If the codebase grows significantly, consider splitting `src/orchestration/` into `src/orchestration/harness/` and `src/orchestration/antig/`.

---

## ADR-3: AO Integration Boundary — Thin Adapter via MCP Mail

**Decision:** The AO adapter is a thin translation layer. AO owns worker lifecycle; control plane owns routing and policy.

**Context:** AO already has a mature worker lifecycle (spawn, supervise, respawn). The control plane should not replicate this. MCP Mail provides a durable, asynchronous message channel.

**Alternatives considered:**
- **Control plane spawns AO workers directly**: Would require control plane to understand AO's internal process model. Too tightly coupled.
- **Control plane exposes a REST API that AO polls**: AO would need to be modified to poll. MCP Mail is already integrated.
- **Control plane embeds AO worker code**: Would create a fork of AO worker logic in smartclaw. Not maintainable.

**Consequence:** The MCP Mail message schema is the critical interface contract. It must be stable and versioned. Changes to the schema require coordinated updates to control plane and AO worker templates.

**Review:** If MCP Mail proves unreliable, revisit REST API polling as fallback.

---

## ADR-4: Singleton Controller Strategy — OS flock

**Decision:** The control plane runs as a singleton using `flock(LOCK_EX | LOCK_NB)` on a lock file. No leader election, no distributed lock service.

**Context:** smartclaw runs on a single macOS machine. The control plane coordinates Antigravity windows on that machine. There is no need for distributed coordination across multiple machines.

**Alternatives considered:**
- **Leader election via distributed lock (Redis, etcd)**: Overkill for single-machine deployment. Adds external dependency.
- **Process ID file only** (no flock): Race condition on crash. `flock` is atomic at the OS level.
- **Thread-based singleton** (Python threading.Lock): Only prevents threads within the same process, not multiple processes.

**Consequence:** If the machine runs multiple smartclaw instances (should never happen), the second instance will refuse to start with a clear error. Recovery from crash is instant — `flock` is released immediately on process exit.

**Review:** If multi-machine Antigravity orchestration is needed in the future, add distributed locking via Redis.

---

## ADR-5: Job State Storage — SQLite + JSONL

**Decision:** Jobs stored in SQLite. Event logs and outcome ledger stored in append-only JSONL files.

**Context:** smartclaw already uses SQLite + JSONL patterns (see `src/orchestration/webhook.py`). No new infrastructure needed.

**Alternatives considered:**
- **PostgreSQL**: Overkill for single-machine workload. Adds operational complexity.
- **Redis**: In-memory, could lose data on crash. Not acceptable for durable job queue.
- **Plain JSON files**: No concurrent access safety. SQLite's WAL mode handles concurrent reads + single writer.
- **SQLAlchemy ORM**: Adds dependency. Raw `sqlite3` with dataclasses is sufficient and matches existing code.

**Consequence:** SQLite WAL mode enables concurrent readers while maintaining write serialization. JSONL append-only pattern provides immutable audit log.

**Schema stability:** Job schema changes require migrations. Use SQLite's `ALTER TABLE` with care — prefer additive changes (new columns) over destructive changes.

---

## ADR-6: Per-Repo Exclusivity — Dispatcher-Level, Not Worktree-Level

**Decision:** The dispatcher enforces at most one `running` or `dispatched` job per repo. Worktree lock files are not the primary mechanism.

**Context:** Two workers could try to operate on the same worktree if both are dispatched for the same repo. The dispatcher prevents this by checking `running`/`dispatched` state before dispatching.

**Alternatives considered:**
- **Worktree-level flock**: A lock file inside the worktree directory. But worktrees may not exist yet (control plane creates them). Also adds filesystem coupling.
- **PostgreSQL advisory locks**: Overkill for single-machine.
- **No exclusivity** (let workers fight): Antigravity Manager window is single-threaded; fighting causes chaos.

**Consequence:** A job for repo A will wait in `queued` state until the previous job for repo A reaches a terminal state. This is correct behavior — it prevents concurrent writes to the same worktree.

**Review:** If Antigravity can safely handle parallel workspaces in the same repo (e.g., different branches), the exclusivity model can be relaxed to per-branch.

---

## ADR-7: Idempotency — Dedupe at Enqueue, Not at Execute

**Decision:** Idempotency is checked at enqueue time (in the scheduler), not at execute time. Duplicate jobs within the same non-terminal state are rejected.

**Context:** The control plane is the authority on what jobs are in flight. AO workers should not need to check deduplication — they receive a job_id and report against it.

**Alternatives considered:**
- **Idempotency check in executor**: AO worker checks if job already ran before executing. Adds unnecessary complexity to workers.
- **Event sourcing**: Every action is an event; deduplication is at the event log level. Overkill for this use case.
- **No idempotency**: Jobs can be submitted multiple times and run multiple times. Unacceptable — Antigravity operations are expensive.

**Consequence:** If the same job is submitted twice before the first starts, the second gets `duplicate=True` with the first job's `job_id`. The submitter can then monitor the original job.

**Review:** If jobs need to be re-run after terminal failure (human explicitly re-queues), the idempotency key is reused intentionally — that's the correct behavior (same job, new attempt).

---

## ADR-8: No Runtime Code in v0 Design PR

**Decision:** v0 is design-only. No Python runtime code is committed in the same PR as the design.

**Context:** Design and implementation should be reviewed separately. Sonnet workers implement; the design author reviews. Mixing them makes review harder and creates ambiguity about scope.

**Alternatives considered:**
- **Design + Phase 1 skeleton in same PR**: Faster, but harder to review. If design changes, skeleton code must change too.
- **Design + all v1-v3 in same PR**: Way too large. Do not do this.

**Consequence:** This PR (v0) contains only `docs/antigravity-control-plane/*.md`. Sonnet workers open subsequent PRs for v1, v2, v3. Design changes after v0 merge require a separate PR.

**Review:** If a design flaw is discovered during Phase 1 implementation, fix the design doc in the same PR as the implementation fix. Don't let design and code diverge.

---

## ADR-9: Executor Is Stateless (No Job Store Access)

**Decision:** The executor (AO worker) reads the job payload once and reports once. It does not poll the job store or make routing decisions.

**Context:** Executors run in AO's environment, not the control plane's. Giving executors direct SQLite access would require sharing the SQLite file across machines/processes, which is fragile.

**Alternatives considered:**
- **Executor polls job store directly**: Requires shared filesystem or SQLite replication. Too complex.
- **Executor receives callbacks from control plane**: Control plane sends heartbeats to executor. Reverse direction adds complexity.

**Consequence:** The dispatcher is the only writer to the job store. Executors are pure functions: job in → Antigravity actions → result out. This makes testing easy (mock executor input/output) and AO workers simple to write.

**Review:** If heartbeat frequency becomes a bottleneck, consider switching to a callback-based model where executors push updates directly.
