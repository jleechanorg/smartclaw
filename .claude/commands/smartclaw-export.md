---
description: Run portability audit and export smartclaw updates to smartclaw
type: workflow
execution_mode: immediate
---

Run the `smartclaw-portability-export` workflow:

1. `bash scripts/update-smartclaw-export-map.sh`
2. `bash scripts/sync-to-smartclaw.sh`
3. Return:
- smartclaw PR URL
- included/excluded counts
- any files deliberately excluded as non-portable
