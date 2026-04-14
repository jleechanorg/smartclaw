---
description: /claw - Route all tasks through Hermes gateway inference
type: orchestration
execution_mode: immediate
---
# /claw - Hermes Gateway Inference

**Usage**: `/claw <task description>`

**Purpose**: Routes ALL tasks through Hermes gateway inference. Coding tasks go through Hermes, which may then call `dispatch-task` internally if needed.

> **Note (2026-04-12):** Hermes replaced OpenClaw as THE agent. All tasks route through Hermes gateway (MiniMax M2.7). OpenClaw is dead.

## Execution

When this command is invoked with `$ARGUMENTS`:

```bash
TASK_DESCRIPTION="$ARGUMENTS"
set -euo pipefail

LOGDIR="/tmp/hermes"
mkdir -p "$LOGDIR"
chmod 700 "$LOGDIR" 2>/dev/null || true
STATUS_LOG="$(mktemp "$LOGDIR/.claw-status-XXXXXXXX")"
LAUNCHD_PLIST="$HOME/Library/LaunchAgents/ai.hermes.prod.plist"

# If a launchd gateway is installed, prefer its env over CLI defaults.
# This keeps /claw aligned with the actual running service.
if [ -f "$LAUNCHD_PLIST" ]; then
  while IFS='=' read -r key value; do
    [ -n "$key" ] || continue
    export "$key=$value"
  done < <(
    python3 - "$LAUNCHD_PLIST" <<'PY'
import plistlib
import sys

path = sys.argv[1]
try:
    with open(path, "rb") as fh:
        data = plistlib.load(fh)
except FileNotFoundError:
    raise SystemExit(0)

env = data.get("EnvironmentVariables") or {}
for key in ("HERMES_HOME", "HERMES_LOG_LEVEL"):
    value = env.get(key)
    if value:
        print(f"{key}={value}")
PY
  )
fi

# Ensure HERMES_HOME is set (prod by default)
export HERMES_HOME="${HERMES_HOME:-$HOME/.hermes_prod}"

# Verify config.yaml exists and is valid YAML
HERMES_CFG="$HERMES_HOME/config.yaml"
if [ ! -f "$HERMES_CFG" ]; then
  echo "Hermes config not found: $HERMES_CFG"
  exit 1
fi
if ! python3 -c "import yaml; yaml.safe_load(open('$HERMES_CFG'))" 2>/dev/null; then
  echo "Hermes config is not valid YAML: $HERMES_CFG"
  exit 1
fi

# Verify the gateway is running
if ! HERMES_HOME="$HERMES_HOME" hermes gateway status >"$STATUS_LOG" 2>&1; then
  echo "Hermes gateway is not running."
  sed -n '1,80p' "$STATUS_LOG"
  exit 1
fi
if ! grep -q 'Gateway is running' "$STATUS_LOG"; then
  echo "Hermes gateway is not healthy."
  sed -n '1,80p' "$STATUS_LOG"
  exit 1
fi

# Resolve slash commands
TASK_WITH_RESOLVED="$TASK_DESCRIPTION"

SLASH_CMD=$(printf '%s' "$TASK_DESCRIPTION" | python3 -c "
import sys, re
text = sys.stdin.read().strip()
clean = re.sub(r'https?://\S+', '', text)
m = re.search(r'(?:^|\s)/([\w-]+)', clean)
if m:
    print(m.group(1))
" 2>/dev/null)

if [ -n "$SLASH_CMD" ]; then
  RESOLVED_CONTENT=""
  RESOLVED_SOURCE=""
  for search_dir in ".claude/commands" "$HOME/.claude/commands"; do
    if [ -f "$search_dir/$SLASH_CMD.md" ]; then
      RESOLVED_CONTENT=$(cat "$search_dir/$SLASH_CMD.md" 2>/dev/null)
      RESOLVED_SOURCE="$search_dir/$SLASH_CMD.md"
      break
    fi
  done
  if [ -z "$RESOLVED_CONTENT" ]; then
    for search_dir in ".claude/skills" "$HOME/.claude/skills"; do
      if [ -f "$search_dir/$SLASH_CMD/SKILL.md" ]; then
        RESOLVED_CONTENT=$(cat "$search_dir/$SLASH_CMD/SKILL.md" 2>/dev/null)
        RESOLVED_SOURCE="$search_dir/$SLASH_CMD/SKILL.md"
        break
      elif [ -f "$search_dir/$SLASH_CMD.md" ]; then
        RESOLVED_CONTENT=$(cat "$search_dir/$SLASH_CMD.md" 2>/dev/null)
        RESOLVED_SOURCE="$search_dir/$SLASH_CMD.md"
        break
      fi
    done
  fi
  if [ -n "$RESOLVED_CONTENT" ]; then
    echo "Resolved /$SLASH_CMD from $RESOLVED_SOURCE"
    TASK_WITH_RESOLVED="The user asked: $TASK_DESCRIPTION

Below is the full definition of /$SLASH_CMD (resolved from $RESOLVED_SOURCE). Execute it as instructed:

---
$RESOLVED_CONTENT
---"
  fi
fi

TASK_FILE="$(mktemp "$LOGDIR/.claw-task-XXXXXXXX")"
chmod 600 "$TASK_FILE" 2>/dev/null || true
printf '%s' "$TASK_WITH_RESOLVED" >"$TASK_FILE"
if [ ! -s "$TASK_FILE" ]; then
  echo "Failed to build Hermes task file"
  exit 1
fi

LOGFILE="$LOGDIR/claw-$(date +%s).log"
nohup hermes chat \
  -q "$(cat "$TASK_FILE")" \
  --yolo \
  --max-turns 90 \
  -Q \
  --source tool \
  >"$LOGFILE" 2>&1 &

CLAW_PID=$!
sleep 3
if ! kill -0 "$CLAW_PID" 2>/dev/null; then
  echo "Hermes agent exited immediately."
  sed -n '1,80p' "$LOGFILE"
  exit 1
fi

echo "Task dispatched to Hermes gateway (PID: $CLAW_PID)"
echo "Log: $LOGFILE"
echo "Monitor: tail -f $LOGFILE"
echo "Kill: kill $CLAW_PID"
```

## Requirements

- Hermes gateway running via `hermes gateway status`
- `config.yaml` valid at `$HERMES_HOME/config.yaml`
- Slash command resolution: looks up `.claude/commands/` and `.claude/skills/` directories

## Notes

- All tasks route through Hermes agent inference (MiniMax M2.7 default).
- Hermes replaced OpenClaw as THE agent (2026-04-12).
- Slash commands are resolved before dispatch.
- Log file written to `/tmp/hermes/claw-<timestamp>.log`
- `--source tool` tag hides /claw sessions from `hermes sessions` user lists.
- `--yolo` bypasses tool approval prompts for autonomous execution.
- `-Q` (quiet) suppresses banner/spinner for clean log output.
