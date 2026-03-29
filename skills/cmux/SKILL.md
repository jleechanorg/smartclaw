---
name: cmux
description: "Control cmux terminal multiplexer via its Unix socket API. Use when needing to: (1) List, create, select, or close workspaces; (2) Split panes and manage surfaces; (3) Send text or key presses to terminals; (4) Create notifications; (5) Set sidebar status, progress bars, or log entries; (6) Query system state. Requires cmux CLI or Unix socket at /tmp/cmux.sock. ALWAYS validate commands via cmux_validator before execution."
---

# cmux

Control cmux terminal multiplexer programmatically via its Unix socket API or CLI.

## ⚠️ Preflight Validation (REQUIRED before execution)

**Before running any cmux CLI command, you MUST validate it.**

The cmux command validator catches common mistakes before they produce silent help dumps:

```python
from orchestration.cmux_validator import validate, truncate_output

result = validate("cmux list-surfaces --workspace 23 --json")
if not result.valid:
    # Post the rejection to Slack thread immediately
    slack.post_message(channel_id, result.to_slack_message(session_id=ws))
    return  # Stop — do not proceed with invalid command
```

**Known failure modes this prevents:**
- `cmux list-surfaces --workspace 23 --json` → `--json` is not a valid flag for this command
- `cmux list-surface` → wrong subcommand (should be `list-surfaces`)
- `cmux tree` → no such command (use `cmux list-surfaces`)

## Error / stderr Handling

If cmux fails and emits a large help dump to stderr:

```python
# Always emit terminal status even on error
try:
    result = subprocess.run(...)
except Exception as exc:
    msg = f":fire: cmux error: `{exc}`"
    slack.post_message(channel_id, msg, thread_ts=thread_ts)
    return

# Truncate large output for Slack display
if len(stderr) > 2000 or stderr.count("\n") > 20:
    summary = truncate_output(stderr)
    slack.post_message(channel_id, f":warning: cmux output truncated:\n```\n{summary}\n```", ...)
```

## Socket Connection

```bash
SOCKET_PATH="${CMUX_SOCKET_PATH:-/tmp/cmux.sock}"
```

Send JSON-RPC requests:
```json
{"id":"req-1","method":"workspace.list","params":{}}
```

## CLI Quick Reference

```bash
# Output as JSON
cmux --json <command>

# Target specific workspace/surface
cmux --workspace <id> --surface <id> <command>
```

## Workspace

| Action | CLI | Socket Method |
|--------|-----|---------------|
| List all | `cmux list-workspaces` | `workspace.list` |
| Create new | `cmux new-workspace` | `workspace.create` |
| Select | `cmux select-workspace --workspace <id>` | `workspace.select` |
| Get current | `cmux current-workspace` | `workspace.current` |
| Close | `cmux close-workspace --workspace <id>` | `workspace.close` |

## Splits & Surfaces

| Action | CLI | Socket Method |
|--------|-----|---------------|
| New split | `cmux new-split <direction>` | `surface.split` (direction: left/right/up/down) |
| List surfaces | `cmux list-surfaces` | `surface.list` |
| Focus surface | `cmux focus-surface --surface <id>` | `surface.focus` |

## Input

| Action | CLI | Socket Method |
|--------|-----|---------------|
| Send text | `cmux send "echo hello"` | `surface.send_text` |
| Send key | `cmux send-key enter` | `surface.send_key` |
| Send to surface | `cmux send-surface --surface <id> "cmd"` | `surface.send_text` (with surface_id) |

Keys: `enter`, `tab`, `escape`, `backspace`, `delete`, `up`, `down`, `left`, `right`

## Notifications

```bash
cmux notify --title "Title" --body "Body"
# Socket: notification.create
```

## Sidebar Metadata

| Action | CLI | Socket Method |
|--------|-----|---------------|
| Set status | `cmux set-status <key> <value>` | (socket only) |
| Clear status | `cmux clear-status <key>` | (socket only) |
| Set progress | `cmux set-progress 0.5 --label "Building..."` | (socket only) |
| Clear progress | `cmux clear-progress` | (socket only) |
| Log entry | `cmux log "message" --level error` | (socket only) |
| Clear log | `cmux clear-log` | (socket only) |

## System

| Action | CLI | Socket Method |
|--------|-----|---------------|
| Ping | `cmux ping` | `system.ping` |
| Capabilities | `cmux capabilities` | `system.capabilities` |
| Identify context | `cmux identify` | `system.identify` |

## Python Client

```python
import json
import os
import socket

SOCKET_PATH = os.environ.get("CMUX_SOCKET_PATH", "/tmp/cmux.sock")

def rpc(method, params=None, req_id=1):
    payload = {"id": req_id, "method": method, "params": params or {}}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.connect(SOCKET_PATH)
        sock.sendall(json.dumps(payload).encode("utf-8") + b"\n")
        # Loop until newline delimiter to avoid truncating newline-delimited JSON responses.
        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            if chunk.endswith(b"\n"):
                break
        return json.loads(b"".join(chunks).decode("utf-8"))

# List workspaces
print(rpc("workspace.list", req_id="ws"))

# Send notification
print(rpc("notification.create", {"title": "Hello", "body": "From Python!"}))
```

## Shell Helper

```bash
cmux_cmd() {
    SOCK="${CMUX_SOCKET_PATH:-/tmp/cmux.sock}"
    printf "%s\n" "$1" | nc -U "$SOCK"
}

cmux_cmd '{"id":"ws","method":"workspace.list","params":{}}'
```

## Check if cmux is Available

```bash
[ -S "${CMUX_SOCKET_PATH:-/tmp/cmux.sock}" ] && echo "cmux socket available"
command -v cmux &>/dev/null && echo "cmux CLI available"
```
