# Evidence: PR #417 mem0-purge smoke tests

## Claim → Artifact Map

| Claim | File | Key Field(s) |
|-------|------|--------------|
| Shellcheck clean | artifacts/smoke_test_output.txt | test_shellcheck_clean PASSED |
| Dry-run default exits 0 | artifacts/smoke_test_output.txt | test_help_flag_exits_zero PASSED |
| --verify-only exits 0 (Qdrant skip) | artifacts/smoke_test_output.txt | test_verify_only_exits_zero SKIPPED |
| Unknown options rejected | artifacts/smoke_test_output.txt | test_dry_run_unknown_option_fails PASSED |
| Missing IDs file fails | artifacts/smoke_test_output.txt | test_missing_ids_file_fails PASSED |
| Empty IDs file fails | artifacts/smoke_test_output.txt | test_empty_ids_file_fails PASSED |
| Invalid UUIDs skipped | artifacts/smoke_test_output.txt | test_invalid_uuid_skipped PASSED |
| Valid UUIDs accepted | artifacts/smoke_test_output.txt | test_valid_uuid_accepted_no_error PASSED |
| Inline invalid IDs rejected | artifacts/smoke_test_output.txt | test_inline_ids_invalid_rejected PASSED |
| --confirm without IDs fails | artifacts/smoke_test_output.txt | test_confirm_without_ids_file_fails PASSED |
| --confirm requires guards | artifacts/smoke_test_output.txt | test_confirm_alone_requires_at_least_one_guard PASSED |
| Duplicate IDs rejected | artifacts/smoke_test_output.txt | test_duplicate_ids_rejected PASSED |
| Dry-run prints preview | artifacts/smoke_test_output.txt | test_dry_run_default_prints_preview PASSED |
| Hash deterministic | artifacts/smoke_test_output.txt | test_hash_is_deterministic PASSED |
| Hash changes on different IDs | artifacts/smoke_test_output.txt | test_hash_changes_on_different_ids PASSED |
| Count mismatch fails | artifacts/smoke_test_output.txt | test_confirm_count_mismatch_fails PASSED |
| Hash mismatch fails | artifacts/smoke_test_output.txt | test_confirm_hash_mismatch_fails PASSED |
| Live mode blocked | artifacts/smoke_test_output.txt | test_confirm_flag_blocks_dry_run PASSED |
| Benjamin hash verification | artifacts/smoke_test_output.txt | test_known_benjamin_hash_verification PASSED |

## Test results: 18 passed, 1 skipped

```
tests/test_mem0_purge_smoke.py::TestMem0PurgeShellcheck::test_shellcheck_clean PASSED
tests/test_mem0_purge_smoke.py::TestMem0PurgeDryRunParsing::test_help_flag_exits_zero PASSED
tests/test_mem0_purge_smoke.py::TestMem0PurgeDryRunParsing::test_verify_only_exits_zero SKIPPED
tests/test_mem0_purge_smoke.py::TestMem0PurgeDryRunParsing::test_dry_run_unknown_option_fails PASSED
tests/test_mem0_purge_smoke.py::TestMem0PurgeDryRunParsing::test_missing_ids_file_fails PASSED
tests/test_mem0_purge_smoke.py::TestMem0PurgeDryRunParsing::test_empty_ids_file_fails PASSED
tests/test_mem0_purge_smoke.py::TestMem0PurgeDryRunParsing::test_invalid_uuid_skipped PASSED
tests/test_mem0_purge_smoke.py::TestMem0PurgeDryRunParsing::test_valid_uuid_accepted_no_error PASSED
tests/test_mem0_purge_smoke.py::TestMem0PurgeDryRunParsing::test_inline_ids_invalid_rejected PASSED
tests/test_mem0_purge_smoke.py::TestMem0PurgeDryRunParsing::test_confirm_without_ids_file_fails PASSED
tests/test_mem0_purge_smoke.py::TestMem0PurgeDryRunParsing::test_confirm_alone_requires_at_least_one_guard PASSED
tests/test_mem0_purge_smoke.py::TestMem0PurgeDryRunParsing::test_duplicate_ids_rejected PASSED
tests/test_mem0_purge_smoke.py::TestMem0PurgeDryRunParsing::test_dry_run_default_prints_preview PASSED
tests/test_mem0_purge_smoke.py::TestMem0PurgeDryRunParsing::test_confirm_flag_blocks_dry_run PASSED
tests/test_mem0_purge_smoke.py::TestMem0PurgeDryRunParsing::test_confirm_hash_mismatch_fails PASSED
tests/test_mem0_purge_smoke.py::TestMem0PurgeDryRunParsing::test_confirm_count_mismatch_fails PASSED
tests/test_mem0_purge_smoke.py::TestMem0PurgeDryRunParsing::test_hash_is_deterministic PASSED
tests/test_mem0_purge_smoke.py::TestMem0PurgeDryRunParsing::test_hash_changes_on_different_ids PASSED
tests/test_mem0_purge_smoke.py::TestMem0PurgeDryRunParsing::test_known_benjamin_hash_verification PASSED

======================== 18 passed, 1 skipped in 18.78s ========================
```

## Test categories
- **Shellcheck**: 1 test (clean SC1090 suppressions only)
- **Dry-run parsing**: 17 tests (guards, UUID validation, duplicate rejection, hash determinism, confirmation guards, preview output)
- **Integration smoke**: 1 test (`test_verify_only_exits_zero` — SKIPPED: Qdrant not reachable)

## Safety guard coverage
| Guard | Covered by test |
|-------|---------------|
| Dry-run default (no deletes) | `test_dry_run_default_prints_preview` |
| --confirm requires guards | `test_confirm_alone_requires_at_least_one_guard` |
| Hash mismatch fails | `test_confirm_hash_mismatch_fails` |
| Count mismatch fails | `test_confirm_count_mismatch_fails` |
| Duplicate IDs rejected | `test_duplicate_ids_rejected` |
| UUID validation | `test_invalid_uuid_skipped`, `test_valid_uuid_accepted_no_error` |
| Deterministic hash | `test_hash_is_deterministic`, `test_known_benjamin_hash_verification` |
| Live mode confirmation | `test_confirm_flag_blocks_dry_run` |
| shellcheck clean | `test_shellcheck_clean` |
