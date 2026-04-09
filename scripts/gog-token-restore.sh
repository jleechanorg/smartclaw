#!/bin/bash
# Restore gog token from backup if keychain entry is missing.
# Run this from bootstrap.sh or manually after a system wipe.
# Usage: ./gog-token-restore.sh [email]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/../lib/gog-env.sh"
load_gog_env_from_openclaw "${HOME}/.openclaw/openclaw.json"

EMAIL="${1:-jleechan@gmail.com}"
BACKUP_FILE="${HOME}/.openclaw/credentials/gog-refresh-token.json"
GOG_BIN="$(command -v gog || true)"

if [ -z "$GOG_BIN" ]; then
  echo "ERROR: gog not found. Install with: brew install jleechanorg/tap/gog" >&2
  exit 1
fi

# Check if a valid refresh token is already stored (not just a row for the email)
_list_out="$("$GOG_BIN" auth list --check 2>&1)"
if echo "$_list_out" | grep -q "$EMAIL" && ! echo "$_list_out" | grep -qiE 'invalid_grant|expired or revoked'; then
  echo "OK: gog token for $EMAIL already valid."
  exit 0
fi

# Try to restore from backup
if [ -f "$BACKUP_FILE" ]; then
  echo "Restoring gog token from $BACKUP_FILE ..."
  "$GOG_BIN" auth tokens import "$BACKUP_FILE"
  echo "Done. Run 'gog auth list' to verify."
else
  echo "No backup found at $BACKUP_FILE."
  echo ""
  echo "Run this to authenticate (opens browser URL):"
  echo "  GOOGLE_CLOUD_PROJECT=infinite-zephyr-487405-d0 gog auth add $EMAIL --services=all --remote"
  echo ""
  echo "After authenticating, back up the token:"
  echo "  gog auth tokens export $EMAIL --out $BACKUP_FILE"
  exit 1
fi
