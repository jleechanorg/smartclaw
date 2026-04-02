# Evidence Bundle: PR-417 / mem0-purge smoke tests

## Package manifest

| Field | Value |
|-------|-------|
| run_id | mem0-purge-smoke-tests-001-20260328T123000 |
| iteration | 001 |
| bundle_version | 1.3.0 |
| PR | #417 |
| branch | feat/orch-yj1 |
| head_sha | 7b6a580debb8e47dbc83c79d7ca0f369a004103e |
| claim_class | unit_test_coverage |
| reviewer | claude-sonnet-4-6 |
| collected_at | 2026-03-28T12:30:00Z |

## Test results: 18 passed, 1 skipped

See `artifacts/smoke_test_output.txt` for full pytest output.
See `run.json` for structured test results.

## SHA-256 verification

checksums.sha256 uses local basenames (run from bundle directory):
```bash
cd docs/evidence/PR-417/iteration_001
sha256sum -c checksums.sha256
```

## Bundle contents

```
iteration_001/
├── README.md                  # This manifest
├── metadata.json              # Machine-readable provenance
├── run.json                   # Structured test results
├── methodology.md             # Evidence collection methodology
├── evidence.md               # Claims and claim→artifact map
├── checksums.sha256          # SHA-256 checksums (local basenames — run from this dir)
├── artifacts/
│   └── smoke_test_output.txt # Full pytest output
└── verification_report.json  # Evidence reviewer report
```

## What this bundle proves

The `scripts/mem0-purge.sh` script and `tests/test_mem0_purge_smoke.py` smoke test suite
were tested and found to pass 18/19 tests (1 skip: Qdrant not reachable locally).
Tests cover all safety-critical paths: UUID validation, duplicate rejection,
confirmation guards, hash determinism, dry-run correctness, and shellcheck clean.
