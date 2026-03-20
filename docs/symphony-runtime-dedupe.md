# Symphony Runtime Dedupe Contract

## Retained Local Extensions

- Plugin input shaping remains local in `scripts/prepare-symphony-payload.py` and `src/orchestration/symphony_plugins.py`.
- Benchmark catalogs remain local in `openclaw-config/symphony/leetcode_hard_5.json` and `openclaw-config/symphony/swe_bench_verified_5.json`.
- `scripts/sym-dispatch.sh` remains the stable local wrapper for freeform text and plugin payload dispatch.

These are retained because they are repository-specific curation layers, not Symphony runtime primitives.

## Explicit Non-Goals

- No local generation of runtime `WORKFLOW.md` at daemon setup time.
- No local policy expansion in the daemon bootstrap path (plugin parsing, issue seeding, or workflow materialization).
- No default production use of `memory_tracker_issues` RPC enqueue for non-benchmark dispatch.

## Rollback Plan

1. Re-enable previous enqueue behavior by setting `SYMPHONY_MEMORY_QUEUE_MODE=always`.
2. Point daemon setup to a prior workflow file using `SYMPHONY_WORKFLOW_PATH=<path>`.
3. If needed, restore earlier script behavior by reverting this dedupe branch and rerunning `scripts/install-symphony-daemon.sh`.
