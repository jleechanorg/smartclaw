#!/bin/bash
# Integration-style regression for monitor-agent.sh Slack E2E matrix coverage.
#
# Proves the monitor runs all six positive Slack delivery modes:
#   - DM: with and without mention
#   - top-level channel message: with and without mention
#   - thread reply: with and without mention
#
# The test uses a stub curl binary that simulates Slack API behavior and returns
# synthetic OpenClaw bot replies in the appropriate conversation surface.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$REPO_ROOT/monitor-agent.sh"

PASSED=0
FAILED=0

pass() { echo "PASS: $1"; PASSED=$((PASSED + 1)); }
fail() { echo "FAIL: $1"; FAILED=$((FAILED + 1)); }

TMP_ROOT="$(mktemp -d /tmp/test-monitor-slack-e2e-matrix.XXXXXX)"
cleanup() {
  if [[ "${KEEP_TMP:-0}" == "1" ]]; then
    echo "KEEP_TMP=1 preserving $TMP_ROOT"
    return 0
  fi
  rm -rf "$TMP_ROOT"
}
trap cleanup EXIT

HOME_DIR="$TMP_ROOT/home"
STATE_DIR="$HOME_DIR/.smartclaw_prod"
OC_DIR="$TMP_ROOT/ocdir"
LOG_FILE="$TMP_ROOT/monitor.log"
LOCK_DIR="$TMP_ROOT/monitor-agent.lock"
SLACK_STATE_FILE="$TMP_ROOT/slack-state.json"

mkdir -p "$HOME_DIR/bin" "$STATE_DIR/logs" "$STATE_DIR/cron" "$OC_DIR/workspace"
echo '{"messages":[],"polls":{}}' > "$SLACK_STATE_FILE"
cat > "$STATE_DIR/cron/jobs.json" <<'JSON'
{"jobs":[]}
JSON
cat > "$STATE_DIR/openclaw.json" <<'JSON'
{
  "gateway": { "auth": { "token": "gateway-token" } },
  "channels": {
    "slack": {
      "enabled": true,
      "botToken": "xoxb-openclaw",
      "appToken": "xapp-openclaw",
      "channels": {
        "*": { "enabled": true, "requireMention": false }
      }
    }
  },
  "plugins": {
    "slots": { "memory": "openclaw-mem0" },
    "entries": {
      "openclaw-mem0": {
        "enabled": false
      }
    }
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
if [[ "${1:-}" == "--version" ]]; then
  echo "2026.4.9"
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

url=""
method="GET"
auth=""
data=""
write_fmt=""
declare -a query_pairs=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    -X)
      method="$2"
      shift 2
      ;;
    -H)
      if [[ "$2" == Authorization:\ Bearer* ]]; then
        auth="${2#Authorization: Bearer }"
      fi
      shift 2
      ;;
    -d|--data)
      data="$2"
      shift 2
      ;;
    -w)
      write_fmt="$2"
      shift 2
      ;;
    --data-urlencode)
      query_pairs+=("$2")
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

python3 - "$url" "$method" "$auth" "$data" "$write_fmt" "${query_pairs[@]-}" <<'PY'
import json
import os
import sys
from pathlib import Path

url = sys.argv[1]
method = sys.argv[2]
auth = sys.argv[3]
data = sys.argv[4]
write_fmt = sys.argv[5]
query_pairs = sys.argv[6:]
state_path = Path(os.environ["SLACK_STATE_FILE"])

def load_state():
    if state_path.exists():
        return json.loads(state_path.read_text())
    return {"messages": [], "polls": {}}

def save_state(state):
    state_path.write_text(json.dumps(state))

def reply_delay_polls():
    try:
        return max(0, int(os.environ.get("STUB_REPLY_DELAY_POLLS", "0")))
    except Exception:
        return 0

def parse_query(pairs):
    out = {}
    for item in pairs:
        if "=" in item:
            key, value = item.split("=", 1)
            out[key] = value
    return out

