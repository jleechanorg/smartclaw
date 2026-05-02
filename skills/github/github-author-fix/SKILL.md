---
name: github-author-fix
description: Fix a GitHub commit author identity and open a PR when force-push to main is blocked. Also covers the correct way to pass multi-line PR bodies to gh CLI.
triggers:
  - "fix commit author"
  - "wrong github author"
  - "force push blocked"
  - "gh pr create body file"
  - "non-fast-forward rejected github"
---

# GitHub Author Fix + PR Workflow for Locked Branches

Use when a commit lands with the wrong author AND the repo blocks direct force-push to `main`.

---

## Problem Pattern

```
git commit -m "..."   # local commit has wrong author (jleechan instead of ${GITHUB_USER})
git push origin main  # rejected: non-fast-forward
git push --force      # rejected: force-push to main is blocked by repo rules
```

---

## Step 1 — Find the correct author identity

```bash
# Query the repo for commits by the correct author to get their email/name
gh api repos/<org>/<repo>/commits?author=<correct_username> --paginate \
  --jq '.[0] | {author: .author.login, email: .commit.author.email, name: .commit.author.name}'
```

Example output:
```json
{"author":"${GITHUB_USER}","email":"${GITHUB_USER}@users.noreply.github.com","name":"${GITHUB_USER}"}
```

---

## Step 2 — Amend the commit with the correct author

```bash
git config user.name "<Correct Name>"
git config user.email "<correct@email>"

# --no-edit keeps the commit message, only changes author
git commit --amend --author="<Correct Name> <correct@email>" --no-edit

# Verify
git log --format="%H %an %ae" -1
```

---

## Step 3 — Push to a new branch (not main directly)

```bash
git checkout -b <branch-name>   # e.g. skill/youtube-transcribe
git push -u origin HEAD         # push the branch
```

Why: `main` rejects non-fast-forward pushes even without force. The only path is a PR from a branch.

---

## Step 4 — Create the PR with a clean body

**Always use `--body-file`**, never heredoc or inline `--body` with multi-line content. The shell will interpolate variable-like content (e.g. `$OUTPUT_DIR` or `$(cmd)`), mangling the body. `--body-file` passes raw content.

```bash
# Write body to a temp file (no shell interpolation)
cat > /tmp/pr-body.txt << 'ENDBODY'
## What

Adds skills/media/youtube-transcribe/

## Coverage

- Download via yt-dlp
- Transcribe via Whisper CLI
- Drive upload via gog or curl REST
- GitHub PR workflow

## Test Plan

- Skill loads correctly
- Commit author verified as ${GITHUB_USER}
- Branch pushed to origin
ENDBODY

# Create PR using the file
gh pr create \
  --repo <org>/<repo> \
  --title "skill: add youtube-transcribe" \
  --body-file /tmp/pr-body.txt
```

**Note:** gh auto-formats `--body-file` content as Markdown (adds headers, code fences, etc). If you need precise control over the final rendered body, write the PR description after creation using `gh pr edit <number> --body-file`.

---

## Step 5 — If a Wrong-Head PR Already Exists

If you accidentally created a PR from the wrong branch first (as a mistake):

```bash
# Close it via API
gh api repos/<org>/<repo>/pulls/<number> --method PATCH --field state=closed

# Reopen if needed
gh api repos/<org>/<repo>/pulls/<number> --method PATCH --field state=open
```

---

## Step 6 — Fixing the Wrong Commit on `origin/main`

After the correct PR is merged, `origin/main` will still contain the wrong-author commit in its history (force-push is blocked). The wrong commit cannot be removed without admin intervention or disabling branch protection.

**Workaround:** The wrong commit remains in history on `origin/main`. The correct commit (from the merged PR branch) supersedes it in the default branch once merged. This is a known limitation — the commit is visible in `git log` but is superseded.

**To sync locally after merge:**
```bash
git checkout main
git pull origin main
```

---

## Prevention

To avoid wrong-author commits in the future, configure the correct identity **before** committing in any repo:

```bash
git config --global user.name "${GITHUB_USER}"
git config --global user.email "${GITHUB_USER}@users.noreply.github.com"
```

Or per-repo (in `~/.hermes/.git/config`):
```bash
git config user.name "${GITHUB_USER}"
git config user.email "${GITHUB_USER}@users.noreply.github.com"
```

**Do not rely on `--global`** if you work across multiple GitHub accounts. Always verify with `git log --format="%H %an %ae" -1` before pushing.
