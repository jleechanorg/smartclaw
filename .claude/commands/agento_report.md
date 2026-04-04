---
name: agento_report
aliases:
  - agentor
description: Get a status report of all PRs agento is handling — merged vs not merged, green status breakdown
type: git
execution_mode: background
---

## ⚡ EXECUTION INSTRUCTIONS FOR CLAUDE

When this command is invoked, generate the agento PR report:

1. Run the report script:
```bash
~/.claude/scripts/agento-report.sh
```

2. After the report is generated, read and display:
```bash
cat /tmp/agento-report.md
```

3. Post the summary to Slack:
```bash
source ~/.profile  # loads $AGENTO_CHANNEL
mcp__slack__conversations_add_message --channel_id "$AGENTO_CHANNEL" --text "$(head -20 /tmp/agento-report.md)"
```

The report checks:
- GREEN: All CI passing, MERGEABLE, no unresolved comments, CodeRabbit APPROVE
- CI_FAILED: Required CI checks failing
- CI_PENDING: CI checks still running
- CONFLICT: mergeable is not MERGEABLE
- NO_CR: No CodeRabbit APPROVE yet
- COMMENTS: Unresolved review comments

Skips PRs merged more than 12 hours ago.