def next_ts(state):
    return f"1776000000.{len(state['messages']) + 1:06d}"

def sender_kind(token):
    if token == "xoxp-human":
        return "human"
    if token == "xoxp-app-user":
        return "app_user"
    if token == "xoxb-canary":
        return "canary"
    if token == "xoxb-openclaw":
        return "bot"
    return "unknown"

def synth_bot_reply(ts_value: str, channel: str, thread_ts: str | None):
    whole, frac = ts_value.split(".")
    frac_i = int(frac)
    return {
        "type": "message",
        "user": "UOPENCLAW",
        "bot_id": "BOPENCLAW",
        "bot_profile": {"user_id": "UOPENCLAW"},
        "text": "synthetic openclaw reply",
        "ts": f"{whole}.{frac_i + 500000:06d}",
        "channel": channel,
        "thread_ts": thread_ts or f"{whole}.{frac:0>6}",
    }

def should_emit_delayed_reply(state, key: str) -> bool:
    polls = state.setdefault("polls", {})
    polls[key] = polls.get(key, 0) + 1
    return polls[key] > reply_delay_polls()

if url.endswith("/health"):
    body = '{"ok":true,"status":"live"}'
    if write_fmt:
        body += write_fmt.replace('%{http_code}', '200')
    print(body)
    sys.exit(0)

if url.endswith("/auth.test"):
    if auth == "xoxb-openclaw":
        print('{"ok":true,"user_id":"UOPENCLAW"}')
    elif auth in ("xoxp-human", "xoxp-app-user"):
        print('{"ok":true,"user_id":"UHUMAN"}')
    else:
        print('{"ok":false,"error":"invalid_auth"}')
    sys.exit(0)

params = parse_query(query_pairs)
state = load_state()

if url.endswith("/conversations.open"):
    if auth in ("xoxp-human", "xoxp-app-user", "xoxb-openclaw") and params.get("users") == "UOPENCLAW":
        print('{"ok":true,"channel":{"id":"DOPENCLAW"}}')
    else:
        print('{"ok":false,"error":"invalid_users"}')
    sys.exit(0)

if url.endswith("/chat.postMessage"):
    payload = json.loads(data or "{}")
    ts = next_ts(state)
    kind = sender_kind(auth)
    msg = {
        "type": "message",
        "ts": ts,
        "channel": payload.get("channel"),
        "thread_ts": payload.get("thread_ts") or ts,
        "text": payload.get("text", ""),
        "sender_kind": kind,
        "user": "UHUMAN" if kind in ("human", "app_user") else "UCANARY",
        "bot_profile": {"user_id": "UCANARY"} if kind in ("canary", "bot") else {},
        "bot_id": "BCANARY" if kind in ("canary", "app_user", "bot") else "",
        "app_id": "AOPENCLAW" if kind in ("canary", "app_user", "bot") else "",
    }
    state["messages"].append(msg)
    save_state(state)
    response = {"ok": True, "channel": payload.get("channel"), "ts": ts, "message": msg}
    print(json.dumps(response))
    sys.exit(0)

if url.endswith("/conversations.history"):
    channel = params.get("channel", "")
    messages = [m for m in state["messages"] if m.get("channel") == channel]
    if channel == "DOPENCLAW":
      replies = []
      for message in messages:
          if message.get("sender_kind") in ("human", "app_user"):
              poll_key = f"history:{channel}:{message['ts']}"
              if should_emit_delayed_reply(state, poll_key):
                  replies.append(synth_bot_reply(message["ts"], channel, None))
      save_state(state)
      messages = messages + replies
    messages.sort(key=lambda m: m["ts"], reverse=True)
    print(json.dumps({"ok": True, "messages": messages}))
    sys.exit(0)

