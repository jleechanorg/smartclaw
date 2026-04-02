# Methodology: mem0-purge smoke test evidence

## Claim
PR #417 adds a safe one-off memory deletion script (`scripts/mem0-purge.sh`) with deterministic shell-check + dry-run smoke tests (`tests/test_mem0_purge_smoke.py`).

## Evidence collection method

### Unit tests (claim class: unit_test_coverage)
- **Test runner**: pytest 9.0.2
- **Command**: `PYTHONPATH=src python -m pytest tests/test_mem0_purge_smoke.py -v --tb=short`
- **Environment**: macOS, Python 3.14.3
- **Realism**: Tests invoke the actual script via `subprocess.run(["bash", "scripts/mem0-purge.sh", ...])`
  — not mocked. They test real bash + python3 code paths.
- **Hermeticity**: `test_verify_only_exits_zero` skips when Qdrant is unreachable (no hard dependency on live Qdrant for dry-run tests). All other tests are pure parsing/guard logic with no external I/O.
- **Static analysis**: `shellcheck` runs via `shutil.which("shellcheck")`; skips if unavailable.

### Skip rationale
`test_verify_only_exits_zero` is SKIPPED because Qdrant is not running locally. This is the correct behavior — the test explicitly checks Qdrant reachability via `urllib.request.urlopen("http://127.0.0.1:6333/healthz", timeout=2)` before running. A skipped test due to missing external dependency is valid; it is not a failure.

## Files covered
| File | SHA256 (truncated) |
|------|-------------------|
| `scripts/mem0-purge.sh` | bfcee7c2df4b7d0c... |
| `tests/test_mem0_purge_smoke.py` | 17e500e351d1b76d... |
| `docs/mem0-purge-runbook.md` | c8a65eba7469aace... |
