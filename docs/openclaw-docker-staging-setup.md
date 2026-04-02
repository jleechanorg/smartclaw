# OpenClaw Docker Staging Setup Guide

Run OpenClaw staging inside Docker, side-by-side with the main native gateway.

**Use this when:** you want an isolated, containerized staging environment that won't interfere with the host-installed gateway.

**vs. native staging (openclaw-staging-setup.md):** Docker gives you a clean separation — different filesystem, different process namespace. Native staging shares the Node.js environment with the host.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Stop Native Staging Gateway](#2-stop-native-staging-gateway)
3. [Docker Login to GHCR](#3-docker-login-to-ghcr)
4. [Pull the Image](#4-pull-the-image)
5. [Configure docker-compose](#5-configure-docker-compose)
6. [Start the Container](#6-start-the-container)
7. [Verify](#7-verify)
8. [Manage](#8-manage)
9. [Updating](#9-updating)
10. [Teardown](#10-teardown)

---

## 1. Prerequisites

- Docker Desktop (or Docker Engine) running
- GHCR pull access (Docker Hub login or GitHub packages auth)
- Existing `~/.openclaw-staging/` with valid `openclaw.json` and `workspace/`
- `OPENCLAW_GATEWAY_TOKEN` from `~/.openclaw-staging/openclaw.json` → `gateway.auth.token`
- `GOG_KEYRING_PASSWORD` from `~/.openclaw-staging/openclaw.json` → `env.GOG_KEYRING_PASSWORD`

---

## 2. Stop Native Staging Gateway

If the native staging launchd service is running, stop it first:

```bash
# Find the staging PID
launchctl list | grep staging

# Stop via launchd (preferred)
launchctl bootout gui/$(id -u)/ai.openclaw.staging

# Or kill directly if bootout fails
launchctl list | grep ai.openclaw.staging
# Note the PID, then:
kill <PID>

# Verify it's down
curl -s --max-time 3 http://127.0.0.1:18810/health || echo "Staging gateway confirmed down"
```

---

## 3. Docker Login to GHCR

The OpenClaw image lives at `ghcr.io/openclaw/openclaw`. If your Docker engine isn't already authenticated:

```bash
TOKEN=$(gh auth token)
echo "$TOKEN" | docker login ghcr.io -u $(gh api /user --jq .login) --password-stdin
```

> Note: `gh auth token` requires the `read:user` scope. Your gh-cli token already has this.

---

## 4. Pull the Image

```bash
docker pull ghcr.io/openclaw/openclaw:latest
```

Or pin to a specific version (recommended for reproducibility):

```bash
OPENCLAW_VERSION=$(npm show openclaw version)
docker pull ghcr.io/openclaw/openclaw:$OPENCLAW_VERSION
```

---

## 5. Configure docker-compose

Create a working directory and `docker-compose.staging.yml`:

```bash
mkdir -p ~/openclaw-docker-staging
```

**`~/openclaw-docker-staging/docker-compose.staging.yml`:**

```yaml
# Standalone docker-compose for OpenClaw staging (Docker)
# Usage: docker compose -f docker-compose.staging.yml [up|down|logs...]
services:
  openclaw-gateway:
    image: ghcr.io/openclaw/openclaw:latest
    environment:
      HOME: /home/node
      TERM: xterm-256color
      # REQUIRED: paste your staging gateway token from ~/.openclaw-staging/openclaw.json
      OPENCLAW_GATEWAY_TOKEN: YOUR_TOKEN_HERE
      # REQUIRED: paste your gog keyring password
      GOG_KEYRING_PASSWORD: YOUR_KEYRING_PASSWORD
      TZ: America/Los_Angeles
    volumes:
      # Mount your existing staging config directory (${HOME} is expanded by Docker Compose from your shell)
      - ${HOME}/.openclaw-staging:/home/node/.openclaw
      # Mount staging workspace
      - ${HOME}/.openclaw-staging/workspace:/home/node/.openclaw/workspace
    ports:
      # Host loopback : Container gateway port
      - "127.0.0.1:18810:18789"
      - "127.0.0.1:18920:18790"
    init: true
    restart: unless-stopped
    command:
      - node
      - dist/index.js
      - gateway
      - --bind
      - lan
      - --port
      - "18789"
    healthcheck:
      test:
        - CMD
        - node
        - -e
        - "fetch('http://127.0.0.1:18789/healthz').then((r)=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"
      interval: 30s
      timeout: 5s
      retries: 5
      start_period: 20s

  openclaw-cli:
    image: ghcr.io/openclaw/openclaw:latest
    network_mode: service:openclaw-gateway
    cap_drop:
      - NET_RAW
      - NET_ADMIN
    security_opt:
      - no-new-privileges:true
    environment:
      HOME: /home/node
      TERM: xterm-256color
      OPENCLAW_GATEWAY_TOKEN: YOUR_TOKEN_HERE
      GOG_KEYRING_PASSWORD: YOUR_KEYRING_PASSWORD
      TZ: America/Los_Angeles
      BROWSER: echo
    volumes:
      - ${HOME}/.openclaw-staging:/home/node/.openclaw
      - ${HOME}/.openclaw-staging/workspace:/home/node/.openclaw/workspace
    stdin_open: true
    tty: true
    init: true
    entrypoint: ["node", "dist/index.js"]
    depends_on:
      - openclaw-gateway
```

**Fill in the two values at the top of the file:**

| Value | Where to find it |
|---|---|
| `OPENCLAW_GATEWAY_TOKEN` | `~/.openclaw-staging/openclaw.json` → `gateway.auth.token` |
| `GOG_KEYRING_PASSWORD` | `~/.openclaw-staging/openclaw.json` → `env.GOG_KEYRING_PASSWORD` |

> **Security note:** This file contains secrets. Never commit it to git. Add `docker-compose.staging.yml` to `.gitignore` if the directory is tracked.

---

## 5b. Required Config Patch for non-loopback bind

When the gateway binds to `lan` (required for Docker bridge networking), it requires explicit Control UI origins. Add this to `~/.openclaw-staging/openclaw.json`:

```bash
python3 -c "
import json
import os
path = os.path.expanduser('~/.openclaw-staging/openclaw.json')
with open(path) as f:
    c = json.load(f)
c.setdefault('gateway', {}).setdefault('controlUi', {})['allowedOrigins'] = [
    'http://127.0.0.1:18810',
    'http://localhost:18810'
]
# SECURITY: dangerouslyAllowHostHeaderOriginFallback relaxes origin checks so the
# Control UI can work when the Host header does not match allowedOrigins (common in
# local Docker setups). Use only for local/testing troubleshooting. Prefer listing
# every origin you need in allowedOrigins. Remove this key or set it to false before
# any non-local or production deployment.
c['gateway']['controlUi']['dangerouslyAllowHostHeaderOriginFallback'] = True
with open(path, 'w') as f:
    json.dump(c, f, indent=2)
print('Updated controlUi.allowedOrigins')
"
```

---

## 6. Start the Container

```bash
cd ~/openclaw-docker-staging
docker compose -f docker-compose.staging.yml up -d
```

Expected output:

```text
Network openclaw-docker-staging_default Creating
 Container openclaw-docker-staging-openclaw-gateway-1 Creating
 ...
Container openclaw-docker-staging-openclaw-gateway-1 Started
```

---

## 7. Verify

```bash
# Health endpoints (both should return {"ok":true,"status":"live"})
curl http://127.0.0.1:18810/health
curl http://127.0.0.1:18810/healthz

# Inside container
docker exec openclaw-docker-staging-openclaw-gateway-1 wget -q -O- http://127.0.0.1:18789/health

# Check container health status
docker inspect openclaw-docker-staging-openclaw-gateway-1 --format 'Health: {{.State.Health.Status}}'

# Check logs
docker compose -f docker-compose.staging.yml logs --tail=20
```

---

## 8. Manage

```bash
# View logs (follow mode)
docker compose -f docker-compose.staging.yml logs -f

# Restart the gateway
docker compose -f docker-compose.staging.yml restart openclaw-gateway

# Stop (containers removed, images kept)
docker compose -f docker-compose.staging.yml down

# Full restart (stop + start)
docker compose -f docker-compose.staging.yml restart

# Open a CLI shell inside the container
docker compose -f docker-compose.staging.yml run --rm openclaw-cli sh

# Run a one-off openclaw CLI command
docker compose -f docker-compose.staging.yml run --rm openclaw-cli status
docker compose -f docker-compose.staging.yml run --rm openclaw-cli config get gateway.port
```

---

## 9. Updating

```bash
# Pull the latest image, then recreate containers so they use it (restart alone keeps the old image)
docker compose -f docker-compose.staging.yml pull openclaw-gateway openclaw-cli
docker compose -f docker-compose.staging.yml up -d
```

For version-pinned updates, update the `image:` line in `docker-compose.staging.yml` first.

---

## 10. Teardown

```bash
# Stop and remove containers
docker compose -f docker-compose.staging.yml down

# Remove the image (optional)
docker rmi ghcr.io/openclaw/openclaw:latest

# Restart the native staging gateway
launchctl start gui/$(id -u)/ai.openclaw.staging
```

---

## Key Differences: Docker vs Native Staging

| Aspect | Docker | Native (launchd) |
|---|---|---|
| Process isolation | Full container | Same host |
| Port exposure | Docker bridge → host | Direct on host |
| Logs | `docker compose logs` | `tail ~/.openclaw-staging/logs/gateway.log` |
| Restart after reboot | `restart: unless-stopped` in compose | launchd auto-starts |
| File changes | Mounted volumes (immediate) | Immediate |
| Healthcheck | Docker native | `curl` via launchd |

---

## Troubleshooting

### curl: (56) Recv failure: Connection reset by peer

Gateway is starting up. Wait 20–30s and retry. If it persists, check logs:
```bash
docker compose -f docker-compose.staging.yml logs --tail=50
```

### Health check failing (unhealthy)

The gateway failed to start. Common cause: misconfigured `openclaw.json`:
```bash
# Verify the mounted config is readable inside the container
docker exec openclaw-docker-staging-openclaw-gateway-1 cat /home/node/.openclaw/openclaw.json | python3 -m json.tool > /dev/null && echo "Config valid"
```

### Discord/Slack not connecting

Check the channel config in `~/.openclaw-staging/openclaw.json`. Each channel (Discord, Slack) needs its own bot token. Shared tokens between native and Docker gateways cause WebSocket conflicts.

### Port 18810 already in use

Something else is holding port 18810:
```bash
lsof -i :18810
kill <PID>
docker compose -f docker-compose.staging.yml start
```