if url.endswith("/conversations.replies"):
    channel = params.get("channel", "")
    root_ts = params.get("ts", "")
    thread_messages = [m for m in state["messages"] if m.get("channel") == channel and m.get("thread_ts") == root_ts]
    root_message = next((m for m in state["messages"] if m.get("channel") == channel and m.get("ts") == root_ts), None)
    messages = []
    if root_message:
        messages.append(root_message)
    messages.extend(thread_messages)
    human_candidates = [
        m for m in ([root_message] if root_message else []) + thread_messages
        if m and m.get("sender_kind") in ("human", "app_user")
    ]
    if human_candidates:
        latest = sorted(human_candidates, key=lambda m: m["ts"])[-1]
        poll_key = f"replies:{channel}:{root_ts}"
        if should_emit_delayed_reply(state, poll_key):
            messages.append(synth_bot_reply(latest["ts"], channel, root_ts))
        save_state(state)
    messages.sort(key=lambda m: m["ts"])
    print(json.dumps({"ok": True, "messages": messages}))
    sys.exit(0)

print('{"ok":true}')
PY
EOF
chmod +x "$HOME_DIR/bin/curl"

HOME="$HOME_DIR" \
PATH="$HOME_DIR/bin:$PATH" \
SLACK_STATE_FILE="$SLACK_STATE_FILE" \
SLACK_USER_TOKEN="xoxp-human" \
OPENCLAW_SLACK_USER_TOKEN="xoxb-canary" \
OPENCLAW_MONITOR_CANARY_BOT_TOKEN="xoxb-canary" \
OPENCLAW_MONITOR_BOT_USER_ID="UOPENCLAW" \
OPENCLAW_STATE_DIR="$STATE_DIR" \
OPENCLAW_CONFIG_PATH="$STATE_DIR/openclaw.json" \
OPENCLAW_MONITOR_OC_DIR="$OC_DIR" \
OPENCLAW_MONITOR_SLACK_E2E_CHANNEL_TARGET="${SLACK_CHANNEL_ID}" \
OPENCLAW_MONITOR_SLACK_E2E_THREAD_CHANNEL_TARGET="C0AJ3SD5C79" \
OPENCLAW_MONITOR_CORE_MD_ENABLE=0 \
OPENCLAW_MONITOR_THREAD_REPLY_CHECK=0 \
OPENCLAW_MONITOR_LOCK_DIR="$LOCK_DIR" \
OPENCLAW_MONITOR_DOCTOR_SH_ENABLE=0 \
OPENCLAW_MONITOR_AO_DOCTOR_ENABLE=0 \
OPENCLAW_MONITOR_INFERENCE_PROBE_ENABLE=0 \
OPENCLAW_MONITOR_TOKEN_PROBES_ENABLE=0 \
OPENCLAW_MONITOR_MEMORY_LOOKUP_ENABLE=0 \
OPENCLAW_MONITOR_SLACK_READ_PROBE_ENABLE=0 \
OPENCLAW_MONITOR_GATEWAY_PROBE_MESSAGE_ENABLE=0 \
OPENCLAW_MONITOR_STATUS_BROADCAST_ENABLE=0 \
OPENCLAW_MONITOR_PHASE2_ENABLE=0 \
OPENCLAW_MONITOR_RUN_CANARY=0 \
OPENCLAW_MONITOR_SLACK_E2E_MATRIX_ENABLE=1 \
OPENCLAW_MONITOR_SLACK_E2E_TIMEOUT_SECONDS=2 \
OPENCLAW_MONITOR_SLACK_E2E_POLL_INTERVAL_SECONDS=1 \
OPENCLAW_MONITOR_WS_CHURN_THRESHOLD=999999 \
OPENCLAW_MONITOR_LOG_FILE="$LOG_FILE" \
bash "$SCRIPT"

if grep -q '^STATUS=GOOD' "$LOG_FILE"; then
  pass "monitor reports STATUS=GOOD with Slack matrix enabled"
