#!/opt/homebrew/bin/bash
# simplify-daily.sh — runs code simplification across all AO-managed repos
# using claude --print headlessly with /simplify, then creates a PR.

# Re-exec with Homebrew bash if invoked with bash 3.x (e.g. via launchd plist using /bin/bash).
# declare -A (associative arrays) requires bash 4+.
if [[ "${BASH_VERSINFO[0]:-0}" -lt 4 ]]; then
    exec /opt/homebrew/bin/bash "$0" "$@"
fi

set -euo pipefail

REPOS_DIR="${HOME}/projects"
REPOS_DIR_FALLBACK="${HOME}/projects_other"
REPOS_DIR_AGENT_ORCHESTRATOR="${HOME}/project_agento"

# Map: worktree_dir_name -> GitHub repo (owner/repo)
declare -A GH_REPOS=(
  ["agent-orchestrator"]="jleechanorg/agent-orchestrator"
  ["worldarchitect.ai"]="jleechanorg/worldarchitect.ai"
  ["worldai_claw"]="jleechanorg/worldai_claw"
  ["ai_universe_living_blog"]="jleechanorg/ai_universe_living_blog"
  ["mcp_mail"]="jleechanorg/mcp_mail"
)

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
LOG_FILE="${HOME}/Library/Logs/openclaw/simplify-daily-${TIMESTAMP}.log"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"; }
log_error() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: $*" | tee -a "$LOG_FILE" >&2; }

mkdir -p "$(dirname "$LOG_FILE")"

SIMPLIFY_SYSTEM_PROMPT="You are a code quality specialist. Run /simplify to find and apply simplification opportunities in this repository.

Rules:
- Only simplify — do NOT add features or fix bugs
- For each fix, commit with message: 'refactor: simplify <file> - <desc>'
- Do NOT run test suites
- If no simplifications are found, exit without making any commits
- IMPORTANT: Work in the repository at the current directory."

