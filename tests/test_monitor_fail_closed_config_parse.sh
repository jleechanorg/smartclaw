#!/bin/bash
# Integration regression: monitor must fail closed when openclaw message send
# returns config/typeerror output signatures even if exit status is 0.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$REPO_ROOT/monitor-agent.sh"

PASSED=0
FAILED=0

pass() { echo "PASS: $1"; PASSED=$((PASSED + 1)); }
fail() { echo "FAIL: $1"; FAILED=$((FAILED + 1)); }

TMP_ROOT="$(mktemp -d /tmp/test-monitor-fail-closed.XXXXXX)"
cleanup() { rm -rf "$TMP_ROOT"; }
trap cleanup EXIT

HOME_DIR="$TMP_ROOT/home"
STATE_DIR="$HOME_DIR/.smartclaw_prod"
OC_DIR="$TMP_ROOT/ocdir"
LOG_FILE="$TMP_ROOT/monitor.log"
LOCK_DIR="$TMP_ROOT/monitor-agent.lock"

mkdir -p "$HOME_DIR/bin" "$STATE_DIR/logs" "$STATE_DIR/cron" "$OC_DIR/workspace"
cat > "$STATE_DIR/cron/jobs.json" <<'JSON'
{"jobs":[]}
JSON

cat > "$STATE_DIR/openclaw.json" <<'JSON'
{
  "gateway": { "auth": { "token": "gateway-token" } },
  "channels": {
    "slack": {
      "enabled": true,
      "botToken": "slack-bot-token",
      "appToken": "slack-app-token",
      "channels": { "*": { "enabled": true, "requireMention": false } }
    }
  },
  "plugins": {
    "slots": { "memory": "openclaw-mem0" },
    "entries": {
      "openclaw-mem0": {
        "enabled": true,
        "config": { "oss": { "embedder": { "config": { "apiKey": "openai-token" } } } }
      }
    }
  },
  "env": { "XAI_API_KEY": "xai-token" }
}
JSON

for name in SOUL TOOLS USER IDENTITY HEARTBEAT AGENTS MEMORY; do
  printf '# %s\n' "$name" > "$OC_DIR/${name}.md"
  printf '# %s\n' "$name" > "$OC_DIR/workspace/${name}.md"
done

cat > "$HOME_DIR/bin/openclaw" <<'EOF'
#!/bin/bash
set -euo pipefail
if [[ "${1:-}" == "message" && "${2:-}" == "read" ]]; then
  echo '{"messages":[{"ts":"1775624999.000001"}]}'
  exit 0
fi
if [[ "${1:-}" == "message" && "${2:-}" == "send" ]]; then
  cat <<'OUT'
Failed to read config at /tmp/fake/openclaw.json
TypeError: Cannot read properties of undefined (reading 't')
OUT
  exit 0
fi
if [[ "${1:-}" == "mem0" && "${2:-}" == "search" ]]; then
  echo "No memories found."
  exit 0
fi
if [[ "${1:-}" == "--version" ]]; then
  echo "2026.4.6"
  exit 0
fi
echo '{"ok":true}'
EOF
chmod +x "$HOME_DIR/bin/openclaw"

cat > "$HOME_DIR/bin/curl" <<'EOF'
#!/bin/bash
set -euo pipefail
output_file=""
write_fmt=""
url=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -o)
      output_file="$2"
      shift 2
      ;;
    -w)
      write_fmt="$2"
      shift 2
      ;;
    http*://*|https://*)
      url="$1"
      shift
      ;;
    *)
      shift
      ;;
  esac
done

body='{"ok":true}'
if [[ "$url" == *"/health" ]]; then
  body='{"ok":true,"status":"live"}'
fi
if [[ -n "$output_file" ]]; then
  printf '%s' "$body" > "$output_file"
fi
if [[ -n "$write_fmt" ]]; then
  printf '%s' "${write_fmt//\%\{http_code\}/200}"
elif [[ -z "$output_file" ]]; then
  printf '%s' "$body"
fi
EOF
chmod +x "$HOME_DIR/bin/curl"

HOME="$HOME_DIR" \
PATH="$HOME_DIR/bin:$PATH" \
OPENCLAW_STATE_DIR="$STATE_DIR" \
OPENCLAW_CONFIG_PATH="$STATE_DIR/openclaw.json" \
OPENCLAW_MONITOR_OC_DIR="$OC_DIR" \
OPENCLAW_MONITOR_THREAD_REPLY_CHECK=0 \
OPENCLAW_MONITOR_LOCK_DIR="$LOCK_DIR" \
OPENCLAW_MONITOR_DOCTOR_SH_ENABLE=0 \
OPENCLAW_MONITOR_AO_DOCTOR_ENABLE=0 \
OPENCLAW_MONITOR_INFERENCE_PROBE_ENABLE=0 \
OPENCLAW_MONITOR_RUN_CANARY=0 \
OPENCLAW_MONITOR_PHASE2_ENABLE=0 \
OPENCLAW_MONITOR_STATUS_BROADCAST_ENABLE=0 \
OPENCLAW_MONITOR_GATEWAY_PROBE_MESSAGE_ENABLE=1 \
OPENCLAW_MONITOR_TOKEN_PROBES_ENABLE=0 \
OPENCLAW_MONITOR_LOG_FILE="$LOG_FILE" \
bash "$SCRIPT" || monitor_rc=$?

monitor_rc="${monitor_rc:-0}"
if [[ "$monitor_rc" -ne 0 ]]; then
  pass "monitor exits non-zero when fail-closed delivery path triggers"
else
  fail "monitor unexpectedly exited zero on fail-closed delivery path"
fi

if grep -q '^STATUS=PROBLEM' "$LOG_FILE"; then
  pass "monitor fails closed to STATUS=PROBLEM on config parse/typeerror signature"
else
  fail "monitor did not fail closed to STATUS=PROBLEM"
fi

if grep -Eq 'fail-closed slack_send_probe(_post_phase1|_post_phase2)?: config parse/typeerror signature detected' "$LOG_FILE"; then
  pass "monitor records fail-closed reason in probe summary"
else
  fail "monitor log missing fail-closed slack_send_probe reason"
fi

if [[ "$FAILED" -gt 0 ]]; then
  echo ""
  echo "FAILED: $FAILED test(s), PASSED: $PASSED"
  exit 1
fi

echo ""
echo "PASSED: $PASSED test(s)"
