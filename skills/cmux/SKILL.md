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
- `cmux system.tree` → no such command (use `cmux tree --all`)
- `cmux tree` without `--all` → returns only active window content; use `cmux tree --all` for full multi-window tree

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

### CLI Can Return Workspace IDs Instead of Response Content (text-RPC Stripping)
**Symptom:** `cmux <socket> workspace.list` (or any CLI command) returns `OK workspace:22` — a workspace *ID reference* — instead of actual JSON response data. The CLI is using text-RPC internally and its output parser strips the JSON body, showing only the message ID.

**This is NOT a socket failure.** The socket is fine (ping works). The CLI's output formatting is the problem.

**Workaround:** Use `nc` (netcat) to send raw JSON-RPC directly to the socket. This bypasses the CLI's output formatter entirely.

```bash
# CORRECT — raw JSON-RPC via nc
echo '{"jsonrpc":"2.0","method":"workspace.list","params":{},"id":1}' | nc -w 1 -U /private/tmp/cmux-debug-appclick.sock

# WRONG — CLI output parser strips response body
cmux /private/tmp/cmux-debug-appclick.sock workspace.list
# Returns: OK workspace:22  (message ID only, no actual data)
```

**Socket discovery first, then nc:**
```bash
# 1. Discover socket path (CLI works fine for this)
ls -1 /private/tmp/cmux*.sock /tmp/cmux*.sock 2>/dev/null

# 2. Ping to verify socket is alive (CLI works for this)
cmux $SOCKET_PATH ping
# Returns: OK (or actual pong) — confirms socket is up

# 3. Use nc for actual data queries (CLI output may be stripped)
echo '{"jsonrpc":"2.0","method":"workspace.list","params":{},"id":1}' | nc -w 1 -U $SOCKET_PATH
# NOTE: There is NO "system.tree" JSON-RPC method. Use CLI for tree: cmux tree --all
```

**Why `-w 1` timeout is critical on macOS:** macOS `nc` uses `-w timeout_secs` (not `-W` which is Linux). Omitting `-w` causes nc to hang indefinitely on unresponsive sockets.

### Multiple Sockets Can Exist — Probe All of Them
On a given machine, multiple cmux socket paths may exist simultaneously:
```bash
# Common paths observed on macOS:
ls -1 \
  ~/Library/Application\ Support/cmux/cmux.sock \
  /private/tmp/cmux-debug-appclick.sock \
  /tmp/cmux-debug-appclick.sock \
  2>/dev/null
```

**A socket file existing does NOT mean it is the active socket.** The production socket (`~/Library/Application Support/cmux/cmux.sock`) may return `{"windows":[],"active":null}` or `"TabManager not available"` while a debug socket has all the real workspace data.

**Always probe ALL discovered sockets with `system.ping` via `nc`** before assuming a socket is the right one:
```bash
for sock in \
  ~/Library/Application\ Support/cmux/cmux.sock \
  /private/tmp/cmux-debug-appclick.sock \
  /tmp/cmux-debug-appclick.sock; do
  result=$(echo '{"jsonrpc":"2.0","method":"system.tree","id":"tree"}' | nc -w 1 -U "$sock" 2>&1)
  if echo "$result" | grep -q '"windows"'; then
    echo "ACTIVE SOCKET: $sock"
    break
  else
    echo "EMPTY/INACTIVE: $sock"
  fi
done
```

**2026-04-30 finding:** The production socket returned `TabManager not available` for `workspace.list` and empty windows in `system.tree`, while `/private/tmp/cmux-debug-appclick.sock` had all 6 workspaces with full data. Use `system.identify` on the working socket to confirm which one cmux.app is actually using.

**Parsing nc output in Python:**
```python
import subprocess, json
result = subprocess.run(
    ['nc', '-w', '1', '-U', '/private/tmp/cmux-debug-appclick.sock'],
    input='{"jsonrpc":"2.0","method":"workspace.list","params":{},"id":1}\n',
    capture_output=True, text=True
)
data = json.loads(result.stdout)
workspaces = data['result']['workspaces']
```