else
  fail "monitor did not report STATUS=GOOD"
fi

if grep -q 'Slack E2E matrix passed=6/6' "$LOG_FILE"; then
  pass "monitor summary reports all six Slack delivery modes passing"
else
  fail "monitor summary missing 6/6 Slack matrix success"
fi

if grep -q 'sender=SLACK_USER_TOKEN' "$LOG_FILE"; then
  pass "monitor prefers SLACK_USER_TOKEN when OPENCLAW_SLACK_USER_TOKEN is bot-scoped"
else
  fail "monitor did not prefer SLACK_USER_TOKEN in mixed bot/user sender setup"
fi

if grep -q 'thread_channel=C0AJ3SD5C79' "$LOG_FILE"; then
  pass "monitor summary records the dedicated thread reply channel target"
else
  fail "monitor summary missing dedicated thread reply channel target"
fi

for mode in dm_no_mention dm_with_mention channel_no_mention channel_with_mention thread_no_mention thread_with_mention; do
  if grep -q "${mode}=ok" "$LOG_FILE"; then
    pass "mode ${mode} recorded as passing"
  else
    fail "mode ${mode} not recorded as passing"
  fi
done

printf '{"messages":[],"polls":{}}' > "$SLACK_STATE_FILE"
cat > "$STATE_DIR/logs/gateway.err.log" <<'EOF'
2026-04-09T19:00:00.000-07:00 [WARN]  socket-mode:SlackWebSocket:99 A pong wasn't received from the server before the timeout of 5000ms!
EOF

WS_LOG_FILE="$TMP_ROOT/monitor-ws-churn.log"
env -u SLACK_USER_TOKEN \
  HOME="$HOME_DIR" \
  PATH="$HOME_DIR/bin:$PATH" \
  SLACK_STATE_FILE="$SLACK_STATE_FILE" \
  OPENCLAW_SLACK_USER_TOKEN="xoxp-human" \
  OPENCLAW_MONITOR_CANARY_BOT_TOKEN="xoxb-canary" \
  OPENCLAW_MONITOR_BOT_USER_ID="UOPENCLAW" \
  OPENCLAW_STATE_DIR="$STATE_DIR" \
  OPENCLAW_CONFIG_PATH="$STATE_DIR/openclaw.json" \
  OPENCLAW_MONITOR_OC_DIR="$OC_DIR" \
  OPENCLAW_MONITOR_SLACK_E2E_CHANNEL_TARGET="${SLACK_CHANNEL_ID}" \
  OPENCLAW_MONITOR_SLACK_E2E_THREAD_CHANNEL_TARGET="C0AJ3SD5C79" \
  OPENCLAW_MONITOR_CORE_MD_ENABLE=0 \
  OPENCLAW_MONITOR_THREAD_REPLY_CHECK=0 \
  OPENCLAW_MONITOR_LOCK_DIR="$LOCK_DIR" \
  OPENCLAW_MONITOR_DOCTOR_SH_ENABLE=0 \
  OPENCLAW_MONITOR_AO_DOCTOR_ENABLE=0 \
  OPENCLAW_MONITOR_INFERENCE_PROBE_ENABLE=0 \
  OPENCLAW_MONITOR_TOKEN_PROBES_ENABLE=0 \
  OPENCLAW_MONITOR_MEMORY_LOOKUP_ENABLE=0 \
  OPENCLAW_MONITOR_SLACK_READ_PROBE_ENABLE=0 \
  OPENCLAW_MONITOR_GATEWAY_PROBE_MESSAGE_ENABLE=0 \
  OPENCLAW_MONITOR_STATUS_BROADCAST_ENABLE=0 \
  OPENCLAW_MONITOR_PHASE2_ENABLE=0 \
  OPENCLAW_MONITOR_RUN_CANARY=1 \
  OPENCLAW_MONITOR_SLACK_E2E_MATRIX_ENABLE=1 \
  OPENCLAW_MONITOR_SLACK_E2E_TIMEOUT_SECONDS=2 \
  OPENCLAW_MONITOR_SLACK_E2E_POLL_INTERVAL_SECONDS=1 \
  OPENCLAW_MONITOR_WS_CHURN_THRESHOLD=1 \
  OPENCLAW_MONITOR_LOG_FILE="$WS_LOG_FILE" \
  bash "$SCRIPT"

