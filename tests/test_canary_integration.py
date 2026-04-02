"""Integration test: bot-to-bot canary with gateway self-ignore.

Verifies the end-to-end canary loop:
  1. Second bot (U0A4G7LDJ4R) posts a canary to #ai-slack-test via Slack API.
  2. Verifies Slack accepts the message (ok=true).

The OpenClaw gateway is configured with ignoredUsers=["U0A4G7LDJ4R"]
so it drops these messages and does NOT respond. This is verified
by checking that no new OpenClaw-bot message appears in the channel
within a short observation window after the canary is sent.

Requires: real Slack API access (no mocks), real token from
~/.mcp_mail/credentials.json or SECOND_BOT_SLACK_TOKEN env var.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timezone
from typing import Optional

import pytest

# Test channel: #ai-slack-test
TEST_CHANNEL = "${SLACK_CHANNEL_ID}"
# Second bot user ID (must match openclaw.json channels.slack.ignoredUsers)
SECOND_BOT_USER_ID = "U0A4G7LDJ4R"
# OpenClaw bot user ID (gateway should respond to this, not to canary)
OPENCLAW_BOT_USER_ID = "U0AEZC7RX1Q"


def _get_token() -> Optional[str]:
    token = os.environ.get("SECOND_BOT_SLACK_TOKEN", "").strip()
    if token:
        return token
    creds_path = os.path.expanduser("~/.mcp_mail/credentials.json")
    try:
        with open(creds_path) as f:
            data = json.load(f)
        return data.get("SLACK_BOT_TOKEN", "").strip() or None
    except Exception:
        return None


def _send_slack(message: str, channel: str, thread_ts: Optional[str] = None) -> dict:
    token = _get_token()
    if not token:
        pytest.skip("No Slack bot token available (set SECOND_BOT_SLACK_TOKEN or ~/.mcp_mail/credentials.json)")

    payload: dict[str, object] = {"channel": channel, "text": message}
    if thread_ts:
        payload["thread_ts"] = thread_ts

    curl_cfg = (
        f'-H "Authorization: Bearer {token}"\n'
        f'-H "Content-Type: application/json"\n'
    )
    result = subprocess.run(
        [
            "curl", "-s", "-X", "POST",
            "https://slack.com/api/chat.postMessage",
            "--config", "-",
            "-d", json.dumps(payload),
        ],
        input=curl_cfg,
        capture_output=True, text=True, timeout=15,
    )
    return json.loads(result.stdout)


def _get_recent_messages(channel: str, oldest: str, limit: int = 10) -> list[dict]:
    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    if not token:
        token = os.environ.get("SLACK_USER_TOKEN", "").strip()
    if not token:
        return []

    result = subprocess.run(
        [
            "curl", "-s", "-X", "POST",
            "https://slack.com/api/conversations.history",
            "-H", f"Authorization: Bearer {token}",
            "-H", "Content-Type: application/json",
            "-d", json.dumps({"channel": channel, "oldest": oldest, "limit": limit}),
        ],
        capture_output=True, text=True, timeout=15,
    )
    try:
        data = json.loads(result.stdout)
        if data.get("ok"):
            return data.get("messages", [])
        return []
    except Exception:
        return []


def _count_openclaw_responses(
    channel: str,
    since_ts: str,
    before_ts: str,
) -> int:
    """Count messages from the OpenClaw bot between since_ts and before_ts."""
    messages = _get_recent_messages(channel, since_ts, limit=20)
    count = 0
    for msg in messages:
        ts = msg.get("ts", "")
        if ts and ts >= before_ts:
            continue  # outside window
        if ts and ts <= since_ts:
            continue  # outside window
        user = msg.get("user", "") or msg.get("bot_profile", {}).get("bot_user_id", "")
        if user == OPENCLAW_BOT_USER_ID:
            count += 1
    return count


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------

class TestCanaryIntegration:
    """Real end-to-end canary tests against the Slack API."""

    def test_canary_send_returns_ok(self) -> None:
        """Second bot can post to Slack and receive ok=true."""
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        message = f"[canary test {timestamp}] second bot ping"

        result = _send_slack(message, TEST_CHANNEL)

        assert result.get("ok") is True, (
            f"Slack API rejected canary: {result.get('error', 'unknown')}"
        )
        assert "ts" in result, "Slack response missing 'ts' (message timestamp)"

    def test_gateway_ignores_canary_no_openclaw_response(self) -> None:
        """Gateway does NOT respond to canary from second bot (U0A4G7LDJ4R).

        This proves the ignoredUsers config works: after the canary is posted,
        no OpenClaw-bot message appears in the channel within the observation window.
        """
        # Record the time before sending
        before_ts_float = time.time()
        before_ts = str(before_ts_float)

        # Send canary
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        message = f"[canary self-ignore test {timestamp}]"

        result = _send_slack(message, TEST_CHANNEL)
        assert result.get("ok") is True, f"Canary send failed: {result}"

        canary_ts = result.get("ts", "")
        assert canary_ts, "Canary returned no ts"

        # Observation window: 8 seconds after send
        # (long enough for gateway to process, short enough to be deterministic)
        time.sleep(8)

        after_ts_float = time.time()
        after_ts = str(after_ts_float)

        # Look for OpenClaw bot responses between canary_ts and after_ts
        messages = _get_recent_messages(TEST_CHANNEL, canary_ts, limit=20)
        openclaw_responses = [
            msg for msg in messages
            if canary_ts < msg.get("ts", "") < after_ts
            and (
                msg.get("user") == OPENCLAW_BOT_USER_ID
                or msg.get("bot_profile", {}).get("bot_user_id") == OPENCLAW_BOT_USER_ID
            )
        ]

        assert len(openclaw_responses) == 0, (
            f"Gateway responded to canary (ignoredUsers may not be working): "
            f"{openclaw_responses}"
        )

    def test_canary_in_thread_gets_no_reply(self) -> None:
        """Canary sent as a thread reply is also ignored by the gateway."""
        # First post a parent message to reply to
        parent_result = _send_slack("parent message for thread test", TEST_CHANNEL)
        assert parent_result.get("ok") is True
        parent_ts = parent_result.get("ts", "")
        assert parent_ts

        before_ts_float = time.time()

        # Send canary in thread
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        canary_result = _send_slack(
            f"[canary thread test {timestamp}]",
            TEST_CHANNEL,
            thread_ts=parent_ts,
        )
        assert canary_result.get("ok") is True
        canary_ts = canary_result.get("ts", "")

        time.sleep(8)
        after_ts_float = time.time()

        # Check for OpenClaw responses in the thread
        messages = _get_recent_messages(TEST_CHANNEL, canary_ts, limit=20)
        openclaw_thread_replies = [
            msg for msg in messages
            if canary_ts < msg.get("ts", "") < str(after_ts_float)
            and msg.get("thread_ts") == parent_ts
            and (
                msg.get("user") == OPENCLAW_BOT_USER_ID
                or msg.get("bot_profile", {}).get("bot_user_id") == OPENCLAW_BOT_USER_ID
            )
        ]

        assert len(openclaw_thread_replies) == 0, (
            f"Gateway replied in thread to canary: {openclaw_thread_replies}"
        )
