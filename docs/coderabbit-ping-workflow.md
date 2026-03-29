# CodeRabbit re-review ping workflow

## Correct behavior

- **Exact comment:** Post exactly `@coderabbitai all good?` (GitHub handle is `coderabbitai`, no hyphen).
- **When to post:** Only after you have **pushed at least one new commit** that addresses CodeRabbit review comments. Do not post on a schedule or before pushing.
- **Deduplication:** Post at most once per push. If the PR already has a recent "all good?" comment for the same head commit, do not post again.

## What was wrong (diagnosis)

- **Wrong handle:** Some automation and commands used `@coderabbit-ai` (hyphen) or `@coderabbit` (missing `ai`). CodeRabbit’s GitHub username is `@coderabbitai`; wrong handles do not trigger re-review.
- **Spam risk:** Posting on a timer or before a fix push causes noise and does not match CodeRabbit’s intended use (re-review after fixes).

## References

- AGENTS.md / CLAUDE.md: CodeRabbit Review Protocol
- openclaw-config/SOUL.md: exact re-review prompt
- worldarchitect.ai automation: comment-validation and PR monitor use `@coderabbitai` in review request bodies; `/coderabbit` command posts `@coderabbitai all good?` only when appropriate.