if grep -q 'Slack E2E matrix passed=6/6' "$WS_LOG_FILE"; then
  pass "monitor still runs Slack matrix when ws churn is red"
else
  fail "monitor skipped or failed Slack matrix under ws churn"
fi

if grep -q 'sender=OPENCLAW_SLACK_USER_TOKEN' "$WS_LOG_FILE"; then
  pass "monitor accepts OPENCLAW_SLACK_USER_TOKEN as the positive probe sender"
else
  fail "monitor did not use OPENCLAW_SLACK_USER_TOKEN for positive Slack probes"
fi

if grep -q '^STATUS=PROBLEM' "$WS_LOG_FILE"; then
  pass "monitor still reports STATUS=PROBLEM when ws churn remains red"
else
  fail "monitor should still surface ws churn as a problem"
fi

printf '{"messages":[],"polls":{}}' > "$SLACK_STATE_FILE"
SLOW_LOG_FILE="$TMP_ROOT/monitor-slow-replies.log"
HOME="$HOME_DIR" \
PATH="$HOME_DIR/bin:$PATH" \
SLACK_STATE_FILE="$SLACK_STATE_FILE" \
SLACK_USER_TOKEN="xoxp-human" \
OPENCLAW_SLACK_USER_TOKEN="xoxb-canary" \
OPENCLAW_MONITOR_CANARY_BOT_TOKEN="xoxb-canary" \
OPENCLAW_MONITOR_BOT_USER_ID="UOPENCLAW" \
OPENCLAW_STATE_DIR="$STATE_DIR" \
OPENCLAW_CONFIG_PATH="$STATE_DIR/openclaw.json" \
OPENCLAW_MONITOR_OC_DIR="$OC_DIR" \
OPENCLAW_MONITOR_SLACK_E2E_CHANNEL_TARGET="${SLACK_CHANNEL_ID}" \
OPENCLAW_MONITOR_SLACK_E2E_THREAD_CHANNEL_TARGET="C0AJ3SD5C79" \
OPENCLAW_MONITOR_CORE_MD_ENABLE=0 \
OPENCLAW_MONITOR_THREAD_REPLY_CHECK=0 \
OPENCLAW_MONITOR_LOCK_DIR="$LOCK_DIR" \
OPENCLAW_MONITOR_DOCTOR_SH_ENABLE=0 \
OPENCLAW_MONITOR_AO_DOCTOR_ENABLE=0 \
OPENCLAW_MONITOR_INFERENCE_PROBE_ENABLE=0 \
OPENCLAW_MONITOR_TOKEN_PROBES_ENABLE=0 \
OPENCLAW_MONITOR_MEMORY_LOOKUP_ENABLE=0 \
OPENCLAW_MONITOR_SLACK_READ_PROBE_ENABLE=0 \
OPENCLAW_MONITOR_GATEWAY_PROBE_MESSAGE_ENABLE=0 \
OPENCLAW_MONITOR_STATUS_BROADCAST_ENABLE=0 \
OPENCLAW_MONITOR_PHASE2_ENABLE=0 \
OPENCLAW_MONITOR_RUN_CANARY=1 \
OPENCLAW_MONITOR_SLACK_E2E_MATRIX_ENABLE=1 \
OPENCLAW_MONITOR_SLACK_E2E_TIMEOUT_SECONDS=5 \
OPENCLAW_MONITOR_SLACK_E2E_POLL_INTERVAL_SECONDS=1 \
OPENCLAW_MONITOR_WS_CHURN_THRESHOLD=999999 \
OPENCLAW_MONITOR_LOG_FILE="$SLOW_LOG_FILE" \
STUB_REPLY_DELAY_POLLS=3 \
bash "$SCRIPT"

