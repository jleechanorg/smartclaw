---
name: git-pr-conflict-resolve
description: Resolve merge conflicts for a PR using a fresh worktree — rebasing onto the PR base branch
category: git
triggers:
  - "fix merge conflicts"
  - "resolve conflicts and push"
  - "PR has conflicts"
  - "this branch has conflicts"
---

# Git PR Conflict Resolution — Fresh Worktree Method

## When to Use

GitHub shows "This branch has conflicts that must be resolved" or `gh pr diff` shows uncommitted merge changes.

## Root Cause

A worktree was created or used with an **outdated base** — `origin/main` was ahead when the branch was created, or the worktree was used across sessions with intervening main merges.

## The Reliable Pattern (ALWAYS use this)

### Step 1 — Verify conflict state locally

```bash
cd <worktree>
git fetch origin <base-branch>   # e.g. origin/main
git merge origin/<base-branch> --no-edit
```

If this says "Already up to date", GitHub's mergeability cache may be stale. Try `git push --force` to retrigger. If it shows conflict markers → proceed to Step 2.

### Step 2 — If conflicts exist: rebase onto current base

```bash
git fetch origin <base-branch>
git rebase origin/<base-branch>
```

Resolve conflict markers in your editor, then:
```bash
git add -A
git rebase --continue
```

### Step 3 — Verify clean state

```bash
git status          # should say "nothing to commit, working tree clean"
git log --oneline origin/<base-branch>..HEAD   # should show only YOUR commits
git push --force origin <branch-name>
```

### Step 4 — Wait for GitHub to recompute mergeability

```bash
sleep 10
gh api repos/<owner>/<repo>/pulls/<pr-number> --jq '.mergeable_state'
```

If still `dirty` or `unknown`, GitHub may need a fresh SHA. Force-push again with an empty amend:
```bash
git commit --allow-empty -m "chore: retrigger mergeability"
git push --force origin <branch-name>
```

### Step 5 — If GitHub is still stale: close + reopen

```bash
gh pr close <pr-number>
gh pr reopen <pr-number>
```

Then wait 30s and recheck `mergeable_state`.

## If local says "up to date" but GitHub says "conflicts"

This means GitHub's cached SHA for your branch head is behind what it thinks the base is. A force-push of the current HEAD always triggers recompute:

```bash
git push --force origin <branch-name>
sleep 15
gh api repos/<owner>/<repo>/pulls/<pr-number> --jq '.mergeable_state'
```

## The Fresh Worktree Shortcut (when Step 1-4 keep failing)

If conflict resolution keeps failing or GitHub stays `dirty`:
```bash
# In the repo root (NOT in the worktree)
git worktree remove ../worktree_<name>      # destroy the worktree
git fetch origin main
git worktree add ../worktree_<name> -b worktree_<name> origin/main
# Cherry-pick your commits from the old branch if needed
git cherry-pick <commit-sha>
git push -u origin worktree_<name>
```

## IMPORTANT: Why you keep getting stuck

1. **Wrong assumption**: `git merge origin/main` saying "up to date" means your branch is based on a current main, NOT that GitHub's mergeability is fresh.
2. **Stale local tracking**: `git fetch` does NOT update remote tracking refs until you explicitly `git fetch origin <branch>`.
3. **Same-epoch confusion**: If origin/main hasn't changed AND your branch is based on it, there's no conflict — but GitHub may have old conflict cache from a previous state of your branch.
4. **Fix**: Always force-push with a new SHA to force GitHub to recompute.

## Verification Checklist

- [ ] `git status` is clean
- [ ] `git log origin/<base>..HEAD` shows only your intended commits
- [ ] `gh pr diff --name-only` shows only intended files  
- [ ] GitHub PR shows `mergeable_state: clean` or `unknown` (then `clean` after refresh)
- [ ] CI checks are still passing
