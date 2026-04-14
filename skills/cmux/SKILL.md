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

## ⚠️ Status Bar Interpretation — NOT Frozen

Claude Code status bar states to interpret correctly:
- **`⏵⏵ bypass permissions on`** in status bar = normal Claude Code prompt UI, NOT a blocking dialog — Claude Code is actively working around it
- **Active churning timestamps** (e.g., "Churned for 9m 41s", "Sautéed for 2m 38s") = genuinely working workspace, NOT frozen
- **"Still running" indicator** = work in progress, not stalled
- **Idle bash shell** (fresh login prompt) = workspace is done or waiting for input, NOT frozen
- **ctx XX% progress** = active context usage, workspace is alive

**What IS actually blocked:**
- `bypass permissions on (shift+tab to cycle)` with NO active churning/time-on-task and NO progress indicator = may be a real stall
- "Index error" or "workspace unreachable" = workspace handle drift, genuinely blocked
- Shell at `claude`/`claudem` typed but no Claude Code active = shell-level stall, needs restart
- "Add a follow-up" dialog open in Composer 2 Fast = blocked on human follow-up, not frozen

**Key rule:** A workspace with an active time-on-task label ("Churned for Xm", "Crunched for Xm") is working, regardless of what the status bar shows. Only report as "frozen" when there's evidence of no activity AND no active time-on-task.

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

## ⚠️ Critical RPC Quirks (Discovered via Trial and Error)

### CLI Refs Don't Cross RPC Boundaries
The `cmux` CLI returns refs like `workspace:26` or `surface:64`, but these refs **do not work** across RPC calls — they only work within a single CLI invocation. For multi-step introspection (list → then read), use raw socket JSON-RPC via `nc` or Python.

### surface.list Ignores the workspace Parameter
**`surface.list` on ANY workspace ref returns the ACTIVE workspace's surfaces.** The workspace targeting in surface.list is non-functional. Always get the active workspace first, then list its surfaces.

### surface.read_text is Very Slow / Times Out
`surface.read_text` can take 10+ seconds on some surfaces and may time out entirely. The focused surface (surface:64) often times out; try sibling split surfaces (surface:72, surface:82) which may respond faster. Always use a 10s+ timeout when reading terminal buffer.

### Correct Inspection Order for cmux State Reports
```python
# 1. Ping first to verify socket is alive
ping = rpc("system.ping", req_id="ping")
assert ping.get("result", {}).get("pong"), "cmux socket unreachable"

# 2. List ALL workspaces (get titles, IDs, refs, pinned status)
result = rpc("workspace.list", req_id="ws")
workspaces = result["result"]["workspaces"]

# 3. Get CURRENT (active) workspace
result = rpc("workspace.current", req_id="cur")
active_ws_id = result["result"]["workspace_id"]

# 4. Verify workspace.select works — try switching and confirming
#    (if this fails, you are stuck inspecting only the active workspace)
select_r = rpc("workspace.select", {"workspace": workspaces[1]["ref"]}, req_id="sw", timeout=5)
time.sleep(1)
current = rpc("workspace.current", req_id="cur2")
if current["result"]["workspace_id"] != workspaces[1]["id"]:
    # workspace.select silently failed — document this limitation
    # Only the originally-active workspace is inspectable
    pass

# 5. List surfaces for the confirmed-active workspace
#    (surface.list ignores the workspace param — it always returns the active workspace's surfaces)
result = rpc("surface.list", {"workspace": workspaces[0]["ref"]}, req_id="sl")
surfaces = result["result"]["surfaces"]

# 6. Read text from each surface (non-focused ones first to avoid timeouts)
for surf in surfaces:
    text = rpc("surface.read_text", {
        "surface": surf["ref"],
        "surface_id": surf["id"],  # always pass surface_id too
        "lines": 50
    }, req_id=f"rt_{surf['ref']}", timeout=25)
    # Note: focused surface often returns empty content but populated text field
    # Use r["result"].get("text", "") not r["result"].get("content", [])
```

### workspace.select Silently Fails (Returns ok:true but Never Switches)
**Symptom:** `workspace.select` returns `{"ok": true}` but `workspace.current` continues returning the original workspace ID. All subsequent `surface.list` calls return the original workspace's surfaces. Switching has no effect.

**Detection:** After calling `workspace.select`, immediately call `workspace.current` and compare the returned `workspace_id` with what you selected. If they don't match, the select failed silently.

**Root cause observed:** On some cmux builds/socket configurations, `workspace.select` completes without error but does not mutate the session's active workspace context.

**Workaround:** If `workspace.select` fails, you can only inspect the currently active workspace. There is no socket-level workaround for switching to a different workspace programmatically — the cmux UI must be used manually. Alternatively, restart the cmux daemon or verify socket auth (password saved in Settings vs `CMUX_SOCKET_PASSWORD` env var).

### Ping Before Bulk Operations
cmux sockets can become unresponsive. Always `system.ping` first to verify connectivity before issuing multiple RPC calls.

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

**Note:** CLI introspection (list-workspaces, list-surfaces) is fine for single queries but breaks when chaining into RPC. Use `nc` raw socket for multi-step workflows.

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

def rpc(method, params=None, req_id=1, timeout=10):
    """Send JSON-RPC to cmux socket. Use timeout=10+ for surface.read_text."""
    payload = {"id": req_id, "method": method, "params": params or {}}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(SOCKET_PATH)
        sock.sendall(json.dumps(payload).encode("utf-8") + b"\n")
        data = b""
        while True:
            try:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                data += chunk
                if b"\n" in data:
                    break
            except socket.timeout:
                return {"error": "timed out"}
        return json.loads(data.decode("utf-8"))

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
