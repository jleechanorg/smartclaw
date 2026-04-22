#!/usr/bin/env bash
# Install the AO orchestrator launchd job.
#
# The orchestrator is the component that polls GitHub for new CI results,
# review comments (CodeRabbit, Cursor Bugbot, Copilot), and fires reactions
# (ci-failed, bugbot-comments, changes-requested, merge-conflicts) to agents.
#
# Without this running, reactions never fire and agents must be nudged manually.
#
# Usage:
#   ./scripts/install-ao-orchestrators.sh [--uninstall]
#
# Reads active projects from agent-orchestrator.yaml and starts one orchestrator
# per project. Requires GITHUB_TOKEN in environment or ~/.bashrc.
#
set -euo pipefail

LAUNCHD_DIR="$HOME/Library/LaunchAgents"
AO_BIN="$HOME/bin/ao"
AO_CONFIG="${AO_CONFIG_PATH:-$HOME/agent-orchestrator.yaml}"
AO_WORKDIR="$HOME/.smartclaw"
LABEL="ai.agento.orchestrators"

UNINSTALL=false
[[ "${1:-}" == "--uninstall" ]] && UNINSTALL=true

uninstall_job() {
  launchctl bootout "gui/$UID" "$LAUNCHD_DIR/$LABEL.plist" 2>/dev/null || true
  rm -f "$LAUNCHD_DIR/$LABEL.plist"
  echo "  ✓ $LABEL uninstalled"
}

if $UNINSTALL; then
  echo "Uninstalling AO orchestrators..."
  uninstall_job
  exit 0
fi

# Validate prerequisites
if [[ ! -x "$AO_BIN" ]]; then
  echo "ERROR: ao binary not found at $AO_BIN" >&2
  echo "  Install: npm i -g @agent-orchestrator/cli" >&2
  exit 1
fi
if [[ ! -f "$AO_CONFIG" ]]; then
  echo "ERROR: agent-orchestrator.yaml not found at $AO_CONFIG" >&2
  echo "  Set AO_CONFIG_PATH or run bootstrap.sh from the smartclaw repo root to create ~/agent-orchestrator.yaml symlink." >&2
  exit 1
fi

# Resolve GITHUB_TOKEN: env > ~/.bashrc
GITHUB_TOKEN_VAL="${GITHUB_TOKEN:-}"
if [[ -z "$GITHUB_TOKEN_VAL" ]]; then
  GITHUB_TOKEN_VAL=$(grep -E '^export GITHUB_TOKEN=' ~/.bashrc 2>/dev/null | tail -1 | sed "s/^export GITHUB_TOKEN=[\"']*//;s/[\"']*\$//" || true)
fi
if [[ -z "$GITHUB_TOKEN_VAL" ]]; then
  echo "ERROR: GITHUB_TOKEN not set. Export it or add to ~/.bashrc." >&2
  echo "  Without it, AO cannot poll GitHub for PR events and reactions won't fire." >&2
  exit 1
fi

# Validate OPENCLAW_AO_HOOK_TOKEN (required for webhook notifications to gateway)
AO_HOOK_TOKEN_VAL="${OPENCLAW_AO_HOOK_TOKEN:-}"
if [[ -z "$AO_HOOK_TOKEN_VAL" ]]; then
  AO_HOOK_TOKEN_VAL=$(grep -E '^export OPENCLAW_AO_HOOK_TOKEN=' ~/.bashrc 2>/dev/null | tail -1 | sed "s/^export OPENCLAW_AO_HOOK_TOKEN=[\"']*//;s/[\"']*\$//" || true)
fi
if [[ -z "$AO_HOOK_TOKEN_VAL" ]]; then
  echo "ERROR: OPENCLAW_AO_HOOK_TOKEN not set. Export it or add to ~/.bashrc." >&2
  echo "  Without it, AO cannot send webhook notifications to the OpenClaw gateway." >&2
  exit 1
fi

# Extract project IDs from yaml (lines matching "^  [a-z][a-z0-9-]+:$" under projects:)
PROJECTS=$(python3 - "$AO_CONFIG" <<'EOF'
import sys, re
path = sys.argv[1]
text = open(path).read()
# Find projects: block
m = re.search(r'^projects:\n(.*?)^(?:reactions:|plugins:|defaults:|notifiers:|notificationRouting:|\Z)', text, re.M | re.S)
if not m:
    sys.exit(0)
block = m.group(1)
for line in block.splitlines():
    m2 = re.match(r'^  ([a-z][a-z0-9_-]+):$', line)
    if m2:
        print(m2.group(1))
EOF
)

if [[ -z "$PROJECTS" ]]; then
  echo "ERROR: no projects found in $AO_CONFIG" >&2
  exit 1
fi

echo "Found projects:"
echo "$PROJECTS" | sed 's/^/  /'
echo ""

# Build the script that starts one orchestrator per project
PROJECTS_ONELINE=$(echo "$PROJECTS" | tr '\n' ' ')

mkdir -p "$LAUNCHD_DIR"
mkdir -p "$AO_WORKDIR"

cat > "$LAUNCHD_DIR/$LABEL.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>-c</string>
        <string>for p in $PROJECTS_ONELINE; do $AO_BIN start "\$p" --no-dashboard &amp; done; wait</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/bin:/opt/homebrew/bin</string>
        <key>HOME</key>
        <string>$HOME</string>
        <key>AO_CONFIG_PATH</key>
        <string>$AO_CONFIG</string>
        <key>GITHUB_TOKEN</key>
        <string>$GITHUB_TOKEN_VAL</string>
        <key>GH_TOKEN</key>
        <string>$GITHUB_TOKEN_VAL</string>
        <key>OPENCLAW_AO_HOOK_TOKEN</key>
        <string>$AO_HOOK_TOKEN_VAL</string>
    </dict>
    <key>WorkingDirectory</key>
    <string>$AO_WORKDIR</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
    <key>StandardOutPath</key>
    <string>/tmp/ao-orchestrators.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/ao-orchestrators.err.log</string>
</dict>
</plist>
PLIST

launchctl bootout "gui/$UID" "$LAUNCHD_DIR/$LABEL.plist" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$LAUNCHD_DIR/$LABEL.plist"

echo "Installing AO orchestrators..."
echo "  ✓ $LABEL loaded"
echo ""
echo "Orchestrators starting for: $PROJECTS_ONELINE"
echo ""
echo "Logs:"
echo "  tail -f /tmp/ao-orchestrators.log"
echo "  tail -f /tmp/ao-orchestrators.err.log"
echo ""
echo "Verify reactions firing:"
echo "  grep -i 'reaction\\|bugbot\\|send-to-agent' ~/.agent-orchestrator/*/lifecycle-worker.log"
