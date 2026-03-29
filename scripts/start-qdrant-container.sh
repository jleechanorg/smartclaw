#!/usr/bin/env bash
# Start the openclaw-mem0-qdrant Docker container, waiting for Docker to be ready.
# Called by ai.openclaw.qdrant launchd agent on login.
set -euo pipefail

CONTAINER="openclaw-mem0-qdrant"
DOCKER="/usr/local/bin/docker"
MAX_WAIT=60
WAITED=0

# Docker Desktop may not be ready immediately on login — wait for it
until "$DOCKER" info >/dev/null 2>&1; do
  if [[ $WAITED -ge $MAX_WAIT ]]; then
    echo "$(date): Docker not ready after ${MAX_WAIT}s, giving up" >&2
    exit 1
  fi
  sleep 5
  WAITED=$((WAITED + 5))
done

# docker start is idempotent (no-op if already running)
if "$DOCKER" start "$CONTAINER" >/dev/null 2>&1; then
  echo "$(date): $CONTAINER started"
else
  echo "$(date): failed to start $CONTAINER (container may not exist — run scripts/install-qdrant-container.sh)" >&2
  exit 1
fi
