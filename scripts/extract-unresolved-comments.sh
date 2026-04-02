#!/usr/bin/env bash
# extract-unresolved-comments.sh — Fetch unresolved CR review threads, sorted by severity.
# Usage: extract-unresolved-comments.sh <owner/repo> <pr_number>
# Output: One comment per line: "[SEVERITY] path:line — snippet"
#         Exits 0 with no output if all threads are resolved.

set -euo pipefail

usage() {
  echo "Usage: $0 <owner/repo> <pr_number>" >&2
  exit 1
}

[[ $# -eq 2 ]] || usage
OWNER_REPO="$1"
PR="$2"

OWNER="${OWNER_REPO%%/*}"
REPO="${OWNER_REPO#*/}"

# Use GraphQL (not REST /pulls/{}/comments) to get proper thread resolution state.
# REST endpoint /comments has no isResolved field — thread resolution is only
# available at the GraphQL reviewThreads level. The in_reply_to_id == null
# heuristic only identifies top-level comments, not thread resolution status.

GQL_QUERY="{
  repository(owner: \"$OWNER\", name: \"$REPO\") {
    pullRequest(number: $PR) {
      reviewThreads(first: 100) {
        nodes {
          isResolved
          comments(first: 10) {
            nodes {
              author { login }
              body
              path
              line
              createdAt
            }
          }
        }
      }
    }
  }
}"

DATA=$(gh api graphql --raw-field query="$GQL_QUERY" \
  --jq '{
    threads: .data.repository.pullRequest.reviewThreads.nodes |
    map(select(.isResolved == false)) |
    [.[] | .comments.nodes[] | select(.author.login == "coderabbitai[bot]")] |
    map({
      severity: (if .body | test("Critical"; "i") then "CRITICAL"
                elif .body | test("Major"; "i") then "MAJOR"
                elif .body | test("Minor|Nitpick"; "i") then "NIT"
                else "NORMAL" end),
      path: .path,
      line: .line,
      body: (.body | split("\n")[0] | .[0:120]),
      created_at: .createdAt
    }) |
    sort_by(if .severity == "CRITICAL" then 0 elif .severity == "MAJOR" then 1 elif .severity == "NORMAL" then 2 else 3 end)
  }')

TOTAL=$(echo "$DATA" | jq '.threads | length')

if [[ "$TOTAL" -eq 0 ]] || [[ "$TOTAL" == "null" ]]; then
  # Print to stderr so programmatic consumers don't misinterpret as a comment entry.
  echo "No unresolved CR review threads found." >&2
  exit 0
fi

echo "$DATA" | jq -r '.threads[] | "[\(.severity)] \(.path)\(if .line then ":" + (.line | tostring) else "" end) — \(.body)"'
