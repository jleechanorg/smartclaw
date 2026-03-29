#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_PARENT="${1:-$HOME}"
AO_DIR="$TARGET_PARENT/agent-orchestrator"

cat <<'MSG'
smartclaw install helper
------------------------
This repo is not an OpenClaw fork.
It is a copy of local settings/config + integration with jleechanorg/agent-orchestrator.
MSG

echo "[1/3] smartclaw repo: $ROOT_DIR"
echo "[2/3] checking dependency: jleechanorg/agent-orchestrator"

if [[ -d "$AO_DIR/.git" ]]; then
  echo "✓ Found existing agent-orchestrator at: $AO_DIR"
else
  echo "→ Cloning jleechanorg/agent-orchestrator into: $AO_DIR"
  git clone https://github.com/jleechanorg/agent-orchestrator.git "$AO_DIR"
fi

echo "[3/3] done"
cat <<EOF

Next steps:
- Review launchd templates in: $ROOT_DIR/launchd
- Review skills in: $ROOT_DIR/skills
- Ensure your OpenClaw runtime points to the smartclaw config you want to use
EOF
