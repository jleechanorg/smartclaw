#!/usr/bin/env bash
# TDD tests for ao-backfill.sh bug fixes
# Run: bash tests/test_ao_backfill_poked_at.sh

set -uo pipefail

# Helpers inlined from retired scripts/agento-helpers.sh
# (The original helper file was removed as part of the agento retirement.)

write_poked_at() {
    local session_file="$1"
    local timestamp="${2:-$(date -u +%Y-%m-%dT%H:%M:%S)}"
    if grep -q "^pokedAt=" "$session_file" 2>/dev/null; then
        sed -e "s/^pokedAt=.*/pokedAt=$timestamp/" "$session_file" > "$session_file.tmp" && mv "$session_file.tmp" "$session_file" || return 1
    else
        echo "pokedAt=$timestamp" >> "$session_file" || return 1
    fi
    return 0
}

check_poke_rate_limit() {
    local session_file="$1"
    local last_poked=$(grep -E "^pokedAt=" "$session_file" 2>/dev/null | cut -d= -f2 || echo "")
    if [[ -n "$last_poked" ]]; then
        local poked_epoch
        poked_epoch=$(python3 -c "from datetime import datetime; print(int(datetime.fromisoformat('${last_poked%%.*}').timestamp()))" 2>/dev/null) || \
        poked_epoch=0
        local now_epoch=$(date +%s)
        local since_poke=$(( (now_epoch - poked_epoch) / 60 ))
        if [[ "$since_poke" -lt 60 ]]; then
            return 1  # rate limited
        fi
    fi
    return 0  # can poke
}

normalize_cr_approved() {
    local cr_approved="$1"
    [[ "$cr_approved" -gt 1 ]] && cr_approved=1
    echo "$cr_approved"
}

get_stage_from_merge_state() {
    local mergeable="$1"
    local merge_state="$2"
    local cr_approved="$3"
    if [[ "$merge_state" == "dirty" ]]; then
        echo "conflict"
    elif [[ "$merge_state" == "unstable" ]]; then
        echo "ci_failing"
    elif [[ "$mergeable" == "true" && "$cr_approved" -ge 1 ]]; then
        echo "ready"
    elif [[ "$mergeable" == "true" ]]; then
        echo "mergeable"
    else
        echo "$merge_state"
    fi
}

PASS=0
FAIL=0
TMPDIR_TEST=$(mktemp -d)
trap 'rm -rf "$TMPDIR_TEST"' EXIT

pass() { echo "  PASS: $1"; ((PASS++)); }
fail() { echo "  FAIL: $1"; ((FAIL++)); }

# ── Bug 1: pokedAt write on first poke (sed fallback never runs) ──────────────

test_poked_at_first_write_buggy() {
    local session_file="$TMPDIR_TEST/session_buggy.txt"
    echo "sessionId=abc123" > "$session_file"
    echo "pr=42" >> "$session_file"

    # Buggy code: sed exits 0 even on no-match → || never runs
    # Use portable sed: write to temp file and mv (works on BSD and GNU)
    sed -e "s/^pokedAt=.*/pokedAt=$(date -u +%Y-%m-%dT%H:%M:%S)/" "$session_file" > "$session_file.tmp" 2>/dev/null && mv "$session_file.tmp" "$session_file" \
        || echo "pokedAt=$(date -u +%Y-%m-%dT%H:%M:%S)" >> "$session_file"

    if grep -q "^pokedAt=" "$session_file"; then
        fail "Bug 1 (first write): expected pokedAt to NOT be written with buggy code, but it was — test environment may differ"
    else
        pass "Bug 1 (first write): confirmed bug — pokedAt not written on first poke with sed fallback"
    fi
}

test_poked_at_first_write_fixed() {
    local session_file="$TMPDIR_TEST/session_fixed.txt"
    echo "sessionId=abc123" > "$session_file"
    echo "pr=42" >> "$session_file"

    # Fixed code using helper (same as production)
    write_poked_at "$session_file"

    if grep -q "^pokedAt=" "$session_file"; then
        pass "Bug 1 (first write fixed): pokedAt written on first poke"
    else
        fail "Bug 1 (first write fixed): pokedAt still not written"
    fi
}

test_poked_at_update_fixed() {
    local session_file="$TMPDIR_TEST/session_update.txt"
    echo "sessionId=abc123" > "$session_file"
    echo "pr=42" >> "$session_file"
    echo "pokedAt=2020-01-01T00:00:00" >> "$session_file"

    # Fixed code using helper (same as production)
    write_poked_at "$session_file" "2025-06-01T12:00:00"

    local count
    count=$(grep -c "^pokedAt=" "$session_file")
    if [[ "$count" -eq 1 ]]; then
        pass "Bug 1 (update): pokedAt updated in-place (no duplicate lines)"
    else
        fail "Bug 1 (update): expected 1 pokedAt line, got $count"
    fi

    local val
    val=$(grep "^pokedAt=" "$session_file" | cut -d= -f2)
    if [[ "$val" == "2025-06-01T12:00:00" ]]; then
        pass "Bug 1 (update value): pokedAt value is updated correctly"
    else
        fail "Bug 1 (update value): expected 2025-06-01T12:00:00, got $val"
    fi
}

