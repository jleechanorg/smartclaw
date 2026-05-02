#!/usr/bin/env bash
# Create or verify the openclaw-mem0-qdrant Docker container.
# Idempotent: skips creation if container already exists.
# Storage is persisted at ~/.smartclaw/qdrant_storage/
set -euo pipefail

CONTAINER="openclaw-mem0-qdrant"
IMAGE="qdrant/qdrant:latest"
STORAGE_DIR="${HOME}/.smartclaw/qdrant_storage"
HOST_PORT=6333
DOCKER_BIN="${DOCKER_BIN:-docker}"

docker_cmd() {
  if [[ -n "${OPENCLAW_QDRANT_DOCKER_CONTEXT:-}" ]]; then
    "$DOCKER_BIN" --context "$OPENCLAW_QDRANT_DOCKER_CONTEXT" "$@"
  else
    "$DOCKER_BIN" "$@"
  fi
}

if docker_cmd ps -a --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
  echo "  ✓ container '${CONTAINER}' already exists (skipping create)"
else
  mkdir -p "$STORAGE_DIR"
  docker_cmd pull "$IMAGE"
  docker_cmd create \
    --name "$CONTAINER" \
    -p "${HOST_PORT}:6333" \
    -v "${STORAGE_DIR}:/qdrant/storage" \
    "$IMAGE"
  echo "  ✓ container '${CONTAINER}' created with storage at ${STORAGE_DIR}"
fi

# Start it now
docker_cmd start "$CONTAINER" >/dev/null
echo "  ✓ ${CONTAINER} running on port ${HOST_PORT}"
