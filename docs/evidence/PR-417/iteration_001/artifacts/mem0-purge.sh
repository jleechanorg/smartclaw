#!/usr/bin/env bash
# === mem0-purge.sh — Safe one-off memory deletion for openclaw mem0/Qdrant store ===
#
# PURPOSE: Delete specific memory IDs from the openclaw mem0 vector store.
#          Defaults to DRY-RUN mode (--dry-run is the default).
#          Requires explicit confirmation before any live delete.
#
# USAGE:
#   Dry run (default — safe, always prints preview):
#     ./mem0-purge.sh --ids-file ./benjamin-ids.txt
#
#   Live run (requires --confirm flag):
#     ./mem0-purge.sh --ids-file ./benjamin-ids.txt --confirm
#
#   Inline IDs:
#     ./mem0-purge.sh --ids-inline "id1,id2,id3" --confirm
#
#   Verification only (no IDs needed, just check store health):
#     ./mem0-purge.sh --verify-only
#
# SAFETY MODEL:
#   1. Dry-run is the default — must pass --confirm to execute deletes.
#   2. Deletes are ID-allowlist only — no glob, no pattern, no all().
#   3. Preview is mandatory in both modes — prints all IDs + text before any action.
#   4. Confirmation requires --confirm COUNT (exact number of IDs) OR --confirm-hash SHA.
#   5. Post-run verification searches for remaining IDs and emits proof.
#
# REQUIREMENTS:
#   - Python 3 with mem0 package (pip install mem0ai)
#   - Qdrant running on localhost:6333
#   - mem0_config.py at ~/.smartclaw/.claude/hooks/ (Python path set automatically)

set -euo pipefail

# --- Config ------------------------------------------------------------------
MEM0_HOOKS_DIR="${HOME}/.smartclaw/.claude/hooks"

# Derive Qdrant URL and collection from the same mem0_config.py that mem0 uses.
# This ensures deletion and verification target the same store.
# Python is always available (checked in check_prereqs) so this is safe to call early.
_load_qdrant_config() {
  local _url _collection
  # set +e: python3 sys.exit(1) in the heredoc would otherwise trigger bash set -e
  # and abort the whole script before _rc=$? is evaluated. Wrapping allows us to
  # catch the non-zero exit and fall through to the defaults below.
  set +e
  _url=$(python3 - "${MEM0_HOOKS_DIR}" <<'PYEOF'
import sys
sys.path.insert(0, sys.argv[1])
try:
    from mem0_config import MEM0_CONFIG
    vs = MEM0_CONFIG.get("vector_store", {})
    host = vs.get("config", {}).get("host", "127.0.0.1")
    port = vs.get("config", {}).get("port", 6333)
    collection = vs.get("config", {}).get("collection_name", "openclaw_mem0")
    sys.stdout.write(f"QDRANT_URL={host}:{port}\nCOLLECTION={collection}\n")
    sys.stdout.flush()
except Exception as e:
    sys.stderr.write(f"# derive failed: {e}\n")
    sys.stderr.flush()
    sys.exit(1)
PYEOF
  )
  local _rc=$?
  set -e
  if [[ $_rc -eq 0 && -n "$_url" ]]; then
    local _parsed_url _parsed_collection
    _parsed_url=$(echo "$_url" | grep "^QDRANT_URL=" | cut -d= -f2 || echo "")
    _parsed_collection=$(echo "$_url" | grep "^COLLECTION=" | cut -d= -f2 || echo "")
    if [[ -n "$_parsed_url" && -n "$_parsed_collection" ]]; then
      QDRANT_URL="http://${_parsed_url}"
      COLLECTION="$_parsed_collection"
    fi
  fi
}

# Fallback defaults — used only if _load_qdrant_config fails
QDRANT_URL="http://127.0.0.1:6333"
COLLECTION="openclaw_mem0"

# --- CLI state ----------------------------------------------------------------
DRY_RUN="true"
MODE="dry-run"
VERIFY_ONLY="false"
IDS_FILE=""
IDS_INLINE=""
CONFIRM_COUNT=""
CONFIRM_HASH=""

