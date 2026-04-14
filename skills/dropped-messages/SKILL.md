---
name: dropped-messages
description: "Diagnose and recover dropped Jeffrey messages — threads or standalone messages that got no response within 30 min. Use when Jeffrey asks about missed messages, the script reports drops, or you need to understand why a message was unanswered."
---

# Dropped Messages — Diagnosis & Recovery

## Operator contract (harness clarity)

- **Not config drift:** Changes to this script’s heuristics live in **`scripts/dropped-thread-followup.sh`** (git-tracked). They do **not** rewrite **`openclaw.json`**, **`~/.cursor/mcp.json`**, or Slack tokens unless an operator edits those files.
- **Overrides:** Prefix the script with env vars (`DROP_LOOKBACK_HOURS`, `DROP_THREAD_REPLY_LIMIT`, `DROP_EXCLUDE_CHANNELS`, `DROP_JEFFREY_ONLY_CHANNELS`, …). LaunchAgent / plist can set them for stable “my settings.”
- **Audit trail:** `git log -p -- scripts/dropped-thread-followup.sh` is the source of truth for why default behavior changed.

## When to invoke this skill

- Jeffrey asks "why didn't you respond to X?" or "did you miss my message?"
- The dropped-thread-followup script found drops (actioned > 0)
- You want to proactively audit for coverage gaps
- A thread shows a very long gap before the agent replied

## Coverage model

The script `~/.smartclaw/scripts/dropped-thread-followup.sh` monitors two classes:

### Class A — Threaded (reply_count > 0)
Messages that started a thread. These ARE visible to the script. A drop occurs when:
- Jeffrey's message is the **last** in the thread with no bot follow-up (≥30 min)
- Thread went cold: >2h old, no result indicators (PR link, "done", etc.)
- Bot admitted it didn't execute ("did not execute", "only sent an ack", etc.)
- **Bot replied with a timeout/overload error** (`request timed out`, `timed out before`, `deadline exceeded`, `high load` / 2064-style messages) — counted as **`timeout-failure`** (dropped run), not a resolved answer

### Class B — Standalone (reply_count == 0)
Messages Jeffrey posted without any thread forming. These were **invisible** before the fix. A drop occurs when:
- Jeffrey posted a root message
- Bot added only a reaction (👀) but no text reply in the channel within 30 min
- Message is >30 min old

### Class C — DM channel
`${SLACK_CHANNEL_ID}` is not in `openclaw.json` channel maps; the script appends `$JLEECHAN_DM_CHANNEL` (default `${SLACK_CHANNEL_ID}`) to the scan list after resolving C-channels.

### Class D — `#all-jleechan-ai` (`${SLACK_CHANNEL_ID}`)
Not in the default C-channel fallback (`${SLACK_CHANNEL_ID}` + `C0AJQ5M0A0Y`); add IDs via `openclaw.json` or `DROP_CHANNELS`. When `${SLACK_CHANNEL_ID}` is in the resolved channel list, it is **skipped** unless you override `DROP_EXCLUDE_CHANNELS` (default is that ID, to avoid nudging a high-churn channel). Clearing exclusions: `DROP_EXCLUDE_CHANNELS=""` excludes **nothing** (the script handles empty explicitly, unlike bash `:-`).

## Quick diagnosis steps

### 1. Run dry-mode audit (non-destructive)
```bash
DRY_RUN=1 DROP_LOOKBACK_HOURS=48 bash ~/.smartclaw/scripts/dropped-thread-followup.sh
```
Look for `DRY_RUN: would nudge` lines. Each is a **candidate** drop (review for false positives, e.g. boilerplate intros).

### 1b. Include `#all-jleechan-ai` in the audit
```bash
DRY_RUN=1 DROP_LOOKBACK_HOURS=48 \
  DROP_CHANNELS="${SLACK_CHANNEL_ID} C0AJQ5M0A0Y ${SLACK_CHANNEL_ID}" DROP_EXCLUDE_CHANNELS="" \
  bash ~/.smartclaw/scripts/dropped-thread-followup.sh
```

### 2. Check DM channel specifically
```bash
DRY_RUN=1 DROP_CHANNELS="${SLACK_CHANNEL_ID}" DROP_LOOKBACK_HOURS=48 \
  bash ~/.smartclaw/scripts/dropped-thread-followup.sh
```

### 3. Manual Slack scan (when script can't reach a channel)
Use MCP: `mcp__slack__conversations_history(channel_id="${SLACK_CHANNEL_ID}", limit="2d")`
Filter for rows where UserID=U09GH5BR3QU (Jeffrey) and ThreadTs==MsgID (root messages).

### 4. Check session lock drops (silent HTTP 200 drops)
```bash
grep "session file locked" ~/.smartclaw/logs/gateway.err.log | tail -20
```
If found: the gateway processed the HTTP request but silently dropped the message before
the agent saw it. Fix: clear stale `.lock` files and restart gateway.

## Root causes & fixes

| Symptom | Root cause | Fix |
|---------|-----------|-----|
| DM never scanned | D-prefix not in resolve_channels() | Set `$JLEECHAN_DM_CHANNEL` (hardcoded ${SLACK_CHANNEL_ID} fallback) |
| Standalone msg missed | reply_count==0 invisible to thread scan | fetch_standalone_user_messages() — now active |
| Thread replied with timeout | Gateway timeout (timeoutSeconds too low) | Keep timeoutSeconds≤600, maxConcurrent≤3 |
| Session lock | Dead PID holding .lock file | Clear locks + restart (see CLAUDE.md gateway section) |
| Work done, no ack | Agent completed task but didn't post thread reply | Always post in-thread ack after completing work |
| Script LOOKBACK too short | Default 8h window misses older drops | Set DROP_LOOKBACK_HOURS=48 for full audits |
| `#all-jleechan` never audited | Not in default fallback; also excluded when listed | `DROP_CHANNELS` including `${SLACK_CHANNEL_ID}` and `DROP_EXCLUDE_CHANNELS=""` |
| Long threads misclassified | Default was 20 replies fetched | `DROP_THREAD_REPLY_LIMIT` (default **200**) |
| Bare word “done” suppressed nudges | Substring `done` matched tiny replies | Strong phrases + weak words only if agent text is **≥80** chars |
| Bug hunt / automation noise | Root posts look like unanswered work | Roots matching `*Daily Bug Hunt*`, `*Repos scanned:*`, etc. skip cold/stale |
| `#all-jleechan` teammate threads | Nudges on threads Jeffrey never joined | `DROP_JEFFREY_ONLY_CHANNELS` (default `${SLACK_CHANNEL_ID}`); set to `""` to disable |
| Nudge cites wrong line | Used first human message (often boilerplate) | Nudge **Original request** prefers Jeffrey’s **latest** message, else latest human |

## Recovery workflow

When you find a dropped thread, reply in-thread immediately:
1. Acknowledge: "I missed this message at [time] — sorry for the delay."
2. Do the work OR explain the blocker
3. Post proof (PR link / commit / result)
4. Record nudge to prevent double-nudging: the script handles this via state file

## Preventing future drops

The cron for `dropped-thread-followup.sh` runs every 4h by default. After this fix:
- DM channel is always scanned
- Standalone messages are caught if bot doesn't reply within 30 min
- The state file `~/.smartclaw/logs/dropped-thread-state.json` tracks nudged threads

To verify the cron is wired:
```bash
grep -r "dropped-thread" ~/.smartclaw/cron/
```