### CLI Refs Don't Cross RPC Boundaries
The `cmux` CLI returns refs like `workspace:26` or `surface:64`, but these refs **do not work** across RPC calls — they only work within a single CLI invocation. For multi-step introspection (list → then read), use raw socket JSON-RPC via `nc` or Python.

### surface.list Ignores the workspace Parameter
**`surface.list` on ANY workspace ref returns the ACTIVE workspace's surfaces.** The workspace targeting in surface.list is non-functional. Always get the active workspace first, then list its surfaces.

### surface.read_text is Very Slow / Times Out
`surface.read_text` can take 10+ seconds on some surfaces and may time out entirely. The focused surface (surface:64) often times out; try sibling split surfaces (surface:72, surface:82) which may respond faster. Always use a 10s+ timeout when reading terminal buffer.

### Reading Terminal Content — Which Command Works Depends on Window/Surface State

When reading terminal content from workspaces in **secondary windows** (e.g., window:2), `capture-pane --surface $surf` alone often returns empty output even when the terminal has content. Two patterns that work:

**Pattern A — Full ref syntax with `--workspace` flag:**
```bash
# The key: use BOTH --workspace AND --surface with full refs
cmux capture-pane --workspace workspace:33 --surface surface:71 --scrollback --lines 80
# vs this which returns empty on secondary-window surfaces:
cmux capture-pane --surface surface:71 --scrollback --lines 80
```

**Pattern B — `read-screen` instead of `capture-pane`:**
```bash
# read-screen works where capture-pane returns empty for secondary-window surfaces
cmux read-screen --workspace workspace:33 --surface surface:71 --scrollback --lines 100 | tail -50
```

**Correct `tree` command syntax (DISCOVERED 2026-04-29):**
The RPC method is `system.tree` but the CLI subcommand is `tree` (not `system.tree`). The `--workspace` flag targets a specific workspace:
```bash
export CMUX_SOCKET_PATH=/private/tmp/cmux-debug-appclick.sock

# CORRECT — tree per workspace (CLI subcommand, NOT "system.tree")
cmux tree --workspace workspace:1
cmux tree --workspace workspace:5

# WRONG — these return help or empty output:
cmux run-command "system.tree" --workspace workspace:1  # returns help text
cmux rpc tree '{"workspace":"workspace:1"}'              # returns exit code 1, no output

# Also useful — surface health per workspace:
cmux surface-health --workspace workspace:5
# Returns: surface:54  type=terminal in_window=true

# Read terminal content from a specific surface:
cmux read-screen --workspace workspace:5 --surface surface:54 --lines 30
cmux read-screen --workspace workspace:5 --surface surface:54 --scrollback --lines 60 | tail -80
```

**Practical inspection workflow (2026-04-22, updated 2026-04-29):**
```bash
# 1. Discover socket paths (CLI works fine for this)
ls -1 /private/tmp/cmux*.sock /tmp/cmux*.sock 2>/dev/null

# 2. Ping to verify socket is alive
cmux $SOCKET_PATH ping
# Returns: PONG

# 3. List all workspaces (names + refs, shows pinned status with asterisk)
cmux list-workspaces

# 4. Get tree per workspace (the correct per-workspace introspection):
for ws in workspace:1 workspace:2 workspace:3 workspace:4 workspace:5 workspace:6; do
  echo "=== $ws ==="
  cmux tree --workspace $ws 2>/dev/null
done

# 5. For each surface of interest, read terminal content:
cmux read-screen --workspace workspace:5 --surface surface:54 --lines 30

# 6. Surface health (shows if surface is in a live window):
cmux surface-health --workspace workspace:5

# Note: Most surfaces idle at login prompt — only surfaces with active agents have content.
# Active surfaces often show churning timestamps, ctx XX%, bypass permissions, etc.
```

**Why this matters:** When workspaces are spread across multiple windows, surfaces in window:2 often don't respond to bare `capture-pane --surface N` but do respond to `read-screen --workspace W --surface S` or `capture-pane --workspace W --surface S` with both refs specified.

### Correct Inspection Order for cmux Status Reports (NO workspace.select)

**CRITICAL: `workspace.select` is forbidden** for cmux status/inspection tasks. Only `system.tree` (via `cmux tree --all`) may be used to enumerate workspace state. Use `read-screen` or `capture-pane` with full `--workspace` and `--surface` refs for content inspection.

