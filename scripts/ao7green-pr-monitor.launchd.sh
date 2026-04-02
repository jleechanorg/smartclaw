#!/usr/bin/env bash
# Wrapper for ao7green-pr-monitor.sh — run under launchd
# Sources full shell environment, then resolves GH_TOKEN via gh auth before
# invoking the monitor script (which uses 'set -u' and expects GH_TOKEN).
set -euo pipefail

# Source .bash_profile → .bashrc to get full interactive env
if [[ -f ~/.bash_profile ]]; then
  source ~/.bash_profile 2>/dev/null || true
fi

# Ensure GH_TOKEN is set (gh is authenticated; this avoids 'set -u' crash
# when resolve_token() falls through to '${tok:-$GH_TOKEN}')
if [[ -z "${GH_TOKEN:-}" ]]; then
  GH_TOKEN="$(gh auth token 2>/dev/null)" || true
fi
export GH_TOKEN

# Also export AO_BIN and AO_DIR if set in profile (monitor script expects them)
if [[ -n "${AO_BIN:-}" ]]; then
  export AO_BIN
fi
if [[ -n "${AO_DIR:-}" ]]; then
  export AO_DIR
fi

# Ensure env vars from launchd plist are also exported (may override profile values)
if [[ -n "${AO_MONITOR_REPO:-}" ]]; then
  export AO_MONITOR_REPO
fi
if [[ -n "${AO_MONITOR_PROJECT:-}" ]]; then
  export AO_MONITOR_PROJECT
fi
if [[ -n "${AO_MONITOR_LOG_DIR:-}" ]]; then
  export AO_MONITOR_LOG_DIR
fi

exec ${HOME}/.smartclaw/scripts/ao7green-pr-monitor.sh "$@"