# OpenClaw Auto-Start Configuration Guide

**Last Updated:** 2026-02-13
**OpenClaw Version:** v2026.2.12
**macOS Configuration:** Complete ✅

---

## 🚧 Scheduling Guardrail

- **Forbidden:** system `crontab` edits for OpenClaw reminder/scheduling jobs.
- **Required:** OpenClaw gateway cron workflow only (`openclaw cron ...`).

---

## ✅ What's Configured

### 1. **Primary Auto-Start: LaunchAgent**
- **File:** `~/Library/LaunchAgents/ai.openclaw.gateway.plist`
- **RunAtLoad:** `true` ← Starts automatically on macOS boot
- **KeepAlive:** `true` ← Automatically restarts if crashes
- **Current Status:** Loaded and running (PID: XXXXX)

### 2. **Startup Verification: LaunchAgent**
- **File:** `~/Library/LaunchAgents/ai.openclaw.startup-check.plist`
- **Purpose:** Sends WhatsApp confirmation after each login/restart
- **Sends message to:** `OPENCLAW_WHATSAPP_TARGET`
- **Logs:** `~/.openclaw/logs/startup-check.log`

### 3. **Health Monitoring: OpenClaw Gateway Cron**
- **Schedule:** Every 5 minutes
- **Script:** `~/.openclaw/health-check.sh`
- **Purpose:** Monitors gateway health and auto-recovery if needed via OpenClaw gateway cron jobs
- **Logs:** `~/.openclaw/logs/health-check.log`

---

## 🔍 How to Verify After Restart

### Test 1: Check LaunchAgent Status
```bash
launchctl list | grep openclaw
```
**Expected Output:**
```text
[PID]  0  ai.openclaw.gateway
[PID]  0  ai.openclaw.startup-check
```

### Test 2: Check Gateway Status
```bash
openclaw gateway status
```
**Expected Output:**
```text
Runtime: running (pid XXXXX)
RPC probe: ok
```

### Test 3: Check WhatsApp Connection
```bash
openclaw channels list
```
**Expected Output:**
```text
WhatsApp default: linked, enabled
```

### Test 4: Check WhatsApp (You'll Receive a Message!)
After each restart/login, you should receive confirmation if `OPENCLAW_WHATSAPP_TARGET` is set:
> 🚀 OpenClaw auto-started successfully (PID: XXXXX) ✅

---

## 📊 Monitoring & Logs

### View Real-Time Gateway Logs
```bash
openclaw logs --follow
```

### View Health Check Results
```bash
tail -f ~/.openclaw/logs/health-check.log
```

### View Startup Check Results
```bash
tail -f ~/.openclaw/logs/startup-check.log
```

### Check Gateway Cron Configuration
```bash
openclaw cron status
openclaw cron list
```

---

## 🔧 Manual Controls

### Start Gateway
```bash
openclaw gateway start
# OR
openclaw gateway install
```

### Stop Gateway
```bash
openclaw gateway stop
```

### Restart Gateway
```bash
openclaw gateway stop && sleep 2 && openclaw gateway install
```

### Force Reload LaunchAgent
```bash
launchctl unload ~/Library/LaunchAgents/ai.openclaw.gateway.plist
launchctl load ~/Library/LaunchAgents/ai.openclaw.gateway.plist
```

### Run Health Check Manually
```bash
~/.openclaw/health-check.sh
```

---

## 🚨 Troubleshooting

### Gateway Not Starting After Restart

1. **Check if LaunchAgent is loaded:**
   ```bash
   launchctl list | grep openclaw
   ```

2. **If not loaded, load manually:**
   ```bash
   launchctl load ~/Library/LaunchAgents/ai.openclaw.gateway.plist
   ```

3. **Check for errors:**
   ```bash
   tail -50 ~/.openclaw/logs/gateway.err.log
   ```

### WhatsApp Disconnected

1. **Check status:**
   ```bash
   openclaw channels list
   ```

2. **Relink if needed:**
   ```bash
   openclaw channels login --channel whatsapp --account default
   ```
   Scan QR code within 60 seconds.

### Health Check Not Running

1. **Verify gateway cron jobs:**
   ```bash
   openclaw cron status
   openclaw cron list
   ```

2. **Test health check manually:**
   ```bash
   ~/.openclaw/health-check.sh && echo "Exit code: $?"
   ```

3. **Inspect gateway logs for cron execution details:**
   ```bash
   openclaw logs --follow
   ```

---

## 📁 File Locations

| Component | Location |
|-----------|----------|
| **Main Gateway LaunchAgent** | `~/Library/LaunchAgents/ai.openclaw.gateway.plist` |
| **Startup Check LaunchAgent** | `~/Library/LaunchAgents/ai.openclaw.startup-check.plist` |
| **Health Check Script** | `~/.openclaw/health-check.sh` |
| **Startup Check Script** | `~/.openclaw/startup-check.sh` |
| **Gateway Logs** | `~/.openclaw/logs/gateway.log` |
| **Gateway Error Logs** | `~/.openclaw/logs/gateway.err.log` |
| **Health Check Logs** | `~/.openclaw/logs/health-check.log` |
| **Startup Check Logs** | `~/.openclaw/logs/startup-check.log` |
| **Configuration** | `~/.openclaw/openclaw.json` |

---

## 🎯 Quick Health Status

Run this one-liner for a complete health check:
```bash
echo "=== OpenClaw Health Status ===" && \
launchctl list | grep openclaw && \
echo "" && openclaw gateway status && \
echo "" && openclaw channels list
```

---

## ✅ Configuration Summary

✅ **LaunchAgent installed** with RunAtLoad=true, KeepAlive=true
✅ **Startup verification** configured (sends WhatsApp confirmation)
✅ **Health monitoring** via OpenClaw gateway cron (every 5 minutes)
✅ **WhatsApp notification** configured via `OPENCLAW_WHATSAPP_TARGET`
✅ **Auto-recovery** enabled (restarts on crash)
✅ **Version:** v2026.2.12 (latest)

**Next Restart:** You will receive a WhatsApp message confirming OpenClaw started successfully if `OPENCLAW_WHATSAPP_TARGET` is set. 🚀

---

*Generated by OpenClaw Auto-Configuration System*