```python
# 1. Ping first to verify socket is alive
ping = rpc("system.ping", req_id="ping")
assert ping.get("result", {}).get("pong"), "cmux socket unreachable"

# 2. Get full tree — shows ALL windows, workspaces, panes, surfaces in one call
result = rpc("system.tree", req_id="tree")
# Output: {ok: true, result: {windows: [{id, workspaces: [{id, name, ref, windows: ...}]}]}}
# Active window has [current] marker. Active workspace has [selected] marker.

# 3. For each workspace of interest, check which surfaces are accessible
#    BEFORE trying read-screen, run surface-health to skip in_window=false surfaces:
for ws_ref in ["workspace:3", "workspace:4", "workspace:7", "workspace:9"]:
    health = rpc("surface.health", {"workspace": ws_ref}, req_id=f"sh_{ws_ref}")
    for surf in health.get("result", {}).get("surfaces", []):
        if not surf.get("in_window", True):
            # SKIP — surface is orphaned, read-screen will fail with "Terminal surface not found"
            continue
        # This surface is readable — try read-screen with full refs
        text = rpc("surface.read_text", {
            "surface": surf["ref"],       # e.g. "surface:5"
            "surface_id": surf["id"],     # full UUID
            "lines": 50
        }, req_id=f"rt_{surf['ref']}", timeout=25)

# 4. Active workspace: use read-screen with --workspace + --surface refs on the selected surface
#    (workspace:1 surface:2 is typically the active terminal shown in identify output)
```

### workspace.select Requires workspace_id (UUID), NOT workspace Ref
**Symptom:** `workspace.select` returns `{"ok": false, "error": {"code": "invalid_params", "message": "Missing or invalid workspace_id"}}`.

**Fix:** Use the full workspace UUID (e.g. `"A6789942-5882-4037-952D-31AF63D02664"`), NOT the `workspace:N` ref string. The ref is for display; the UUID is what the RPC accepts.

```python
# WRONG ❌
rpc("workspace.select", {"workspace": "workspace:4"})

# CORRECT ✅
rpc("workspace.select", {"workspace_id": "A6789942-5882-4037-952D-31AF63D02664"})
```

**Socket path:** Use `/tmp/cmux-debug-appclick.sock` (not `/private/tmp/...` which may also exist but is not the active socket). Confirm via `system.identify` → `result.socket_path`.

### Socket Exists But Connection Refused (ECONNREFUSED)
**Symptom:** The socket file exists (`ls -la /tmp/cmux-debug-appclick.sock` shows `srw-rw-rw-`) but Python `sock.connect()` or `nc -U` raises `ConnectionRefusedError` or hangs.

**Root cause:** The cmux.app process is running but was NOT started with debug mode enabled. The socket file on disk is a leftover from a previous debug session that ended abnormally (crash, kill -9, etc.). The current cmux process is not listening on it.

**Diagnosis checklist:**
```bash
# 1. Confirm socket file exists
ls -la /tmp/cmux-debug-appclick.sock /private/tmp/cmux-debug-appclick.sock

# 2. Check if cmux process is running (it may be, but not listening)
pgrep -fl cmux

# 3. Check what file descriptors the cmux process actually has open
lsof -p <cmux_pid> 2>/dev/null | grep -E "socket|IPv|TCP"

# If lsof shows no socket FDs for the cmux process → debug socket not enabled
# If lsof shows socket FDs but on a different path → socket path mismatch
```

**Fix:** The cmux.app must be relaunched with debug mode to create the socket. There is no runtime way to enable the debug socket without restarting cmux.

**Reporting pattern for cron jobs:** When socket is down, post a status report to Slack noting `🔴 SOCKET DOWN — connection refused` and include the last known workspace state from the previous successful run. Do not repeatedly retry the same socket call.

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

### ⚠️ GUARDRAIL — `send` Does NOT Submit: ALWAYS Follow with Enter

**`cmux send` types text into the terminal buffer but does NOT press Enter.** The text sits at the prompt unexecuted. This is the #1 failure mode when steering terminals remotely.