# --- Helpers ----------------------------------------------------------------
log()  { echo "[$(date +%H:%M:%S)] $*" >&2; }
info() { echo "[$(date +%H:%M:%S)] INFO  $*" >&2; }
warn() { echo "[$(date +%H:%M:%S)] WARN  $*" >&2; }
die()  { echo "[$(date +%H:%M:%S)] ERROR $*" >&2; exit 1; }

usage() {
  grep "^#" "$0" | sed 's/^# //' | sed 's/^#//'
  exit 0
}

check_prereqs() {
  if ! command -v python3 &>/dev/null; then
    die "python3 not found"
  fi
  if ! curl -sf "${QDRANT_URL}/healthz" &>/dev/null; then
    warn "Qdrant health check failed — store may not be running"
  fi
  if [[ ! -d "$MEM0_HOOKS_DIR" ]]; then
    die "mem0 hooks dir not found: $MEM0_HOOKS_DIR"
  fi
}

# Verify a string is a valid UUID (hyphenated form, 8-4-4-4-12 lowercase hex).
# The regex accepts any UUID version/variant (v1–v5 and any variant).
# mem0/Qdrant use lowercase hex internally; users should normalise to lowercase.
is_uuid() {
  [[ "$1" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]]
}

# Compute SHA256 confirmation hash of a newline-separated list of UUIDs (must be sorted).
# Uses LC_ALL=C for deterministic sort across locales, and python3 hashlib (always present
# in a Python 3 environment) to avoid depending on sha256sum/shasum availability.
compute_hash() {
  LC_ALL=C sort <<<"$1" | python3 -c \
    'import sys, hashlib; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())'
}

