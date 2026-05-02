---
name: cmux-terminal-review
description: Review all cmux terminal workspaces and report status
triggers: ["review terminals", "check terminals", "review cmux", "terminal status"]
---

# cmux Terminal Review Skill

## Trigger
Jeffrey asks to "review terminals", "check terminals", "review cmux", or similar terminal inventory requests.

## Workflow
1. Run `cmux list-workspaces` to get all workspace names/IDs
2. Run `cmux list-surfaces` for surface topology view
3. For any workspace that looks active, run `cmux identify --workspace <id>` to get details
4. Categorize into: **Active/Working**, **Idle/Fresh**, **Dead/Problematic**
5. Report using the standard status format (Healthy/Risky/Blocked/Next actions)

## Output Format
Use Slack-native concise sections:
- **Healthy** (🟢 Active/Working) — workspaces doing useful work
- **Risky** (🟡 Idle/Fresh) — fresh but no activity, or mid-work items
- **Blocked** (🔴 Dead/Problematic) — crashed, hung, or stuck
- **Next actions** — items needing Jeffrey's attention

## Common Flags to Watch
- `composer 2 fast` / mid-edit states → possible crash risk
- `suspended` / `--resume=` → resumable agent sessions
- `git push` in progress → push may still be running
- `claude --teammate-mode` → autonomous agent running
- "fresh login" → no activity since last review

## Notes
- Run `cmux list-workspaces` and `cmux list-surfaces` dynamically — do not hardcode workspace inventory
- Recurring risk indicators: `composer 2 fast`, `mid-edit`, `suspended`, `--resume=`