**EVERY time you call `cmux send`, you MUST immediately follow with `cmux send-key enter`** or the command will never execute:

```bash
# WRONG ❌ — text typed but never submitted
cmux send --workspace workspace:1 --surface surface:1 "git status"
# Terminal shows "git status" but nothing happens

# CORRECT ✅ — text typed AND submitted
cmux send --workspace workspace:1 --surface surface:1 "git status"
cmux send-key --workspace workspace:1 --surface surface:1 "enter"
# Command executes, output appears in terminal
```

**Multi-line commands:** Send each line separately, with Enter after each:
```bash
cmux send --workspace $ws --surface $surf "git add -A"
cmux send-key --workspace $ws --surface $surf "enter"
cmux send --workspace $ws --surface $surf "git commit -m 'fix: description'"
cmux send-key --workspace $ws --surface $surf "enter"
cmux send --workspace $ws --surface $surf "git push"
cmux send-key --workspace $ws --surface $surf "enter"
```

**Verification pattern:** After sending + enter, wait 3-5s then `capture-pane` to confirm the command executed and output appeared.

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
| Workspace tree | `cmux tree --workspace <id>` | `system.tree` |
| Surface health | `cmux surface-health --workspace <id>` | (socket only) |

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
cmux_rpc() {
    # Send raw JSON-RPC to cmux socket via nc (bypasses CLI output formatter)
    # Args: method params_json
    SOCK="${CMUX_SOCKET_PATH:-/private/tmp/cmux-debug-appclick.sock}"
    METHOD="$1"
    PARAMS="${2:-{}}"
    printf '{"jsonrpc":"2.0","method":"%s","params":%s,"id":1}\n' "$METHOD" "$PARAMS" | nc -w 1 -U "$SOCK"
}

# Examples:
cmux_rpc "workspace.list" "{}"
cmux_rpc "system.tree" "{}"
cmux_rpc "surface.read" "{\"surface_id\":\"<id>\"}"
```

**Why nc instead of cmux CLI:** The cmux CLI uses text-RPC which strips response bodies — `cmux workspace.list` returns `OK workspace:22` (message ID) instead of actual data. Use `nc` for queries, `cmux` CLI only for ping/capabilities/discovery.

**`system.tree` vs `tree --all`:** Do NOT use `cmux system.tree` as a CLI command — `system.tree` is a JSON-RPC method for `nc`/socket use only. The correct CLI command for full workspace hierarchy is `cmux tree --all`. Both produce similar output; `tree --all` is what the cmux CLI accepts.

## Check if cmux is Available

```bash
[ -S "${CMUX_SOCKET_PATH:-/tmp/cmux.sock}" ] && echo "cmux socket available"
command -v cmux &>/dev/null && echo "cmux CLI available"
```

---

## Skill Completeness (skillify 10-item)

| # | Item | Status | Notes |
|---|---|---|---|
| 1 | SKILL.md | ✅ | Present |
| 2 | Code | ✅ | `scripts/cmux_client.py` |
| 3 | Unit tests | ✅ | `tests/test_cmux_client.py` — 6 tests, all green |
| 4 | Integration tests | ✅ | `tests/test_cmux_integration.py` — live socket, JSON-RPC roundtrip |
| 5 | LLM evals | N/A | No LLM calls in this skill |
| 6 | Resolver trigger | ✅ | `skills/RESOLVER.md` — "cmux" entry |
| 7 | Resolver trigger eval | ✅ | `tests/test_skillify_resolver_trigger.py` — cmux routing verified |
| 8 | check-resolvable | ✅ | `test_skill_tree_resolvable` — all RESOLVER.md refs valid |
| 9 | E2E test | ✅ | `tests/test_cmux_integration.py` — full socket roundtrip |
| 10 | Brain filing | N/A | No brain pages written |

**E2E / Integration test target:** Live production socket at `~/Library/Application Support/cmux/cmux.sock` (PID 626, `cmux.app`). Tests use `socket_path = os.path.expanduser("~/Library/Application Support/cmux/cmux.sock")` with fallback to `CMUX_SOCKET_PATH` env var.

**Running E2E:**
```bash
python -m pytest tests/test_cmux_integration.py -v
```
