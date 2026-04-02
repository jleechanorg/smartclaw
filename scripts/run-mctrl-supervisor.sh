#!/usr/bin/env bash
# RETIRED: orchestration.supervisor has been retired.
#
# This script previously launched the Python mctrl supervisor loop.
# The equivalent functionality is now handled by agent-orchestrator (AO):
#
#   ao lifecycle-worker <project>
#
# The launchd plist (ai.mctrl.supervisor) should be updated to call
# install-mctrl-supervisor.sh --uninstall, or replaced by the AO lifecycle
# plist managed via install-ao-lifecycle-agent-orchestrator.sh.
set -euo pipefail

echo "ERROR: run-mctrl-supervisor.sh is retired." >&2
echo "  The Python orchestration.supervisor module has been removed." >&2
echo "  Use agent-orchestrator instead: ao lifecycle-worker <project>" >&2
echo "  See: agent-orchestrator.yaml for project configuration." >&2
exit 1
