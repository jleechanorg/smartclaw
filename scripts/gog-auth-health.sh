#!/usr/bin/env bash
# Non-interactive gog OAuth + API probes (Gmail, Calendar, Drive).
# Uses GOG_KEYRING_PASSWORD from env or ~/.smartclaw/openclaw.json (see lib/gog-env.sh).
#
# Exit codes:
#   0 — token valid; Gmail, Calendar, and Drive API probes succeed
#   1 — gog missing, invalid_grant, or API failure
#   2 — Gmail + Calendar OK; Drive needs broader OAuth scopes (re-run gog auth add … --services=all)
#
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=/dev/null
source "$REPO_ROOT/lib/gog-env.sh"
load_gog_env_from_openclaw "${LIVE_OPENCLAW:-$HOME/.smartclaw}/openclaw.json"

if ! command -v gog >/dev/null 2>&1; then
  echo "gog not installed (brew install jleechanorg/tap/gog)" >&2
  exit 1
fi

auth_out="$(gog auth list --check 2>&1)" || true
if echo "$auth_out" | grep -qiE 'invalid_grant|expired or revoked'; then
  echo "refresh token invalid — try: gog auth tokens import ~/.smartclaw/credentials/gog-refresh-token.json" >&2
  echo "or: GOOGLE_CLOUD_PROJECT=\$GOOGLE_CLOUD_PROJECT gog auth add EMAIL --services=all --remote" >&2
  exit 1
fi

email="$(echo "$auth_out" | grep -E '^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}' | head -1 | awk '{print $1}')"
if [[ -z "$email" ]]; then
  email="jleechan@gmail.com"
fi

read -r CAL_FROM CAL_TO <<< "$(python3 -c "from datetime import datetime, timedelta
z = datetime.now().astimezone().strftime('%z')
z = z[:3] + ':' + z[3:]
n = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
print(n.strftime('%Y-%m-%dT%H:%M:%S') + z, (n + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M:%S') + z)")"

gmail_out="$(gog --account "$email" --no-input gmail search 'in:inbox newer_than:1d' --max 1 --json 2>&1)" || true
if echo "$gmail_out" | grep -qiE 'invalid_grant|invalid authentication|401 Unauthorized'; then
  echo "gmail API probe failed" >&2
  exit 1
fi

cal_out="$(gog --account "$email" --no-input calendar list primary --from "$CAL_FROM" --to "$CAL_TO" --json 2>&1)" || true
if echo "$cal_out" | grep -qiE 'invalid_grant|401 Unauthorized'; then
  echo "calendar API probe failed" >&2
  exit 1
fi

drive_out="$(gog --account "$email" --no-input drive ls --max 1 --json 2>&1)" || true
if echo "$drive_out" | grep -qi 'insufficientPermissions'; then
  echo "Drive/Docs/Slides scopes missing — re-authorize with:" >&2
  echo "  GOG_KEYRING_PASSWORD=… GOOGLE_CLOUD_PROJECT=\"\${GOOGLE_CLOUD_PROJECT}\" gog auth add $email --services=all --remote" >&2
  echo "  gog auth tokens export $email --out ~/.smartclaw/credentials/gog-refresh-token.json --overwrite" >&2
  exit 2
fi
if echo "$drive_out" | grep -qiE 'Google API error|invalid_grant'; then
  echo "drive API probe: $drive_out" >&2
  exit 1
fi

echo "Google OAuth OK ($email) — gmail, calendar, drive probes passed"
exit 0
