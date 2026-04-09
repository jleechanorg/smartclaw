#!/opt/homebrew/bin/bash
# Wrapper for stability-report.sh — run under launchd
set -euo pipefail

# Source full shell environment
if [[ -f ~/.bash_profile ]]; then
  source ~/.bash_profile 2>/dev/null || true
fi

# Ensure GH_TOKEN is set
if [[ -z "${GH_TOKEN:-}" ]]; then
  GH_TOKEN="$(gh auth token 2>/dev/null)" || true
fi
export GH_TOKEN

exec "$HOME/.openclaw/scripts/stability-report.sh" "$@"