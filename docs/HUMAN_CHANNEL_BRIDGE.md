# Human Channel Bridge

## Overview

The Human Channel Bridge mirrors AO worker lifecycle events into Slack channel `C0ANK6HFW66` with human-readable messages and thread support. It also includes a startup health check for the MCP mail listener and a worker heartbeat mechanism.

**Source:** `src/orchestration/human_channel_bridge.py`
**Tests:** `src/tests/test_human_channel_bridge.py`
**Launcher:** `scripts/run-human-channel-bridge.sh`
**Launchd:** `launchd/ai.smartclaw.human-channel-bridge.plist.template`

---

## Architecture

```
AO Session Lifecycle (every poll)
  └── session metadata files: ~/.agent-orchestrator/{hash}-{project}/sessions/{session-id}
        ↓
  human_channel_bridge (every 60s)
        ├── Health check: MCP mail server alive?
        ├── Session scan: detect spawn / exit transitions
        ├── Slack post: human-readable message (threaded)
        └── Channel reader: contextual replies to human questions
```

The bridge is **read-only** from the AO's perspective — it only reads session metadata files. It maintains a state file to track which sessions have already been reported, preventing duplicate posts across polling cycles.

### AO Lifecycle → Slack Mapping

| AO Event | Slack Message |
|---|---|
| Session → `spawning` | "AO worker *{id}* just picked up a new ticket..." |
| Session → `done`/`killed`/`merged` | "AO worker *{id}* finished up and is heading out..." |
| (periodic heartbeat) | "AO workers still alive: `jc-613`, `jc-614`..." |

### MCP Mail Thread Sync (AO Config)

The AO `agent-orchestrator.yaml` adds `session-spawned` and `session-exited` reactions that route to `mcp-mail` via the notifier system, keeping inter-agent mcp-mail threads in sync with AO lifecycle events. See `agent-orchestrator.yaml → reactions:`.

---

## Configuration

| Environment Variable | Default | Description |
|---|---|---|
| `BRIDGE_ENABLED` | `true` | Enable/disable bridge Slack posts |
| `BRIDGE_HEALTH_CHECK_ENABLED` | `true` | Enable/disable MCP mail health check |
| `HUMAN_CHANNEL_ID` | `C0ANK6HFW66` | Slack channel for human-audience posts |
| `SLACK_BOT_TOKEN` | (required) | Slack bot token for posting |
| `MCP_AGENT_MAIL_URL` | `http://127.0.0.1:18789/mcp` | MCP mail server health check URL |
| `SLACK_POST_COOLDOWN_SECONDS` | `30` | Cooldown between Slack posts |
| `BRIDGE_STATE_FILE` | `~/.smartclaw/state/human_channel_bridge.json` | Persistent state file |
| `AO_DATA_DIR` | `~/.agent-orchestrator` | AO session data directory |

---

## Entry Points

```bash
# Full bridge: health check + session scan + Slack posts + channel reader
./scripts/run-human-channel-bridge.sh

# Health check only (exit 0 = healthy, exit 1 = unhealthy)
./scripts/run-human-channel-bridge.sh health

# Heartbeat only
./scripts/run-human-channel-bridge.sh heartbeat
```

---

## Safety Guardrails

| Guardrail | Mechanism |
|---|---|
| **Duplicate prevention** | State file tracks `reported_spawns` and `reported_exits` — each session is posted at most once |
| **Threading** | Exit messages reply in the same Slack thread as the spawn message (via `thread_ts`) |
| **Cooldown** | `SLACK_POST_COOLDOWN_SECONDS=30` prevents rapid re-posts |
| **Bot filter** | Channel reader only replies to non-bot users |
| **Rate limit** | Max 3 contextual replies per poll cycle |
| **MCP mail ack intact** | No changes to mcp-mail plugin; AO notifier routes unchanged for worker-initiated messages |
| **Disable flag** | `BRIDGE_ENABLED=0` skips all Slack operations |

---

## Failure Modes

| Failure | Behavior | Detection |
|---|---|---|
| MCP mail server down | Health check returns exit 1; bridge continues (logs warning) | Startup health check via launchd |
| Slack token missing | Bridge skips posting, logs error | Exit code 1 |
| Slack API error | `post_message` returns `None`; bridge continues | Logged at WARN level |
| AO data dir missing | `scan_ao_sessions` returns empty dict; no posts | Logged at INFO level |
| Bridge stuck / duplicate posts | State file corruption → `load_state` returns empty state on parse error; deduplication resets | Manual inspection of state file |