# Parse IDs from file or inline, return newline-separated list
resolve_ids() {
  local _ids=""
  local _first="true"

  if [[ -n "$IDS_FILE" ]]; then
    if [[ ! -f "$IDS_FILE" ]]; then
      die "IDs file not found: $IDS_FILE"
    fi
    while IFS= read -r _line || [[ -n "$_line" ]]; do
      _line="${_line%%#*}"     # strip trailing comments
      _line="${_line%"${_line##*[![:space:]]}"}"  # trim trailing whitespace
      _line="${_line#"${_line%%[![:space:]]*}"}"  # trim leading whitespace
      [[ -z "$_line" ]] && continue
      if ! is_uuid "$_line"; then
        warn "Skipping invalid UUID: $_line"
        continue
      fi
      if $_first; then
        _ids="$_line"
        _first="false"
      else
        _ids="$_ids"$'\n'"$_line"
      fi
    done < "$IDS_FILE"
  fi

  if [[ -n "$IDS_INLINE" ]]; then
    IFS=',' read -ra _inline <<< "$IDS_INLINE"
    for _id in "${_inline[@]}"; do
      _id="${_id#"${_id%%[![:space:]]*}"}"
      _id="${_id%"${_id##*[![:space:]]}"}"
      [[ -z "$_id" ]] && continue
      if ! is_uuid "$_id"; then
        warn "Skipping invalid UUID: $_id"
        continue
      fi
      if $_first; then
        _ids="$_id"
        _first="false"
      else
        _ids="$_ids"$'\n'"$_id"
      fi
    done
  fi

  if [[ -z "$_ids" ]]; then
    die "No valid IDs provided (--ids-file or --ids-inline required)"
  fi

  # Reject duplicate IDs — use awk to normalise trailing newlines before counting.
  # Problem with "printf '%s' | wc -l": printf '%s' drops the trailing newline from the
  # final ID, so wc -l counts one fewer line than there are IDs whenever the list ends
  # with a valid UUID (e.g. "aaa\n" → wc -l = 0, "aaa\nbbb\n" → wc -l = 1).
  # awk always appends a newline after every record (including the last), so
  # both _total and _unique count the same way regardless of whether _ids ends with \n.
  local _raw_count _unique_count _dupes
  _raw_count=$(printf '%s' "$_ids" | awk '{print}' | wc -l | tr -d ' ')
  _unique_count=$(printf '%s' "$_ids" | awk 'seen[$0]++ == 0 {print}' | wc -l | tr -d ' ')
  if [[ "$_unique_count" != "$_raw_count" ]]; then
    _dupes=$(printf '%s' "$_ids" | awk 'seen[$0]++ > 0 {print $0}' | tr '\n' ' ')
    die "Duplicate IDs found: ${_dupes} (${_raw_count} total, ${_unique_count} unique). Deduplicate the allowlist and retry."
  fi

  printf '%s' "$_ids"
}

# Fetch memory text for a given ID via mem0 Python API
fetch_memory_text() {
  local _id="$1"
  python3 - "${_id}" "${MEM0_HOOKS_DIR}" <<'PYEOF'
import sys
sys.path.insert(0, sys.argv[2])
try:
    from mem0_config import MEM0_CONFIG
    from mem0 import Memory
    m = Memory.from_config(MEM0_CONFIG)
    r = m.get(sys.argv[1])
    print(r.get("memory", "(no text)")[:500])
except Exception as e:
    print(f"(error fetching: {e})")
    sys.exit(0)  # never fail the script
PYEOF
}

# Print preview of all candidate IDs with their memory text
preview_candidates() {
  local _id_list="$1"
  local _count
  _count=$(echo "$_id_list" | wc -l | tr -d ' ')
  info "=== PREVIEW: ${_count} candidate ID(s) ==="
  echo "" >&2

  local _hash
  _hash=$(compute_hash "$_id_list")
  echo "Confirmation hash (SHA256 of sorted IDs): ${_hash}" >&2
  echo "Use --confirm-hash ${_hash} to authorize live deletion." >&2
  echo "" >&2

  local _line_num=0
  while IFS= read -r _id; do
    ((_line_num++)) || true
    echo "  [${_line_num}] ${_id}" >&2
    local _text
    _text=$(fetch_memory_text "$_id")
    echo "       TEXT: ${_text}" >&2
    echo "" >&2
  done <<< "$_id_list"

  echo "Run with --confirm-count ${_count} --confirm-hash ${_hash} to delete." >&2
}

# Take pre-delete point count snapshot
take_snapshot() {
  info "Taking pre-delete point count snapshot..."
  curl -sf "${QDRANT_URL}/collections/${COLLECTION}" \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['points_count'])"
}

# Verify IDs are gone after deletion
verify_deletion() {
  local _id_list="$1"
  local _before_count="$2"
  local _all_gone="true"
  local _remaining=""
  local _deleted_count=0

  info "=== POST-RUN VERIFICATION ==="

  while IFS= read -r _id; do
    local _http_code
    local _payload
    _payload=$(curl -s "${QDRANT_URL}/collections/${COLLECTION}/points/${_id}" \
      -w "\n%{http_code}" -o - 2>/dev/null)
    _http_code=$(echo "$_payload" | tail -1)
    # Only treat 404 as "definitely gone"; 200 means still present;
    # anything else (5xx, network, etc.) is an error — do NOT claim deleted
    if [[ "$_http_code" == "200" ]]; then
      local _result
      _result=$(echo "$_payload" | sed '$d' | python3 -c "import sys,json; print('yes' if json.load(sys.stdin).get('result') else 'no')" 2>/dev/null)
      local _py_rc=$?
      if [[ $_py_rc -ne 0 ]] || [[ -z "$_result" ]]; then
        # Python parse failed (malformed JSON) — treat as unknown, not deleted
        warn "  UNKNOWN: ${_id} — could not parse Qdrant response (HTTP 200, parse error)"
        _all_gone="false"
        _remaining="${_remaining}  UNKNOWN: ${_id} (HTTP 200, parse error)"$'\n'
      elif [[ "$_result" == "yes" ]]; then
        _all_gone="false"
        _remaining="${_remaining}  STILL PRESENT: ${_id}"$'\n'
        warn "STILL PRESENT: ${_id}"
      else
        info "  DELETED: ${_id}"
        ((_deleted_count++)) || true
      fi
    elif [[ "$_http_code" == "404" ]]; then
      info "  DELETED: ${_id}"
      ((_deleted_count++)) || true
    else
      warn "  UNKNOWN: ${_id} — HTTP ${_http_code} (could not verify deletion)"
      _all_gone="false"
      _remaining="${_remaining}  UNKNOWN: ${_id} (HTTP ${_http_code})"$'\n'
    fi
  done <<< "$_id_list"

  local _after_count
  _after_count=$(curl -sf "${QDRANT_URL}/collections/${COLLECTION}" \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['points_count'])" 2>/dev/null || echo "?")

  # Guard: if Qdrant was unreachable during verification, _after_count will be "?".
  # Arithmetic $((_before_count - ?)) would crash under set -e; fail closed instead.
  if [[ "$_after_count" == "?" ]]; then
    die "Post-delete point count unavailable — Qdrant may be unreachable. Verify manually."
  fi

  info "Pre-delete points : ${_before_count}"
  info "Post-delete points: ${_after_count}"
  local _delta
  _delta=$((_before_count - _after_count))
  info "Delta             : ${_delta}"

  # Delta consistency must be verified first — _all_gone reflects per-ID HTTP checks,
  # but the point-count delta is the authoritative proof of deletion. A mismatch means
  # some deletions were not persisted even if individual HTTP calls returned OK.
  local _id_count
  _id_count=$(echo "$_id_list" | wc -l | tr -d ' ')
  if [[ "${_delta}" != "${_id_count}" ]]; then
    echo "" >&2
    warn "VERIFICATION FAIL — point delta (${_delta}) != IDs deleted (${_id_count})"
    info "  before=${_before_count}  after=${_after_count}  delta=${_delta}  ids=${_id_count}"
    info "  all_confirmed_gone=false"
    echo "" >&2
    return 1
  fi

  if [[ "$_all_gone" == "true" ]]; then
    echo "" >&2
    info "VERIFICATION PASS — all ${_deleted_count} IDs confirmed deleted."
    echo "" >&2
    info "VERIFICATION_PROOF:"
    info "  before=${_before_count}"
    info "  after=${_after_count}"
    info "  delta=${_delta}"
    info "  ids_deleted=${_id_count}"
    info "  all_confirmed_gone=true"
    echo "" >&2
    return 0
  else
    echo "" >&2
    warn "VERIFICATION FAIL — some IDs could not be confirmed deleted."
    echo "$_remaining" >&2
    return 1
  fi
}

# Perform actual deletion (one ID at a time)
do_delete() {
  local _id_list="$1"
  local _before_count="$2"
  local _deleted=0
  local _errors=0

  info "=== LIVE DELETION IN PROGRESS ==="
  local _id_count
  _id_count=$(echo "$_id_list" | wc -l | tr -d ' ')
  info "Deleting ${_id_count} ID(s)..."

  # Write the Python deletion script once (avoids stdin-stealing heredoc-in-loop issue)
  local _script
  set +e   # mktemp and cat may fail; handle gracefully with set -e guard off
  _script=$(mktemp "${TMPDIR:-/tmp}/mem0_delete_XXXXXX.py")
  local _rc=$?
  set -e
  if [[ $_rc -ne 0 ]] || [[ ! -f "$_script" ]]; then
    die "Failed to create temp script for deletion: exit $_rc"
  fi
  cat > "$_script" <<'PYEOF'
import sys
sys.path.insert(0, sys.argv[2])
_id = sys.argv[1]
try:
    from mem0_config import MEM0_CONFIG
    from mem0 import Memory
    m = Memory.from_config(MEM0_CONFIG)
    m.delete(_id)
    print(f"deleted:{_id}")
except Exception as e:
    print(f"error:{e}", file=sys.stderr)
    sys.exit(0)  # bash set -e would abort without this; exit 0 so bash error handling is reachable
PYEOF

  while IFS= read -r _id; do
    info "  Deleting ${_id}..."
    local _result
    local _rc
    _result=$(python3 "$_script" "$_id" "${MEM0_HOOKS_DIR}" 2>&1)
    _rc=$?
    if [[ $_rc -eq 0 ]] && [[ "$_result" == "deleted:${_id}" ]]; then
      info "  Deleted: ${_id}"
      ((_deleted++)) || true
    else
      warn "  FAILED: ${_id} — ${_result}"
      ((_errors++)) || true
    fi
  done <<< "$_id_list"

  rm -f "$_script"

  info "Deleted: ${_deleted}  Errors: ${_errors}"
  verify_deletion "$_id_list" "$_before_count"
}

# --- Parse arguments ---------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)       DRY_RUN="true"; MODE="dry-run"; shift ;;
    --confirm)       DRY_RUN="false"; MODE="live"; shift ;;
    --ids-file)      IDS_FILE="$2"; shift 2 ;;
    --ids-inline)    IDS_INLINE="$2"; shift 2 ;;
    --confirm-count) CONFIRM_COUNT="$2"; shift 2 ;;
    --confirm-hash)  CONFIRM_HASH="$2"; shift 2 ;;
    --verify-only)   VERIFY_ONLY="true"; shift ;;
    -h|--help)       usage ;;
    -*)              die "Unknown option: $1 (use --help)" ;;
    *)               die "Unexpected argument: $1" ;;
  esac
