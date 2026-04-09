#!/usr/bin/env bash
# ao-manager-notifier.sh — wrapper that launches the notifier for ao-manager.
#
# ao-manager.sh calls this script to start the notifier.
# This wrapper sets required env vars then execs the Python notifier.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NOTIFIER_PY="${AO_NOTIFIER_PY:-$SCRIPT_DIR/agento-notifier.py}"

if [[ ! -f "$NOTIFIER_PY" ]]; then
  echo "[ao-manager-notifier] ERROR: notifier script not found: $NOTIFIER_PY" >&2
  exit 1
fi

# bash -lc ensures ~/.bash_profile and ~/.bashrc are sourced before python3 runs,
# making GITHUB_TOKEN, OPENCLAW_SLACK_BOT_TOKEN, etc. available.
exec bash -lc "python3 '$NOTIFIER_PY'"
