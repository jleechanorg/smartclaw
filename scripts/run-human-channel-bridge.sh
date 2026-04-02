#!/usr/bin/env bash
# Launcher for Human Channel Bridge.
# Usage:
#   ./run-human-channel-bridge.sh         # full bridge run
#   ./run-human-channel-bridge.sh health  # health check only
#   ./run-human-channel-bridge.sh heartbeat  # heartbeat only

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LOG_DIR="${HOME}/.smartclaw/logs/scheduled-jobs"
STATE_FILE="${HOME}/.smartclaw/state/human_channel_bridge.json"

mkdir -p "$LOG_DIR"
mkdir -p "$(dirname "$STATE_FILE")"

MODE="${1:-bridge}"

case "$MODE" in
  health)
    exec python3 -c "
import sys
sys.path.insert(0, '$REPO_ROOT/src')
from src.orchestration.human_channel_bridge import health_check_main
sys.exit(health_check_main())
"
    ;;
  heartbeat)
    exec python3 -c "
import sys, os
sys.path.insert(0, '$REPO_ROOT/src')
os.chdir('$REPO_ROOT')
from src.orchestration.human_channel_bridge import heartbeat_main
sys.exit(heartbeat_main())
"
    ;;
  bridge|*)
    exec python3 -c "
import sys, os
sys.path.insert(0, '$REPO_ROOT/src')
os.chdir('$REPO_ROOT')
from src.orchestration.human_channel_bridge import main
sys.exit(main())
"
    ;;
esac
