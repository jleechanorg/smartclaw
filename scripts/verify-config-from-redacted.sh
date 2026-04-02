#!/usr/bin/env bash
# Verify that live openclaw.json matches openclaw.json.redacted + env vars
# Usage: bash scripts/verify-config-from-redacted.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
REDACTED_CONFIG="$REPO_ROOT/openclaw.json.redacted"
LIVE_CONFIG="$REPO_ROOT/openclaw.json"
REGEN_CONFIG="/tmp/openclaw.json.regen"

# Check prerequisites
if [[ ! -f "$REDACTED_CONFIG" ]]; then
  echo "ERROR: openclaw.json.redacted not found at $REDACTED_CONFIG"
  exit 1
fi

if [[ ! -f "$LIVE_CONFIG" ]]; then
  echo "ERROR: Live openclaw.json not found at $LIVE_CONFIG"
  exit 1
fi

if ! command -v jq >/dev/null 2>&1; then
  echo "ERROR: jq is required but not installed"
  exit 1
fi

# Required env vars (from _REDACTION_MAP in test_openclaw_configs.py)
required_vars=(
  "XAI_API_KEY"
  "SLACK_BOT_TOKEN"
  "OPENCLAW_SLACK_APP_TOKEN"
  "OPENCLAW_HOOKS_TOKEN"
  "OPENCLAW_GATEWAY_TOKEN"
  "OPENCLAW_GATEWAY_REMOTE_TOKEN"
  "OPENAI_API_KEY"
  "GROQ_API_KEY"
  "DISCORD_BOT_TOKEN"
)

# Check if all required env vars are set
missing_vars=()
for var in "${required_vars[@]}"; do
  if [[ -z "${!var:-}" ]]; then
    missing_vars+=("$var")
  fi
done

if [[ ${#missing_vars[@]} -gt 0 ]]; then
  echo "ERROR: Missing required environment variables: ${missing_vars[*]}"
  echo ""
  echo "These should be loaded from openclaw.json env section or shell environment."
  echo "Check ~/.profile or openclaw.json for the real values."
  exit 1
fi

echo "Generating openclaw.json.regen from openclaw.json.redacted + env vars..."

# Use Python to do the substitution (matches _expand_redacted from test_openclaw_configs.py)
python3 - "$REDACTED_CONFIG" "$REGEN_CONFIG" <<'PYPYTHON'
import json
import os
import sys

# Redaction map from test_openclaw_configs.py
REDACTION_MAP = [
    (["env", "XAI_API_KEY"],                                     "XAI_API_KEY"),
    (["env", "SLACK_BOT_TOKEN"],                        "SLACK_BOT_TOKEN"),
    (["env", "OPENCLAW_SLACK_APP_TOKEN"],                        "OPENCLAW_SLACK_APP_TOKEN"),
    (["env", "OPENCLAW_HOOKS_TOKEN"],                            "OPENCLAW_HOOKS_TOKEN"),
    (["hooks", "token"],                                          "OPENCLAW_HOOKS_TOKEN"),
    (["channels", "slack", "botToken"],                           "SLACK_BOT_TOKEN"),
    (["channels", "slack", "appToken"],                           "OPENCLAW_SLACK_APP_TOKEN"),
    (["channels", "discord", "token"],                            "DISCORD_BOT_TOKEN"),
    (["gateway", "auth", "token"],                                "OPENCLAW_GATEWAY_TOKEN"),
    (["gateway", "remote", "token"],                              "OPENCLAW_GATEWAY_REMOTE_TOKEN"),
    (["plugins", "entries", "openclaw-mem0", "config", "oss", "embedder", "config", "apiKey"], "OPENAI_API_KEY"),
    (["plugins", "entries", "openclaw-mem0", "config", "oss", "llm", "config", "api_key"],    "GROQ_API_KEY"),
    (["plugins", "entries", "openclaw-mem0", "config", "oss", "llm", "config", "apiKey"],     "GROQ_API_KEY"),
]

redacted_path = sys.argv[1]
output_path = sys.argv[2]

with open(redacted_path, 'r') as f:
    raw = f.read()

# Expand ${HOME} back to real home directory (matches generate_redacted_config.py)
home = os.path.expanduser("~")
raw = raw.replace("${HOME}", home)
config = json.loads(raw)

# Substitute env vars
for path, env_var in REDACTION_MAP:
    value = os.environ.get(env_var)
    if value is None:
        continue
    obj = config
    try:
        for p in path[:-1]:
            obj = obj[p]
        if path[-1] in obj:
            obj[path[-1]] = value
    except (KeyError, TypeError):
        pass

# Blank volatile timestamp fields
volatile_paths = [
    ["meta", "lastTouchedAt"],
    ["wizard", "lastRunAt"],
]
for path in volatile_paths:
    obj = config
    try:
        for p in path[:-1]:
            obj = obj[p]
        if path[-1] in obj:
            obj[path[-1]] = "__volatile__"
    except (KeyError, TypeError):
        pass

with open(output_path, 'w') as f:
    json.dump(config, f, indent=2, sort_keys=True)
    f.write('\n')

print(f"Generated {output_path}")
PYPYTHON

# Blank volatile fields in live config too for comparison
LIVE_BLANKED="/tmp/openclaw.json.live-blanked"
python3 - "$LIVE_CONFIG" "$LIVE_BLANKED" <<'PYPYTHON2'
import json
import sys

live_path = sys.argv[1]
output_path = sys.argv[2]

with open(live_path, 'r') as f:
    config = json.load(f)

# Blank volatile timestamp fields
volatile_paths = [
    ["meta", "lastTouchedAt"],
    ["wizard", "lastRunAt"],
]
for path in volatile_paths:
    obj = config
    try:
        for p in path[:-1]:
            obj = obj[p]
        if path[-1] in obj:
            obj[path[-1]] = "__volatile__"
    except (KeyError, TypeError):
        pass

with open(output_path, 'w') as f:
    json.dump(config, f, indent=2, sort_keys=True)
    f.write('\n')
PYPYTHON2

echo ""
echo "Comparing regenerated config to live config..."
echo ""

if diff -u "$REGEN_CONFIG" "$LIVE_BLANKED" > /tmp/config-diff.txt; then
  echo "✅ SUCCESS: Live openclaw.json matches openclaw.json.redacted + env vars"
  echo ""
  echo "This means:"
  echo "  - All tokens in openclaw.json come from the tracked redacted template"
  echo "  - No manual edits have drifted from the template"
  echo "  - Config is reproducible from openclaw.json.redacted + env vars"
  exit 0
else
  echo "❌ FAIL: Live openclaw.json does NOT match openclaw.json.redacted + env vars"
  echo ""
  echo "Differences found:"
  cat /tmp/config-diff.txt
  echo ""
  echo "This means the live config has drifted from the template."
  echo ""
  echo "To fix:"
  echo "  1. Review the diff above"
  echo "  2. If live config has manual edits that should be kept:"
  echo "     - Run: python3 scripts/generate_redacted_config.py"
  echo "     - Commit the updated openclaw.json.redacted"
  echo "  3. If live config has stale/wrong values:"
  echo "     - Regenerate live config: (no script yet - manual fix required)"
  echo "     - Or fix the specific fields shown in diff"
  echo ""
  echo "Files for inspection:"
  echo "  - Redacted template: $REDACTED_CONFIG"
  echo "  - Live config: $LIVE_CONFIG"
  echo "  - Regenerated (redacted + env vars): $REGEN_CONFIG"
  echo "  - Live (with volatile blanked): $LIVE_BLANKED"
  exit 1
fi