done

# --- Main --------------------------------------------------------------------
# Load mem0-derived Qdrant config first so --verify-only targets the right host/collection.
_load_qdrant_config

# --verify-only needs no mem0 Python prereqs; handle it after config load.
if [[ "$VERIFY_ONLY" == "true" ]]; then
  info "=== VERIFY-ONLY MODE ==="
  _total=$(curl -s "${QDRANT_URL}/collections/${COLLECTION}" 2>/dev/null \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['points_count'])" 2>/dev/null \
    || echo "unavailable")
  info "Total points in ${COLLECTION}: ${_total}"
  _health=$(curl -sf "${QDRANT_URL}/healthz" || echo 'unreachable')
  info "Qdrant health: ${_health}"
  exit 0
fi

check_prereqs

# Resolve IDs (newline-separated string)
ID_LIST=$(resolve_ids)
ID_COUNT=$(echo "$ID_LIST" | wc -l | tr -d ' ')

# Compute actual hash for confirmation check
ACTUAL_HASH=$(compute_hash "$ID_LIST")

info "=== mem0-purge.sh | mode=${MODE} | IDs=${ID_COUNT} ==="
echo "" >&2

# Preview always runs
preview_candidates "$ID_LIST"

if [[ "$DRY_RUN" == "true" ]]; then
  echo "" >&2
  info "DRY-RUN complete. No deletions performed."
  info "To run live deletion, re-run with: --ids-file <path> --confirm ..."
  exit 0