if grep -q 'Slack E2E matrix passed=6/6' "$SLOW_LOG_FILE"; then
  pass "monitor tolerates delayed Slack replies within the e2e timeout window"
else
  fail "monitor did not tolerate delayed Slack replies"
fi

printf '{"messages":[],"polls":{}}' > "$SLACK_STATE_FILE"
APP_USER_LOG_FILE="$TMP_ROOT/monitor-app-user.log"
env -u SLACK_USER_TOKEN \
  HOME="$HOME_DIR" \
  PATH="$HOME_DIR/bin:$PATH" \
  SLACK_STATE_FILE="$SLACK_STATE_FILE" \
  OPENCLAW_SLACK_USER_TOKEN="xoxp-app-user" \
  OPENCLAW_MONITOR_CANARY_BOT_TOKEN="xoxb-canary" \
  OPENCLAW_MONITOR_BOT_USER_ID="UOPENCLAW" \
  OPENCLAW_STATE_DIR="$STATE_DIR" \
  OPENCLAW_CONFIG_PATH="$STATE_DIR/openclaw.json" \
  OPENCLAW_MONITOR_OC_DIR="$OC_DIR" \
  OPENCLAW_MONITOR_SLACK_E2E_CHANNEL_TARGET="${SLACK_CHANNEL_ID}" \
  OPENCLAW_MONITOR_SLACK_E2E_THREAD_CHANNEL_TARGET="C0AJ3SD5C79" \
  OPENCLAW_MONITOR_CORE_MD_ENABLE=0 \
  OPENCLAW_MONITOR_THREAD_REPLY_CHECK=0 \
  OPENCLAW_MONITOR_LOCK_DIR="$LOCK_DIR" \
  OPENCLAW_MONITOR_DOCTOR_SH_ENABLE=0 \
  OPENCLAW_MONITOR_AO_DOCTOR_ENABLE=0 \
  OPENCLAW_MONITOR_INFERENCE_PROBE_ENABLE=0 \
  OPENCLAW_MONITOR_TOKEN_PROBES_ENABLE=0 \
  OPENCLAW_MONITOR_MEMORY_LOOKUP_ENABLE=0 \
  OPENCLAW_MONITOR_SLACK_READ_PROBE_ENABLE=0 \
  OPENCLAW_MONITOR_GATEWAY_PROBE_MESSAGE_ENABLE=0 \
  OPENCLAW_MONITOR_STATUS_BROADCAST_ENABLE=0 \
  OPENCLAW_MONITOR_PHASE2_ENABLE=0 \
  OPENCLAW_MONITOR_RUN_CANARY=1 \
  OPENCLAW_MONITOR_SLACK_E2E_MATRIX_ENABLE=1 \
  OPENCLAW_MONITOR_SLACK_E2E_TIMEOUT_SECONDS=2 \
  OPENCLAW_MONITOR_SLACK_E2E_POLL_INTERVAL_SECONDS=1 \
  OPENCLAW_MONITOR_WS_CHURN_THRESHOLD=999999 \
  OPENCLAW_MONITOR_LOG_FILE="$APP_USER_LOG_FILE" \
  bash "$SCRIPT"

if grep -q 'Slack E2E matrix passed=6/6' "$APP_USER_LOG_FILE"; then
  pass "monitor treats app-backed xoxp sender messages as human-authored probes"
else
  fail "monitor misclassified app-backed xoxp sender messages"
fi

