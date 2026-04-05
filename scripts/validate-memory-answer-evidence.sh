#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 <artifact-json>"
  exit 2
fi

artifact="$1"
if [[ ! -f "$artifact" ]]; then
  echo "ERROR: artifact not found: $artifact"
  exit 2
fi

if ! command -v jq >/dev/null 2>&1; then
  echo "ERROR: jq is required"
  exit 2
fi

if ! command -v rg >/dev/null 2>&1; then
  echo "ERROR: rg is required"
  exit 2
fi

repo_root="$(git rev-parse --show-toplevel)"
openclaw_workspace="${HOME}/.openclaw/workspace"
tmp_text="$(mktemp)"
tmp_json="$(mktemp)"
trap 'rm -f "$tmp_text" "$tmp_json"' EXIT

# Some artifacts include log prefix lines before the JSON payload.
awk 'f || /^\s*{/ {f=1; print}' "$artifact" >"$tmp_json"
if ! jq -e '.' "$tmp_json" >/dev/null 2>&1; then
  echo "ERROR: could not parse JSON payload in $artifact"
  exit 2
fi

jq -r '(.payloads[]?.text // empty), (.result.payloads[]?.text // empty)' "$tmp_json" >"$tmp_text"
if [[ ! -s "$tmp_text" ]]; then
  echo "ERROR: no payload text found in $artifact"
  exit 2
fi

declare -a missing_files=()
declare -a missing_orch=()
declare -a bad_shas=()

while IFS= read -r p; do
  [[ -z "$p" ]] && continue
  p="$(printf '%s' "$p" | sed -E 's/[),;:]+$//')"
  p="${p%%#*}"
  [[ -z "$p" ]] && continue
  if [[ "$p" == workspace/* ]]; then
    mapped="${openclaw_workspace}/${p#workspace/}"
    [[ -e "$mapped" ]] || missing_files+=("$p")
  elif [[ "$p" == /* ]]; then
    [[ -e "$p" ]] || missing_files+=("$p")
  else
    [[ -e "$repo_root/$p" ]] || missing_files+=("$p")
  fi
done < <(
  rg -o --no-filename '`[^`]+`' "$tmp_text" \
    | sed 's/^`//; s/`$//' \
    | rg '(^/|^\./|^\.\./|^[A-Za-z0-9._-]+/.*\.(md|json|yaml|yml|ts|tsx|js|py|sh|txt)$|^[A-Za-z0-9._-]+\.(md|json|yaml|yml|ts|tsx|js|py|sh|txt)$)' \
    | sort -u
)

while IFS= read -r id; do
  [[ -z "$id" ]] && continue
  if ! rg -uuu -n --fixed-strings "$id" "$repo_root" >/dev/null; then
    missing_orch+=("$id")
  fi
done < <(rg -o --no-filename 'ORCH-[A-Za-z0-9.-]+' "$tmp_text" | sort -u)

while IFS= read -r sha; do
  [[ -z "$sha" ]] && continue
  if ! git -C "$repo_root" cat-file -e "${sha}^{commit}" 2>/dev/null; then
    bad_shas+=("$sha")
  fi
done < <(rg -o --no-filename '\b[0-9a-f]{7,40}\b' "$tmp_text" | sort -u)

status=0
echo "Evidence validation report: $artifact"

if ((${#missing_files[@]} > 0)); then
  status=1
  echo
  echo "FAIL: cited paths not found"
  printf ' - %s\n' "${missing_files[@]}"
fi

if ((${#missing_orch[@]} > 0)); then
  status=1
  echo
  echo "FAIL: cited ORCH IDs not found in workspace"
  printf ' - %s\n' "${missing_orch[@]}"
fi

if ((${#bad_shas[@]} > 0)); then
  status=1
  echo
  echo "FAIL: cited commit SHAs not found"
  printf ' - %s\n' "${bad_shas[@]}"
fi

if [[ $status -eq 0 ]]; then
  echo "PASS: all cited paths/ORCH IDs/commit SHAs are locally verifiable."
else
  echo
  echo "Result: FAIL (unsupported citations detected)"
fi

exit "$status"