# Test rate limit helper
test_poke_rate_limit_first_time() {
    local session_file="$TMPDIR_TEST/session_no_poke.txt"
    echo "sessionId=abc123" > "$session_file"
    echo "pr=42" >> "$session_file"
    
    if check_poke_rate_limit "$session_file"; then
        pass "Bug 1 (rate limit): first poke allowed (no existing pokedAt)"
    else
        fail "Bug 1 (rate limit): first poke should be allowed"
    fi
}

test_poke_rate_limit_within_60min() {
    local session_file="$TMPDIR_TEST/session_recent_poke.txt"
    echo "sessionId=abc123" > "$session_file"
    echo "pr=42" >> "$session_file"
    # Set pokedAt to 30 minutes ago (portable across GNU/BSD date)
    local recent_time
    if date -v-30M +%Y-%m-%dT%H:%M:%S >/dev/null 2>&1; then
        # macOS/BSD
        recent_time=$(date -u -v-30M +%Y-%m-%dT%H:%M:%S)
    else
        # GNU (Linux)
        recent_time=$(date -u -d "30 minutes ago" +%Y-%m-%dT%H:%M:%S)
    fi
    echo "pokedAt=$recent_time" >> "$session_file"
    
    if check_poke_rate_limit "$session_file"; then
        fail "Bug 1 (rate limit): poke within 30min should be blocked"
    else
        pass "Bug 1 (rate limit): poke within 30min correctly blocked"
    fi
}

test_poke_rate_limit_after_60min() {
    local session_file="$TMPDIR_TEST/session_old_poke.txt"
    echo "sessionId=abc123" > "$session_file"
    echo "pr=42" >> "$session_file"
    # Set pokedAt to 90 minutes ago (portable across GNU/BSD date)
    local old_time
    if date -v-90M +%Y-%m-%dT%H:%M:%S >/dev/null 2>&1; then
        # macOS/BSD
        old_time=$(date -v-90M +%Y-%m-%dT%H:%M:%S)
    else
        # GNU (Linux)
        old_time=$(date -d "90 minutes ago" +%Y-%m-%dT%H:%M:%S)
    fi
    echo "pokedAt=$old_time" >> "$session_file"
    
    if check_poke_rate_limit "$session_file"; then
        pass "Bug 1 (rate limit): poke after 90min correctly allowed"
    else
        fail "Bug 1 (rate limit): poke after 90min should be allowed"
    fi
}

# ── Bug 2: cr_approved comparison (== "1" fails for count > 1) ────────────────

test_cr_approved_count_one() {
    local cr_approved=1
    # Fixed check using helper
    local normalized
    normalized=$(normalize_cr_approved "$cr_approved")
    if [[ "$normalized" -ge 1 ]]; then
        pass "Bug 2 (count=1): helper correctly normalizes 1 to 1"
    else
        fail "Bug 2 (count=1): helper should normalize 1 to >=1"
    fi
}

test_cr_approved_count_two() {
    local cr_approved=2
    # Fixed check using helper - now handles >1 correctly
    local normalized
    normalized=$(normalize_cr_approved "$cr_approved")
    if [[ "$normalized" -ge 1 ]]; then
        pass "Bug 2 (count=2): helper correctly normalizes 2 to 1 and passes -ge 1"
    else
        fail "Bug 2 (count=2): helper should normalize 2 to >=1"
    fi
}

# ── Bug 3: unstable vs dirty distinction ─────────────────────────────────────

test_unstable_not_conflict() {
    local stage
    stage=$(get_stage_from_merge_state "true" "unstable" 1)
    if [[ "$stage" == "ci_failing" ]]; then
        pass "Bug 3 (unstable): correctly labeled as ci_failing"
    else
        fail "Bug 3 (unstable): expected ci_failing, got '$stage'"
    fi
}

test_dirty_is_conflict() {
    local stage
    stage=$(get_stage_from_merge_state "false" "dirty" 0)
    if [[ "$stage" == "conflict" ]]; then
        pass "Bug 3 (dirty): correctly labeled as conflict"
    else
        fail "Bug 3 (dirty): expected conflict, got '$stage'"
    fi
}

test_mergeable_ready() {
    local stage
    stage=$(get_stage_from_merge_state "true" "MERGEABLE" 1)
    if [[ "$stage" == "ready" ]]; then
        pass "Bug 3 (ready): mergeable + CR approved = ready"
    else
        fail "Bug 3 (ready): expected ready, got '$stage'"
    fi
}

test_mergeable_needs_cr() {
    local stage
    stage=$(get_stage_from_merge_state "true" "MERGEABLE" 0)
    if [[ "$stage" == "mergeable" ]]; then
        pass "Bug 3 (needs cr): mergeable but no CR = mergeable (needs CR)"
    else
        fail "Bug 3 (needs cr): expected mergeable, got '$stage'"
    fi
}

# ── Run all tests ─────────────────────────────────────────────────────────────

echo "=== Bug 1: pokedAt first-write (sed fallback) ==="
test_poked_at_first_write_buggy
test_poked_at_first_write_fixed
test_poked_at_update_fixed
test_poke_rate_limit_first_time
test_poke_rate_limit_within_60min
test_poke_rate_limit_after_60min

echo ""
echo "=== Bug 2: cr_approved count > 1 ==="
test_cr_approved_count_one
test_cr_approved_count_two

echo ""
echo "=== Bug 3: unstable vs dirty ==="
test_unstable_not_conflict
test_dirty_is_conflict
test_mergeable_ready
test_mergeable_needs_cr

echo ""
echo "Results: ${PASS} passed, ${FAIL} failed"
[[ "$FAIL" -eq 0 ]]
