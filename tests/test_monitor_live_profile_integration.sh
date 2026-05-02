#!/bin/bash
# Integration-style smoke test for monitor-agent.sh against a temp prod-like state dir.
#
# Proves the monitor targets OPENCLAW_CONFIG_PATH / OPENCLAW_STATE_DIR instead of the
# repo-root stub config, and treats a healthy mem0 empty-corpus response as STATUS=GOOD.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$REPO_ROOT/monitor-agent.sh"

PASSED=0
FAILED=0

pass() { echo "PASS: $1"; PASSED=$((PASSED + 1)); }
fail() { echo "FAIL: $1"; FAILED=$((FAILED + 1)); }

TMP_ROOT="$(mktemp -d /tmp/test-monitor-live-profile.XXXXXX)"
cleanup() { rm -rf "$TMP_ROOT"; }
trap cleanup EXIT

HOME_DIR="$TMP_ROOT/home"
STATE_DIR="$HOME_DIR/.smartclaw_prod"
OC_DIR="$TMP_ROOT/ocdir"
LOG_FILE="$TMP_ROOT/monitor.log"
MEMORY_SURFACE_FILE="$TMP_ROOT/memory-surface.txt"
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
      "channels": {
        "*": { "enabled": true, "requireMention": false }
      }
    },
    "discord": { "enabled": false }
  },
  "plugins": {
    "slots": { "memory": "openclaw-mem0" },
    "entries": {
      "openclaw-mem0": {
        "enabled": true,
        "config": {
          "oss": {
            "embedder": {
              "config": { "apiKey": "openai-token" }
            }
          }
        }
      }
    }
  },
  "env": {
    "XAI_API_KEY": "xai-token"
  }
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
  echo '{"ok":true,"messageId":"msg-1","ts":"1775624999.000002"}'
  exit 0
fi
if [[ "${1:-}" == "mem0" && "${2:-}" == "search" ]]; then
  printf 'mem0\n' >> "${MEMORY_SURFACE_FILE:?}"
  echo "No memories found."
  exit 0
fi
if [[ "${1:-}" == "memory" && "${2:-}" == "search" ]]; then
  printf 'memory\n' >> "${MEMORY_SURFACE_FILE:?}"
  echo "unexpected legacy memory surface"
  exit 9
fi
if [[ "${1:-}" == "--version" ]]; then
  echo "2026.4.6"
  exit 0
fi
echo '{"ok":true}'
EOF
chmod +x "$HOME_DIR/bin/openclaw"

cat > "$HOME_DIR/bin/ao" <<'EOF'
#!/bin/bash
set -euo pipefail
echo "Results: PASS"
EOF
chmod +x "$HOME_DIR/bin/ao"

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
MEMORY_SURFACE_FILE="$MEMORY_SURFACE_FILE" \
OPENCLAW_STATE_DIR="$STATE_DIR" \
OPENCLAW_CONFIG_PATH="$STATE_DIR/openclaw.json" \
OPENCLAW_MONITOR_OC_DIR="$OC_DIR" \
OPENCLAW_MONITOR_THREAD_REPLY_CHECK=0 \
OPENCLAW_MONITOR_LOCK_DIR="$LOCK_DIR" \
OPENCLAW_MONITOR_DOCTOR_SH_ENABLE=0 \
OPENCLAW_MONITOR_AO_DOCTOR_ENABLE=0 \
OPENCLAW_MONITOR_INFERENCE_PROBE_ENABLE=0 \
OPENCLAW_MONITOR_RUN_CANARY=0 \
OPENCLAW_MONITOR_SLACK_E2E_MATRIX_ENABLE=0 \
OPENCLAW_MONITOR_WS_CHURN_THRESHOLD=999999 \
OPENCLAW_MONITOR_STATUS_BROADCAST_ENABLE=0 \
OPENCLAW_MONITOR_GATEWAY_PROBE_MESSAGE_ENABLE=0 \
OPENCLAW_MONITOR_LOG_FILE="$LOG_FILE" \
bash "$SCRIPT"

if grep -q '^STATUS=GOOD' "$LOG_FILE"; then
  pass "monitor reports STATUS=GOOD against live profile"
else
  fail "monitor did not report STATUS=GOOD"
fi

if grep -q 'memory_lookup rc=0 summary=memory lookup functional (corpus empty)' "$LOG_FILE"; then
  pass "monitor accepts empty mem0 corpus as healthy"
else
  fail "monitor did not treat empty mem0 corpus as healthy"
fi

if [[ -f "$MEMORY_SURFACE_FILE" ]] && grep -qx 'mem0' "$MEMORY_SURFACE_FILE"; then
  pass "monitor used the temp live config instead of repo-root stub"
else
  fail "monitor did not select the mem0 surface from the temp live config"
fi

if [[ "$FAILED" -gt 0 ]]; then
  echo ""
  echo "FAILED: $FAILED test(s), PASSED: $PASSED"
  exit 1
fi

echo ""
echo "PASSED: $PASSED test(s)"
