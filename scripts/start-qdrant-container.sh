#!/usr/bin/env bash
# Start the openclaw-mem0-qdrant Docker container, waiting for Docker to be ready.
# Called by ai.smartclaw.qdrant launchd agent on login.
set -euo pipefail

CONTAINER="openclaw-mem0-qdrant"
DOCKER_BIN="${DOCKER_BIN:-/usr/local/bin/docker}"
MAX_WAIT=60
WAITED=0

docker_info_ok() {
  local context="${1:-}"
  if [[ -n "$context" ]]; then
    "$DOCKER_BIN" --context "$context" info >/dev/null 2>&1
  else
    "$DOCKER_BIN" info >/dev/null 2>&1
  fi
}

select_docker_context() {
  if [[ -n "${OPENCLAW_QDRANT_DOCKER_CONTEXT:-}" ]] && docker_info_ok "${OPENCLAW_QDRANT_DOCKER_CONTEXT}"; then
    printf '%s' "${OPENCLAW_QDRANT_DOCKER_CONTEXT}"
    return 0
  fi
  if docker_info_ok ""; then
    printf '%s' ""
    return 0
  fi
  local context
  for context in colima-ci default desktop-linux; do
    if docker_info_ok "$context"; then
      printf '%s' "$context"
      return 0
    fi
  done
  return 1
}

docker_cmd() {
  if [[ -n "${DOCKER_CONTEXT_NAME:-}" ]]; then
    "$DOCKER_BIN" --context "$DOCKER_CONTEXT_NAME" "$@"
  else
    "$DOCKER_BIN" "$@"
  fi
}

# Docker Desktop or Colima may not be ready immediately on login — wait for any usable context.
DOCKER_CONTEXT_NAME=""
until DOCKER_CONTEXT_NAME="$(select_docker_context)"; do
  if [[ $WAITED -ge $MAX_WAIT ]]; then
    echo "$(date): no usable Docker context after ${MAX_WAIT}s, giving up" >&2
    exit 1
  fi
  sleep 5
  WAITED=$((WAITED + 5))
done

# docker start is idempotent (no-op if already running)
if docker_cmd start "$CONTAINER" >/dev/null 2>&1; then
  if [[ -n "$DOCKER_CONTEXT_NAME" ]]; then
    echo "$(date): $CONTAINER started via docker context $DOCKER_CONTEXT_NAME"
  else
    echo "$(date): $CONTAINER started"
  fi
else
  echo "$(date): failed to start $CONTAINER (container may not exist — run scripts/install-qdrant-container.sh)" >&2
  exit 1
fi
