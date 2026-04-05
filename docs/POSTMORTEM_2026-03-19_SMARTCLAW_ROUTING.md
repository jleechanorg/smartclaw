# Postmortem — 2026-03-19 — Smartclaw Routing / Delegation Failures

## Summary
A delegation flow intended for `jleechanorg/smartclaw` initially produced work in the wrong repo context (`jleechanorg/worldarchitect.ai`), then required corrective rerouting and tighter prompt constraints.

## What went wrong
1. Prompt lacked explicit source/target repo contract at first dispatch.
2. No mandatory pre-PR repo identity checks were enforced before first PR creation.
3. Session context became stale/anchored to wrong PR context.

## Impact
- Incorrect PR was created in wrong repository context.
- Rework and extra coordination required.
- Loss of operator trust/confidence in delegation reliability.

## Corrective actions applied
- Enforced explicit dispatch headers:
  - `SOURCE_REPO=jleechanorg/jleechanclaw`
  - `TARGET_REPO=jleechanorg/smartclaw`
- Added mandatory pre-PR checks:
  - `git remote -v`
  - `gh repo view --json nameWithOwner`
  - explicit `gh pr create --repo jleechanorg/smartclaw ...`
- Reset with fresh session when contamination/stale context appeared.

## Prevention (going forward)
- No cross-repo delegation without explicit SOURCE/TARGET contract.
- No "done" report without proof bundle:
  1) edited file paths,
  2) remote commit URL,
  3) PR URL.
- Any repo mismatch triggers immediate stop + correction before continuing.
