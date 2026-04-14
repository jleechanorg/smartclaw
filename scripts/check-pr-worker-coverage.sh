#!/usr/bin/env bash
# check-pr-worker-coverage.sh — report which open PRs in agent-orchestrator have active AO sessions
# Exit 0 if ALL open PRs are covered; exit non-zero if ANY are uncovered.

set -uo pipefail

REPO="jleechanorg/agent-orchestrator"
PROJECT="agent-orchestrator"
LIFECYCLE_LOG="${LIFECYCLE_LOG:-$HOME/.smartclaw/logs/ao-lifecycle-${PROJECT}.log}"

latest_claim_failure_for_pr() {
  local pr_number="$1"
  python3 - "$LIFECYCLE_LOG" "$pr_number" 2>/dev/null <<'PY' || true
import json
import pathlib
import sys

log_path = pathlib.Path(sys.argv[1])
target_pr = int(sys.argv[2])
latest = ""

if not log_path.exists():
    raise SystemExit(0)

for raw in log_path.read_text(errors="ignore").splitlines():
    if "lifecycle.backfill.claim_failed" not in raw:
        continue
    try:
        payload = json.loads(raw)
    except Exception:
        continue
    if payload.get("operation") != "lifecycle.backfill.claim_failed":
        continue
    data = payload.get("data") or {}
    if data.get("prNumber") != target_pr:
        continue
    latest = " ".join(str(data.get("error", "")).split())

if latest:
    if len(latest) > 220:
        latest = latest[:217] + "..."
    print(latest)
PY
}

# --- Fetch open PRs via GitHub REST API (per_page=100 to avoid silent truncation) ---
pr_json=$(gh api "repos/$REPO/pulls?state=open&per_page=100" --jq '
  [.[] | {number, title, updatedAt}] | sort_by(.number)
' 2>/dev/null) || {
  echo "ERROR: gh api failed for $REPO" >&2
  exit 1
}

if [[ -z "$pr_json" || "$pr_json" == "[]" ]]; then
  echo "No open PRs in $REPO"
  exit 0
fi

# Pre-count PRs so the "all covered" verdict is only emitted when the loop ran
pr_count=$(echo "$pr_json" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))" 2>/dev/null)
[[ -z "$pr_count" || "$pr_count" == "0" ]] && { echo "ERROR: could not parse PR list"; exit 1; }

# --- Fetch active sessions (suppress stderr; non-zero exit if ao is unavailable) ---
sessions_raw=$(ao session ls --project "$PROJECT" 2>/dev/null) || {
  echo "ERROR: ao session ls failed for project $PROJECT" >&2
  exit 1
}

if [[ -z "$sessions_raw" ]]; then
  echo "No active sessions found for project $PROJECT"
  # All PRs are uncovered
  uncovered=$(echo "$pr_json" | python3 -c "import sys,json; [print(p['number']) for p in json.load(sys.stdin)]" 2>/dev/null)
  if [[ -n "$uncovered" ]]; then
    echo ""
    echo "UNCOVERED PRs: $(echo "$uncovered" | tr '\n' ' ')"
  fi
  exit 1
fi

# --- Filter to active sessions only (exclude killed / completed) ---
active_sessions=$(echo "$sessions_raw" | grep -vE '\[(killed|completed)\]' || true)

# --- Process each open PR ---
echo ""
printf "%-6s %-50s %-12s %s\n" "PR #" "Title" "Session" "Status"
printf "%-6s %-50s %-12s %s\n" "------" "-----" "-------" "------"

uncovered_count=0
uncovered_prs=""

while IFS= read -r pr_line; do
  [[ -z "$pr_line" ]] && continue

  pr_num=$(echo "$pr_line" | python3 -c "import sys,json; print(json.load(sys.stdin)['number'])" 2>/dev/null) || continue
  pr_title=$(echo "$pr_line" | python3 -c "import sys,json; print(json.load(sys.stdin)['title'])" 2>/dev/null) || continue

  # Truncate title to 47 chars for table alignment
  pr_title_short=$(echo "$pr_title" | cut -c1-47)
  [[ ${#pr_title} -gt 47 ]] && pr_title_short="${pr_title_short}..."

  # Find session for this PR from `ao session ls`.
  # Current output usually includes a GitHub API PR URL (`.../pulls/<num>`),
  # while older output used `pull/<num>`. Accept both shapes.
  session_line=""
  session_name=""
  session_status=""
  if echo "$active_sessions" | grep -qE "(/pulls?/)$pr_num( |$)"; then
    session_line=$(echo "$active_sessions" | grep -E "(/pulls?/)$pr_num( |$)" | head -1)
    session_name=$(echo "$session_line" | awk '{print $1}')
    # Extract status from brackets, e.g. [ci_failed] -> ci_failed
    session_status=$(echo "$session_line" | grep -oE '\[[^]]+\]' | tr -d '[]' | tr -d ' ')
    if [[ -z "$session_status" ]]; then
      session_status="active"
    fi
  fi

  if [[ -n "$session_name" ]]; then
    printf "%-6s %-50s %-12s %s\n" "#$pr_num" "$pr_title_short" "$session_name" "[$session_status]"
  else
    claim_failure=$(latest_claim_failure_for_pr "$pr_num")
    if [[ -n "$claim_failure" ]]; then
      printf "%-6s %-50s %-12s %s\n" "#$pr_num" "$pr_title_short" "—" "BLOCKED"
      echo "       blocker: $claim_failure"
    else
      printf "%-6s %-50s %-12s %s\n" "#$pr_num" "$pr_title_short" "—" "UNCOVERED"
    fi
    uncovered_prs="${uncovered_prs}#${pr_num} "
    ((uncovered_count++))
  fi
done < <(echo "$pr_json" | python3 -c "import sys,json; [print(json.dumps(p)) for p in json.load(sys.stdin)]" 2>/dev/null)

echo ""

# --- Summary ---
# uncovered_count is the authoritative count; pr_count guards against empty loop
if [[ "$uncovered_count" == "0" && "$pr_count" -gt "0" ]]; then
  echo "All PRs covered ✓"
  exit 0
else
  echo "UNCOVERED PRs: ${uncovered_prs}"
  exit 1
fi
