#!/usr/bin/env bash
# Bootstrap: post-clone setup for ~/.smartclaw/ (jleechanorg/smartclaw)
# This script is idempotent — safe to re-run on an existing installation.
# Requirements: bash 4+, jq, launchctl (macOS only for launchd install).
# Note: bootstrap.sh uses `set -euo pipefail` — the installer call captures
# output via assignment; `set -e` does not trigger on command substitution exit.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "=== OpenClaw Bootstrap ==="
echo "Repo root: $REPO_ROOT"

# Recreate workspace-monitor skills symlink (gitignored)
if [ -d "$REPO_ROOT/workspace-monitor" ]; then
    ln -sf "$REPO_ROOT/skills" "$REPO_ROOT/workspace-monitor/skills" && echo "Symlink: workspace-monitor/skills -> skills/"
fi

# Agent Orchestrator config — symlink ~/agent-orchestrator.yaml → repo copy
AO_YAML="$HOME/agent-orchestrator.yaml"
REPO_YAML="$REPO_ROOT/agent-orchestrator.yaml"
if [ -f "$REPO_YAML" ]; then
    if [ -L "$AO_YAML" ]; then
        # Verify symlink points to correct target (not stale from repo move).
        # Use python for realpath — readlink -f is GNU-only and unavailable on macOS.
        CURRENT_TARGET="$(python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" "$AO_YAML" 2>/dev/null || echo "")"
        EXPECTED_TARGET="$(python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" "$REPO_YAML" 2>/dev/null || echo "")"
        if [ -n "$CURRENT_TARGET" ] && [ -n "$EXPECTED_TARGET" ] && [ "$CURRENT_TARGET" = "$EXPECTED_TARGET" ]; then
            echo "Symlink valid: $AO_YAML -> $CURRENT_TARGET"
        else
            echo "WARNING: symlink points to stale target ($CURRENT_TARGET), updating to $EXPECTED_TARGET"
            ln -sf "$REPO_YAML" "$AO_YAML" && echo "Symlink updated: ~/agent-orchestrator.yaml -> $REPO_YAML"
        fi
    elif [ -f "$AO_YAML" ]; then
        echo "WARNING: $AO_YAML exists as a regular file — backing up to $AO_YAML.bak and replacing with symlink"
        mv "$AO_YAML" "$AO_YAML.bak"
        ln -s "$REPO_YAML" "$AO_YAML" && echo "Symlink: ~/agent-orchestrator.yaml -> $REPO_YAML"
    else
        ln -s "$REPO_YAML" "$AO_YAML" && echo "Symlink: ~/agent-orchestrator.yaml -> $REPO_YAML"
    fi
fi

# --- Webhook daemon setup ---
WEBHOOK_CFG="$HOME/.smartclaw/webhook.json"

# Generate webhook secret if not already set (idempotent)
_existing_secret=""
if [[ -f "$WEBHOOK_CFG" ]]; then
    _existing_secret="$(python3 -c "import json; d=json.load(open('$WEBHOOK_CFG')); print(d.get('webhookSecret',''))" 2>/dev/null || true)"
fi
if [[ -z "$_existing_secret" ]]; then
    _new_secret="$(openssl rand -hex 32)"
    python3 - "$_new_secret" <<'PYEOF'
import json, os, sys
secret = sys.argv[1]
p = os.path.expanduser("~/.smartclaw/webhook.json")
d = json.load(open(p)) if os.path.exists(p) else {}
d.setdefault("webhookDaemonPort", 19888)
d["webhookSecret"] = secret
open(p, "w").write(json.dumps(d, indent=2))
PYEOF
    echo "Generated GITHUB_WEBHOOK_SECRET -> webhook.json"
else
    echo "GITHUB_WEBHOOK_SECRET already set in webhook.json (skipping)"
fi

# Install LaunchAgents + scheduled jobs (central install — idempotent)
# install-openclaw-launchd.sh calls both install-launchagents.sh and
# install-openclaw-scheduled-jobs.sh, covering all openclaw launchd services.
echo "Installing OpenClaw launchd services (core + scheduled jobs)..."
# Capture installer output, filter to key status lines, warn if no output (install likely silently failed).
install_out="$(bash "$REPO_ROOT/scripts/install-openclaw-launchd.sh" 2>&1)"
install_rc=$?
printf '%s\n' "$install_out" | grep -E "✓|✗|skipping|WARNING" || true
if [[ $install_rc -ne 0 ]]; then
  echo "WARNING: install-openclaw-launchd.sh exited with errors. Review output above." >&2
fi