for repo_name in "${!GH_REPOS[@]}"; do
  gh_repo="${GH_REPOS[$repo_name]}"

  # Resolve repo path: check all known base directories
  # Strict check: require .git dir or worktree marker directly in candidate
  repo_path=""
  for base_dir in "$REPOS_DIR" "$REPOS_DIR_FALLBACK" "$REPOS_DIR_AGENT_ORCHESTRATOR"; do
    candidate="${base_dir}/${repo_name}"
    if [[ -d "$candidate/.git" ]]; then
      repo_path="$candidate"
      break
    fi
    # Also accept git worktree (gitdir: pointer file)
    if [[ -f "$candidate/.git" ]] && head -1 "$candidate/.git" | grep -q "^gitdir:"; then
      repo_path="$candidate"
      break
    fi
  done

  if [[ -z "$repo_path" ]]; then
    log "Repo not found for ${repo_name}, skipping"
    continue
  fi

  log "=== Processing ${repo_name} (${gh_repo}) ==="

  # Helper: run git in repo directory (strict — requires .git dir here, not parent)
  git_r() { git -C "$repo_path" "$@"; }

  # Helper to check if a ref exists (returns 0 if yes, 1 if no) — safe with set -e
  ref_exists() { git_r rev-parse --verify "$1" >/dev/null 2>&1; }

  # Helper: check for uncommitted changes (staged or unstaged)
  has_changes() { ! git_r diff --quiet 2>/dev/null || ! git_r diff --cached --quiet 2>/dev/null; }

  # Helper: checkout base branch with fallback chain
  checkout_base() { git_r checkout "$base_branch" 2>/dev/null || git_r checkout main 2>/dev/null || git_r checkout master 2>/dev/null || true; }

  # Helper: clean up a branch and return to base
  cleanup_branch() { checkout_base; git_r branch -D "$branch_name" 2>/dev/null || true; }

  # Resolve base branch (origin/main or origin/master), fetching if needed
  if ! ref_exists origin/main && ! ref_exists origin/master; then
    log "No origin/main found, fetching origin..."
    set +e
    git_r fetch origin >/dev/null 2>&1
    fetch_rc=$?
    set -e
    if [[ $fetch_rc -ne 0 ]]; then
      log_error "Failed to fetch origin for ${repo_name} (exit $fetch_rc), skipping"
      continue
    fi
  fi

  if ref_exists origin/main; then
    base_branch="origin/main"
  elif ref_exists origin/master; then
    base_branch="origin/master"
  else
    log_error "Neither origin/main nor origin/master exists for ${repo_name}, skipping"
    continue
  fi

  # Skip if uncommitted changes
  if has_changes; then
    log "Skipping ${repo_name} — uncommitted changes present"
    continue
  fi

  # Create daily simplify branch
  branch_name="simplify/$(date +%Y-%m-%d)-${repo_name}"
  if ref_exists "$branch_name"; then
    log "Branch $branch_name already exists for ${repo_name}, skipping"
    continue
  fi

  log "Creating branch ${branch_name} from ${base_branch}..."
  if ! git_r checkout -B "$branch_name" "$base_branch" 2>/dev/null; then
    log_error "Failed to create branch for ${repo_name}, skipping"
    checkout_base
    continue
  fi

  # Run claude --print headlessly with /simplify via stdin
  log "Running /simplify in ${repo_name} (10 min timeout)..."
  claude_exit=0
  (
    echo "/simplify"
  ) | claude --print \
    --add-dir "$repo_path" \
    --permission-mode bypassPermissions \
    --system-prompt "$SIMPLIFY_SYSTEM_PROMPT" \
    >> "$LOG_FILE" 2>&1 \
    &
  claude_pid=$!

  # Wait up to 10 minutes (600s)
  (
    sleep 600
    # If claude is still running after 10 min, kill it
    if kill -0 $claude_pid 2>/dev/null; then
      kill $claude_pid 2>/dev/null || true
    fi
  ) &
  timeout_pid=$!

  # Wait for claude to finish
  wait $claude_pid 2>/dev/null || claude_exit=$?

  # Kill the timeout subshell if it's still running
  kill $timeout_pid 2>/dev/null || true
  wait $timeout_pid 2>/dev/null || true

  log "Claude exited with code ${claude_exit} for ${repo_name}"

  # Verify repo is still accessible
  if ! ref_exists HEAD; then
    log_error "Repo ${repo_path} is not accessible after claude run, skipping"
    continue
  fi

  # Check if any changes were made (claude commits its work as new commits ahead of base)
  _n_commits_ahead=$(git_r rev-list --count HEAD ^"$base_branch" 2>/dev/null || echo 0)
  if [ "$_n_commits_ahead" -eq 0 ]; then
    log "No simplifications made in ${repo_name} — cleaning up branch"
    cleanup_branch
    continue
  fi

  # Show what changed (git diff is empty when branch has commits ahead — use commit count)
  log "${_n_commits_ahead} commit(s) changed in ${repo_name}"

  # Push the branch
  log "Pushing branch ${branch_name}..."
  if ! git_r push -u origin "$branch_name" 2>&1 | tee -a "$LOG_FILE"; then
    log_error "Failed to push ${repo_name}, cleaning up"
    cleanup_branch
    continue
  fi

  # Check if PR already exists
  existing_pr=$(gh pr list \
    --repo "$gh_repo" \
    --head "$branch_name" \
    --state open \
    --json number \
    --jq '.[0].number' 2>/dev/null || echo "")

  pr_action="skipped"
  if [[ -n "$existing_pr" ]]; then
    log "PR #$existing_pr already exists for ${repo_name}"
    pr_action="existing"
  else
    pr_title="[simplify] Code cleanup - $(date +%Y-%m-%d)"
    pr_body="Automated code simplification via daily simplify run.

${_n_commits_ahead} commit(s) — review and merge if looks good."
    if gh pr create \
      --repo "$gh_repo" \
      --title "$pr_title" \
      --body "$pr_body" \
      --base "${base_branch#origin/}" \
      2>&1 | tee -a "$LOG_FILE"; then
      pr_action="created"
    else
      log_error "Failed to create PR for ${repo_name}"
      pr_action="failed"
    fi
  fi

  # Return to base branch
  checkout_base
  log "Simplify PR ${pr_action} for ${repo_name}"
done

log "=== Simplify run complete ==="
