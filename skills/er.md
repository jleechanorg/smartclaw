# ER - Evidence Review

Run an independent evidence review of files matching `evidence*` or `testing_*` in the current PR.

## When to use

Use this skill when you need to verify that evidence or test files in a PR are correct. This is required as condition 6 of the merge gate — skeptic-cron.yml Gate 6 checks the evidence-review-bot's GitHub review state (APPROVED/DISMISSED/CHANGES_REQUESTED), so you must submit a real GitHub review, not a PR comment.

## Steps

1. **Identify evidence/test files** in the current PR:
   ```bash
   gh pr view $PR_NUMBER --repo $GITHUB_ORG/$REPO --json files | jq '.files[].path' | grep -E "^evidence|testing_"
   ```

2. **Read the content** of each evidence/test file:
   ```bash
   gh pr diff $PR_NUMBER --repo $GITHUB_ORG/$REPO -- -- evidence* testing_*
   ```

3. **Run codex review** on those files:
   ```bash
   codex review --files "<file1,file2,...>"
   ```

4. **Post the result** as a GitHub PR review (not a comment):
   - PASS → `gh api .../reviews --method POST -f event=APPROVE -f body="**PASS** — evidence review: agent self-reviewed ✅, CR reviewed ✅, codex passed ✅"`
   - FAIL → `gh api .../reviews --method POST -f event=REQUEST_CHANGES -f body="**FAIL** — evidence review: <specific reasons>"`

   Gate 6 checks evidence-review-bot's GitHub review state, not PR comments, so an
   actual review (approve/dismiss/changes_requested) is required for the skeptic
   workflow to detect it.

## Example

```bash
# Find evidence files
EVIDENCE_FILES=$(gh pr view 123 --repo $GITHUB_ORG/$REPO --json files | jq -r '.files[].path' | grep -E "^evidence|testing_" | tr '\n' ',')

# Review with codex
codex review --files "$EVIDENCE_FILES"

# Post result as a GitHub review (Gate 6 checks review state, not comments)
gh api repos/$GITHUB_ORG/$REPO/pulls/123/reviews --method POST \
  -f event=APPROVE \
  -f body="**PASS** — evidence review: agent self-reviewed ✅, CR reviewed ✅, codex passed ✅"
```

## Requirements

- CodeRabbit must have already reviewed the PR (check via `gh pr reviews`)
- Agent must have self-reviewed the diff
- This skill provides the independent verification step

## Output format

Always post the result as a GitHub review (not a PR comment). The format must be exactly:
- PASS: `gh api .../reviews --method POST -f event=APPROVE -f body="**PASS** — evidence review: agent self-reviewed ✅, CR reviewed ✅, codex passed ✅"`
- FAIL: `gh api .../reviews --method POST -f event=REQUEST_CHANGES -f body="**FAIL** — evidence review: <reason>"`