# --- Install openclaw CLI via npm (prefer Homebrew Node over NVM) ---
echo ""
echo "Installing openclaw CLI..."
if command -v node >/dev/null 2>&1; then
    NODE_PATH="$(command -v node)"
    if [[ "$NODE_PATH" =~ \.nvm/versions/node/ ]]; then
        echo "  WARNING: Node from NVM detected ($NODE_PATH)"
        echo "  Recommendation: install Node via Homebrew (brew install node) for stable paths"
        echo "  Current NVM-based Node will work but may break after Node version upgrades"
    fi
    
    # Check if openclaw is already installed
    if command -v openclaw >/dev/null 2>&1; then
        CURRENT_VERSION="$(openclaw --version 2>/dev/null | head -1 || echo 'unknown')"
        echo "  ✓ openclaw already installed: $CURRENT_VERSION"
        echo "  To upgrade: npm install -g openclaw@latest"
    else
        echo "  Installing openclaw globally via npm..."
        npm install -g openclaw@latest
        if command -v openclaw >/dev/null 2>&1; then
            INSTALLED_VERSION="$(openclaw --version 2>/dev/null | head -1 || echo 'unknown')"
            echo "  ✓ openclaw installed: $INSTALLED_VERSION"
        else
            echo "  ✗ openclaw installation failed or not in PATH"
        fi
    fi
else
    echo "  ✗ Node.js not found. Install Node first:"
    echo "    brew install node    # recommended (stable path)"
    echo "    or use NVM (warning: may break after Node upgrades)"
fi

# Optional: register GitHub webhook (requires GITHUB_REPO and Tailscale URL)
_TAILSCALE_HOST="${TAILSCALE_HOST:-}"
_GH_REPO="${GITHUB_REPO:-jleechanorg/smartclaw}"
if [[ -n "$_TAILSCALE_HOST" ]]; then
    echo "Registering GitHub webhook on $_GH_REPO -> http://$_TAILSCALE_HOST:19888/webhook ..."
    _secret="$(python3 -c "import json,os; p=os.path.expanduser('~/.smartclaw/webhook.json'); d=json.load(open(p)); print(d.get('webhookSecret',''))")"
    # Check if webhook already registered (idempotent)
    _existing_hook="$(gh api "repos/$_GH_REPO/hooks" 2>/dev/null | python3 -c "
import json,sys
hooks=json.load(sys.stdin)
for h in hooks:
    if '$_TAILSCALE_HOST' in h.get('config',{}).get('url',''):
        print(h['id'])
        break
" 2>/dev/null || true)"
    if [[ -n "$_existing_hook" ]]; then
        echo "  GitHub webhook already registered (id=$_existing_hook) — skipping"
    else
        gh api "repos/$_GH_REPO/hooks" \
          -f "name=web" \
          -f "config[url]=http://$_TAILSCALE_HOST:19888/webhook" \
          -f "config[content_type]=json" \
          -f "config[secret]=$_secret" \
          -f "config[insecure_ssl]=0" \
          -F "events[]=pull_request" \
          -F "events[]=pull_request_review" \
          -F "events[]=check_suite" \
          -F "active=true" 2>&1 | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(f'  Registered webhook id={d[\"id\"]}')
" 2>/dev/null || echo "  WARNING: gh api call failed — register webhook manually"
    fi
else
    echo "TAILSCALE_HOST not set — skipping GitHub webhook registration"
    echo "  To register: TAILSCALE_HOST=mac-1.tail5eb762.ts.net bash scripts/bootstrap.sh"
fi

# gog token restore — re-import from backup if keychain entry is missing
echo ""
echo "=== gog auth check ==="
if command -v gog &>/dev/null; then
    if gog auth list 2>&1 | grep -q "@"; then
        echo "OK: gog token present."
    elif [ -f "$REPO_ROOT/credentials/gog-refresh-token.json" ]; then
        echo "Restoring gog token from backup..."
        gog auth tokens import "$REPO_ROOT/credentials/gog-refresh-token.json" && echo "OK: gog token restored."
    else
        echo "WARNING: No gog token stored. Run:"
        echo "  GOOGLE_CLOUD_PROJECT=infinite-zephyr-487405-d0 gog auth add jleechan@gmail.com --services=gmail,calendar --remote"
        echo "Then back up the token:"
        echo "  gog auth tokens export jleechan@gmail.com --out ~/.smartclaw/credentials/gog-refresh-token.json"
    fi
else
    echo "gog not installed. Install with: brew install jleechanorg/tap/gog"
fi

echo ""
echo "Next: inject real tokens into openclaw.json"
echo "  cp openclaw.json.redacted openclaw.json"
echo "  # then edit openclaw.json with real tokens"
echo ""
echo "Also set in ~/.bashrc:"
echo "  export OPENCLAW_AO_HOOK_TOKEN='<token>'   # AO → openclaw notifier webhook token"
