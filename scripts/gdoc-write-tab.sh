#!/bin/bash
# gdoc-write-tab.sh — Write content to a specific Google Docs tab via Docs API
#
# Usage:
#   gdoc-write-tab.sh <doc-id> <tab-id> <content-file>
#   gdoc-write-tab.sh <doc-id> <tab-id> -  (read from stdin)
#
# Examples:
#   gdoc-write-tab.sh 1Cm4a... t.ssvp22c6w2uc /tmp/staging.md
#   cat staging.md | gdoc-write-tab.sh 1Cm4a... t.ssvp22c6w2uc -
#
# Tab IDs: gog docs list-tabs <doc-id>
# Token:   gog auth tokens export jleechan@gmail.com --out /tmp/gog_token.json --overwrite
#
# Notes:
# - Replaces all content in the target tab (delete existing + insert new)
# - Does NOT affect other tabs
# - Requires Docs API enabled: console.developers.google.com/apis/api/docs.googleapis.com

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/../lib/gog-env.sh"
load_gog_env_from_openclaw "${HOME}/.smartclaw/openclaw.json"

DOC_ID="${1:-}"
TAB_ID="${2:-}"
CONTENT_FILE="${3:-}"

if [[ -z "$DOC_ID" || -z "$TAB_ID" || -z "$CONTENT_FILE" ]]; then
  echo "Usage: $0 <doc-id> <tab-id> <content-file|->" >&2
  echo "  List tab IDs: gog docs list-tabs <doc-id>" >&2
  exit 1
fi

if [[ "$CONTENT_FILE" == "-" ]]; then
  CONTENT=$(cat)
else
  CONTENT=$(cat "$CONTENT_FILE")
fi

CREDS_FILE="$HOME/Library/Application Support/gogcli/credentials.json"
TOKEN_FILE="/tmp/gog_token_tab.json"

# Refresh token
gog auth tokens export jleechan@gmail.com --out "$TOKEN_FILE" --overwrite --no-input >/dev/null 2>&1

ACCESS_TOKEN=$(python3 - "$CREDS_FILE" "$TOKEN_FILE" << 'PYEOF'
import json, sys, urllib.request, urllib.parse
creds_file, token_file = sys.argv[1], sys.argv[2]
with open(creds_file) as f: creds = json.load(f)
with open(token_file) as f: tok = json.load(f)
if 'installed' in creds: creds = creds['installed']
elif 'web' in creds: creds = creds['web']
refresh_token = tok.get('refresh_token') or tok.get('token', {}).get('refresh_token')
data = urllib.parse.urlencode({
    'client_id': creds['client_id'], 'client_secret': creds['client_secret'],
    'refresh_token': refresh_token, 'grant_type': 'refresh_token'
}).encode()
result = json.loads(urllib.request.urlopen(urllib.request.Request(
    'https://oauth2.googleapis.com/token', data=data,
    headers={'Content-Type': 'application/x-www-form-urlencoded'}
)).read())
print(result['access_token'])
PYEOF
)

# Get current tab end index to delete existing content
END_INDEX=$(python3 - "$DOC_ID" "$TAB_ID" "$ACCESS_TOKEN" << 'PYEOF'
import json, sys, urllib.request
doc_id, tab_id, token = sys.argv[1], sys.argv[2], sys.argv[3]
req = urllib.request.Request(
    f"https://docs.googleapis.com/v1/documents/{doc_id}?includeTabsContent=true",
    headers={"Authorization": f"Bearer {token}"}
)
doc = json.loads(urllib.request.urlopen(req).read())
for tab in doc.get('tabs', []):
    if tab.get('tabProperties', {}).get('tabId') == tab_id:
        body = tab.get('documentTab', {}).get('body', {})
        content = body.get('content', [])
        if content:
            end = content[-1].get('endIndex', 1)
            # Leave at least index 1 (empty doc has index 1)
            print(max(end - 1, 1))
        else:
            print(1)
        break
PYEOF
)

python3 - "$DOC_ID" "$TAB_ID" "$ACCESS_TOKEN" "$END_INDEX" << PYEOF
import json, sys, urllib.request
doc_id, tab_id, token, end_index = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
content = """$(printf '%s' "$CONTENT" | python3 -c "import sys; s=sys.stdin.read(); print(s.replace('\\\\', '\\\\\\\\').replace('\"', '\\\\\"').replace(chr(10), '\\\\n'))")"""

requests = []

# Delete existing content if tab has content (end_index > 1)
if end_index > 1:
    requests.append({
        "deleteContentRange": {
            "range": {
                "startIndex": 1,
                "endIndex": end_index,
                "tabId": tab_id
            }
        }
    })

# Insert new content
requests.append({
    "insertText": {
        "location": {"index": 1, "tabId": tab_id},
        "text": content
    }
})

payload = json.dumps({"requests": requests}).encode()
req = urllib.request.Request(
    f"https://docs.googleapis.com/v1/documents/{doc_id}:batchUpdate",
    data=payload,
    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
)
result = json.loads(urllib.request.urlopen(req).read())
print(f"Written to tab {tab_id} in doc {result.get('documentId')}")
PYEOF

rm -f "$TOKEN_FILE"