---

## MCP Mail Listener Health Check

The health check (`health_check_main`) probes `MCP_AGENT_MAIL_URL/health`. If the server returns HTTP 200, it exits 0. Otherwise it exits 1 and logs the error.

This check is run at bridge startup and also exposed as a standalone entry point for use in monitoring / startup scripts.

---

## Worker Mailbox Heartbeat

The heartbeat (`run_heartbeat`) posts a message listing all currently alive AO workers. It is invoked:
- As a standalone entry point: `./scripts/run-human-channel-bridge.sh heartbeat`

> Note: Heartbeat is not yet wired into the automatic bridge run. The standalone invocation above can be scheduled separately.

Workers are considered "alive" if their session status is in `ACTIVE_STATUSES` or `SPAWNING_STATUS`.

---

## AO Config Changes

Two changes to `agent-orchestrator.yaml`:

### 1. Session Lifecycle Events Extended

In `lifecycle-manager.ts` (agent-orchestrator):
- Added `spawning → session.spawned` in `statusToEventType()`
- Added `terminated → session.exited` in `statusToEventType()`
- Added `session.spawned → session-spawned` and `session.exited → session-exited` in `eventToReactionKey()`

These changes require a rebuild of the agent-orchestrator package (`npm run build` in `~/project_agento/agent-orchestrator`).

### 2. New Reactions in `agent-orchestrator.yaml`

```yaml
reactions:
  session-spawned:
    auto: false        # workers send their own mcp-mail; this routes to mcp-mail notifier
    action: notify
    priority: action
    message: "AO worker {{sessionId}} started — project {{projectId}}. Routing to mcp-mail thread."

  session-exited:
    auto: true
    action: notify
    priority: urgent
    retries: 3
    escalateAfter: 2
    message: "AO worker {{sessionId}} exited — project {{projectId}}. Routing to mcp-mail thread for session reconciliation."

  # Also added mcp-mail notifier and routing:
notifiers:
  mcp-mail:
    plugin: mcp-mail
    endpoint: "${MCP_AGENT_MAIL_URL:-http://127.0.0.1:18789/mcp}"
    projectId: smartclaw
    agentId: ao-lifecycle
    to: ["jleechan"]

notificationRouting:
  urgent: [openclaw, orchestrator, slack, mcp-mail]
  action: [openclaw, orchestrator, slack, mcp-mail]
```

---

## Launchd Installation

The bridge runs as a launchd agent (LaunchAgent) on macOS. Use the install script which handles token substitution:

```bash
# Install all launchd agents (including human-channel-bridge)
bash scripts/install-launchagents.sh

# Or install just the bridge:
bash scripts/install-launchagents.sh human-channel-bridge

# Load after install:
launchctl load ~/Library/LaunchAgents/ai.smartclaw.human-channel-bridge.plist

# Unload:
launchctl unload ~/Library/LaunchAgents/ai.smartclaw.human-channel-bridge.plist
```

Required env vars in the plist: `SLACK_BOT_TOKEN`, `MCP_AGENT_MAIL_URL`.

---

## Testing

```bash
# Unit tests (mocked Slack, real session file parsing)
PYTHONPATH=src python -m pytest src/tests/test_human_channel_bridge.py -v

# Smoke test (no Slack posting)
BRIDGE_ENABLED=false \
  SLACK_BOT_TOKEN="" \
  MCP_AGENT_MAIL_URL=http://127.0.0.1:18789/mcp \
  PYTHONPATH=src \
  python3 -c "from src.orchestration.human_channel_bridge import health_check_main; print(health_check_main())"
# Expected: "OK — MCP mail listener responding" exit=0
```

---

## Files

| File | Purpose |
|---|---|
| `src/orchestration/human_channel_bridge.py` | Main bridge logic |
| `src/tests/test_human_channel_bridge.py` | 38 unit tests |
| `scripts/run-human-channel-bridge.sh` | Launcher script |
| `launchd/ai.smartclaw.human-channel-bridge.plist.template` | Launchd plist template |
| `agent-orchestrator.yaml` | AO config (reactions + mcp-mail notifier) |
| `docs/HUMAN_CHANNEL_BRIDGE.md` | This file |
