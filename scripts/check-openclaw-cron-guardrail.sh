#!/usr/bin/env bash
set -euo pipefail

# Guardrail:
# - Forbidden: system crontab usage for OpenClaw reminder/scheduling/automation jobs.
# - Forbidden: OpenClaw in-app cron workflow (`openclaw cron ...`) for repo-managed recurring jobs.
# - Required: launchd workflow (`openclaw-config/ai.smartclaw.schedule.*.plist` + install scripts).

# Check for required tools
if ! command -v rg &> /dev/null; then
  echo "Error: ripgrep (rg) is required but not installed." >&2
  echo "Install via: brew install ripgrep" >&2
  exit 1
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

FILES="$(git ls-files '*.md' '*.mdx' '*.sh')"

had_violations=0

# Check if a file contains guardrail context (words that indicate the file is talking about policy/forbidden items)
file_has_guardrail_context() {
  local file="$1"
  if rg -i -q 'forbidden|never use system crontab|do not use.*crontab|must not.*crontab|prohibit.*crontab|no system crontab|use launchd|launchd scheduler|openclaw.*cron|legacy cron|fallback crontab|migrated off system crontab' "$file"; then
    return 0
  else
    return 1
  fi
}

# Check for violation: crontab + OpenClaw in close proximity (within 3 lines)
check_file_for_violation() {
  local file="$1"
  local violations=0

  # Skip if file explicitly discusses the guardrail policy
  if file_has_guardrail_context "$file"; then
    return 0
  fi

  # Look for crontab references combined with OpenClaw identifiers
  # Use multiline mode to catch cases where they're on nearby lines
  while IFS=: read -r line_no line; do
    [[ -z "$line_no" ]] && continue

    # Get context: current line + 2 lines after
    local context
    context=$(sed -n "${line_no},$((line_no + 2))p" "$file")

    # Check if context contains both crontab and OpenClaw-related terms
    if echo "$context" | rg -iq 'crontab' && echo "$context" | rg -iq 'openclaw|\.smartclaw|ai\.smartclaw|openclaw-backup|backup-content'; then
      echo "Guardrail violation: $file:$line_no"
      echo "  $line"
      violations=1
    fi
  done < <(rg -n --no-heading -i 'crontab' "$file" || true)

  return $violations
}

while IFS= read -r file; do
  case "$file" in
    .smartclaw-backups/*|openclaw/.smartclaw-backups/*|openclaw-config/agents/*|openclaw-config/credentials/*|scripts/check-openclaw-cron-guardrail.sh|scripts/install-openclaw-backup-jobs.sh|scripts/setup-openclaw-full.sh|BACKUP_AND_RESTORE.md|CLAUDE.md|testing_llm/MEMORY_QUALITY_TEST.md)
      continue
      ;;
  esac

  if ! check_file_for_violation "$file"; then
    had_violations=1
  fi
done <<< "$FILES"

if [[ "$had_violations" -ne 0 ]]; then
  echo
  echo "OpenClaw cron guardrail failed."
  echo "Use launchd for recurring OpenClaw automation in this repo."
  echo "Do not add system crontab entries or new openclaw in-app cron schedules for repo-managed jobs."
  exit 1
fi

echo "OpenClaw cron guardrail check passed."
