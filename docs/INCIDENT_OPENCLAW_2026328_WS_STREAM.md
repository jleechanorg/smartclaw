# Incident: OpenClaw 2026.3.28 ws-stream Protocol Regression

**Date:** 2026-03-29
**Severity:** P0 (Slack reply drops — user-visible)
**Status:** Resolved (downgraded to 2026.3.24)
**Bead:** orch-420

## Summary

OpenClaw 2026.3.28 introduced a protocol version mismatch in the embedded agent WebSocket stream (`ws-stream`) that caused all WebSocket upgrade attempts to fail with HTTP 500. The constant retry storm (367 failed connections/day) starved the Node.js event loop, causing Slack Socket Mode pong timeouts and dropped Slack replies.

## Timeline

| Time (Pacific) | Event |
|---|---|
| ~14:11 | ws-stream 500 errors begin (first observed in gateway log) |
| ~14:54 | OpenClaw package modified (upgrade to 2026.3.28 completed) |
| 16:58:35 | Slack Socket Mode pong timeout — WebSocket declared dead |
| 16:58:37 | 4x "Failed to send a message as the client has no active connection" |
| 16:58:40 | User message `p1774828720475069` in #ai-slack-test gets :eyes: but no reply |
| 16:59:48 | Gateway auto-restarted (PID 93058 → new PID) |
| 17:02-17:10 | ws-stream 500s and pong timeouts continue post-restart |
| ~17:23 | better-sqlite3 rebuilt for Node 24 (wrong — gateway uses Node 22) |
| ~17:30 | Attempted `checkCompatibility: false` at gateway/agents level — crashed gateway (unrecognized key) |
| ~17:32 | Gateway recovered under `com.smartclaw.gateway` plist |
| ~17:45 | Downgraded to OpenClaw 2026.3.24 |
| ~17:50 | better-sqlite3 rebuilt for Node 22 (correct target) |
| 18:15 | Gateway restarted with 2026.3.24 |
| 18:25 | First successful Slack reply verified |
| 18:31 | Second Slack reply verified — incident resolved |

## Root Cause

### Primary: @agentclientprotocol/sdk version mismatch

OpenClaw 2026.3.28 upgraded `@agentclientprotocol/sdk` from **0.16.1** to **0.17.0**.

- Server protocol version: **1.17.0** (from SDK 0.17.0)
- Embedded agent client protocol: **1.13.0** (from the Codex Responses API stream)
- Server check: "Major versions should match and minor version difference must not exceed 1"
- 1.17 - 1.13 = 4 → rejected

The server returns HTTP 500 on the WebSocket upgrade. The client retries indefinitely (no backoff cap visible).

### Secondary: Retry storm → event loop starvation → Socket Mode death

Each ws-stream 500 retry involves:
1. WebSocket upgrade attempt (TCP + HTTP upgrade)
2. Server processes upgrade, checks version, rejects with 500
3. Error logged (structured JSON, ~500 bytes per entry)
4. Client schedules retry (5-15 second interval)

With 41 unique sessions retrying, this generated ~320 failed connections. The synchronous error handling and logging consumed enough event loop time to miss Slack's 5-second pong deadline, causing Socket Mode disconnects.

### Contributing: mem0 better-sqlite3 NODE_MODULE_VERSION mismatch

The `com.smartclaw.gateway` plist (created by `openclaw doctor`) uses **nvm's Node 22** (`${HOME}/.nvm/versions/node/v22.22.0/bin/node`, MODULE_VERSION 127). The better-sqlite3 native module was compiled for homebrew's Node 24 (MODULE_VERSION 137), causing mem0 recall/capture failures. This added error logging load to the event loop.

## Resolution

1. **Downgraded OpenClaw**: 2026.3.28 → 2026.3.24 via `/opt/homebrew/bin/npm i -g openclaw@2026.3.24`
2. **Rebuilt better-sqlite3**: Using nvm's Node 22 node-gyp (`${HOME}/.nvm/versions/node/v22.22.0/bin/npx --yes node-gyp rebuild`)
3. **Updated config**: `meta.lastTouchedVersion` → `2026.3.24` in openclaw.json
4. **Plist cleanup**: Removed broken `ai.smartclaw.gateway` plist, kept `com.smartclaw.gateway`

### Verification

| Metric | Before (2026.3.28) | After (2026.3.24) |
|---|---|---|
| ws-stream 500 errors | 367/day | 0 |
| Slack Socket Mode pong timeouts | Every 20-30s | Occasional (~1-2/min) |
| Slack reply delivery | Failed | Working |
| mem0 recall/capture | Failed (MODULE_VERSION) | Working |

## Lessons Learned

1. **Always check SDK dependency versions before upgrading openclaw**: `npm view openclaw@<version> dependencies | grep agentclientprotocol`
2. **`checkCompatibility` is NOT a valid openclaw.json key** at gateway or agents.defaults level — it only exists in the mem0 vectorStore config. Setting it crashes the gateway on config reload.
3. **The gateway plist Node binary matters for native modules**: `openclaw doctor` creates a plist using whatever `node` is in PATH at install time. After switching Node versions, the plist may point to a different Node than expected. Always check: `launchctl print gui/$(id -u)/com.smartclaw.gateway | grep program`
4. **npm rebuild doesn't always recompile**: When the prebuilt binary doesn't match, `npm rebuild` may download another prebuilt instead of compiling from source. Use `node-gyp rebuild` directly.
5. **ws-stream retries don't stop after session completion**: Session `dc1fa192` completed at 14:49 but its ws-stream retry loop continued indefinitely — this is an upstream bug in OpenClaw.

## Prevention

- `gateway-preflight.sh` should verify SDK version compatibility before upgrades
- A pre-start hook (tracked as orch-yps) should auto-rebuild native modules when NODE_MODULE_VERSION changes
- Monitor agent should alert on ws-stream 500 rates exceeding a threshold