fi

# --- LIVE MODE ---
echo "" >&2
echo "=== LIVE DELETION GUARD ===" >&2

# At least one confirmation guard is required in live mode
if [[ -z "$CONFIRM_COUNT" && -z "$CONFIRM_HASH" ]]; then
  warn "Live mode requires --confirm-count and/or --confirm-hash (neither provided)"
  echo "Use: --confirm-count <N> --confirm-hash <sha256>" >&2
  die "Confirmation guards FAILED — aborting live deletion."
fi

_ok="true"

if [[ -n "$CONFIRM_COUNT" ]]; then
  if [[ "$CONFIRM_COUNT" != "$ID_COUNT" ]]; then
    warn "Count mismatch: expected ${CONFIRM_COUNT} IDs, got ${ID_COUNT}"
    _ok="false"
  else
    info "Count check: PASS (${CONFIRM_COUNT} == ${ID_COUNT})"
  fi
fi

if [[ -n "$CONFIRM_HASH" ]]; then
  if [[ "$CONFIRM_HASH" != "$ACTUAL_HASH" ]]; then
    warn "Hash mismatch: expected ${CONFIRM_HASH}, got ${ACTUAL_HASH}"
    _ok="false"
  else
    info "Hash check: PASS"
  fi
fi

if [[ "$_ok" != "true" ]]; then
  echo "" >&2
  die "Confirmation guards FAILED — aborting live deletion."
fi

echo "" >&2
warn "ALL GUARDS PASSED — proceeding with LIVE deletion of ${ID_COUNT} ID(s) in 5 seconds..."
warn "Press Ctrl+C NOW to abort." >&2
sleep 5

BEFORE=$(take_snapshot)
# Guard: if Qdrant was unreachable, take_snapshot would have died already.
# But if it somehow returned "?", arithmetic in verify_deletion would crash — defend anyway.
if [[ "$BEFORE" == "?" || -z "$BEFORE" ]]; then
  die "Pre-delete point count unavailable (got '${BEFORE}') — aborting. Verify Qdrant is reachable."
fi
do_delete "$ID_LIST" "$BEFORE"
