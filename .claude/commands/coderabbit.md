---
description: Post "@coderabbitai all good?" on the current branch's PR only after pushing fixes for CodeRabbit comments
type: git
execution_mode: immediate
---
## ⚡ EXECUTION INSTRUCTIONS FOR CLAUDE
**When this command is invoked, YOU (Claude) must execute these steps immediately.**

## 🚨 GUARDRAIL — When to run
- **Only** run after you have **pushed at least one commit** that addresses CodeRabbit review comments. Do **not** run on a timer or before pushing.
- If you have not yet pushed fixes for CodeRabbit feedback, tell the user to push first, then run this command.

## Step 1: Get current branch and PR
```bash
gh pr view --json number,url,title
```
If no PR is found, tell the user: "No open PR found for the current branch."

## Step 2: Post CodeRabbit re-review ping
Post exactly this (correct handle is `coderabbitai`, no hyphen):
```bash
gh pr comment <PR_NUMBER> --body "@coderabbitai all good?"
```

## Step 3: Request evidence review (if PR has evidence)
If the PR has evidence, also ask CodeRabbit to run `/er` on it. First, determine the evidence path:

```bash
# Get repo info
REPO=$(gh repo view --json nameWithOwner -q .nameWithOwner)
BRANCH=$(gh pr view <PR_NUMBER> --json headRefName -q .headRefName)

# Check which evidence format exists (file or directory)
if gh api repos/$REPO/contents/docs/pr-validation/pr-<PR_NUMBER>-evidence.md >/dev/null 2>&1; then
  EVIDENCE_PATH="docs/pr-validation/pr-<PR_NUMBER>-evidence.md"
  EVIDENCE_URL="https://github.com/$REPO/blob/$BRANCH/$EVIDENCE_PATH"
else
  EVIDENCE_PATH="evidence/pr-<PR_NUMBER>/"
  EVIDENCE_URL="https://github.com/$REPO/tree/$BRANCH/$EVIDENCE_PATH"
fi

# Post the evidence review request
gh pr comment <PR_NUMBER> --body "@coderabbitai please also run /er on the evidence bundle at $EVIDENCE_PATH and verify the evidence meets standards. See: $EVIDENCE_URL"
```

## Step 4: Confirm
Report to the user: PR number, URL, and that the comments were posted:
- `@coderabbitai all good?` 
- Evidence review request (if evidence was found)

Remind that this should be done only after a fix push, not repeatedly.

## Reference
- See docs/coderabbit-ping-workflow.md and AGENTS.md CodeRabbit Review Protocol.