printf '{"messages":[],"polls":{}}' > "$SLACK_STATE_FILE"
BOT_SENDER_LOG_FILE="$TMP_ROOT/monitor-bot-sender.log"
env -u SLACK_USER_TOKEN \
  HOME="$HOME_DIR" \
  PATH="$HOME_DIR/bin:$PATH" \
  SLACK_STATE_FILE="$SLACK_STATE_FILE" \
  OPENCLAW_SLACK_USER_TOKEN="xoxb-openclaw" \
  OPENCLAW_MONITOR_CANARY_BOT_TOKEN="xoxb-canary" \
  OPENCLAW_MONITOR_BOT_USER_ID="UOPENCLAW" \
  OPENCLAW_STATE_DIR="$STATE_DIR" \
  OPENCLAW_CONFIG_PATH="$STATE_DIR/openclaw.json" \
  OPENCLAW_MONITOR_OC_DIR="$OC_DIR" \
  OPENCLAW_MONITOR_SLACK_E2E_CHANNEL_TARGET="${SLACK_CHANNEL_ID}" \
  OPENCLAW_MONITOR_SLACK_E2E_THREAD_CHANNEL_TARGET="C0AJ3SD5C79" \
  OPENCLAW_MONITOR_CORE_MD_ENABLE=0 \
  OPENCLAW_MONITOR_THREAD_REPLY_CHECK=0 \
  OPENCLAW_MONITOR_LOCK_DIR="$LOCK_DIR" \
  OPENCLAW_MONITOR_DOCTOR_SH_ENABLE=0 \
  OPENCLAW_MONITOR_AO_DOCTOR_ENABLE=0 \
  OPENCLAW_MONITOR_INFERENCE_PROBE_ENABLE=0 \
  OPENCLAW_MONITOR_TOKEN_PROBES_ENABLE=0 \
  OPENCLAW_MONITOR_MEMORY_LOOKUP_ENABLE=0 \
  OPENCLAW_MONITOR_SLACK_READ_PROBE_ENABLE=0 \
  OPENCLAW_MONITOR_GATEWAY_PROBE_MESSAGE_ENABLE=0 \
  OPENCLAW_MONITOR_STATUS_BROADCAST_ENABLE=0 \
  OPENCLAW_MONITOR_PHASE2_ENABLE=0 \
  OPENCLAW_MONITOR_RUN_CANARY=1 \
  OPENCLAW_MONITOR_SLACK_E2E_MATRIX_ENABLE=1 \
  OPENCLAW_MONITOR_SLACK_E2E_TIMEOUT_SECONDS=2 \
  OPENCLAW_MONITOR_SLACK_E2E_POLL_INTERVAL_SECONDS=1 \
  OPENCLAW_MONITOR_WS_CHURN_THRESHOLD=999999 \
  OPENCLAW_MONITOR_LOG_FILE="$BOT_SENDER_LOG_FILE" \
  bash "$SCRIPT"

if grep -Eq 'channel_no_mention=invalid_sender_(bot|app)' "$BOT_SENDER_LOG_FILE"; then
  pass "monitor rejects bot-authored top-level no-mention sender probes"
else
  fail "monitor did not flag bot-authored top-level no-mention sender probes"
fi

for mode in thread_no_mention thread_with_mention; do
  if grep -Eq "${mode}=invalid_sender_(bot|app)" "$BOT_SENDER_LOG_FILE"; then
    pass "monitor rejects bot-authored ${mode} sender probes"
  else
    fail "monitor did not flag bot-authored ${mode} sender probes"
  fi
done

if grep -q '^STATUS=PROBLEM' "$BOT_SENDER_LOG_FILE"; then
  pass "monitor stays red when the sender surface cannot prove channel_no_mention"
else
  fail "monitor should fail closed when top-level no-mention uses a bot sender"
fi

if [[ "$FAILED" -gt 0 ]]; then
  echo ""
  echo "Artifacts: $TMP_ROOT"
  echo "FAILED: $FAILED test(s), PASSED: $PASSED"
  exit 1
fi

echo ""
echo "PASSED: $PASSED test(s)"
