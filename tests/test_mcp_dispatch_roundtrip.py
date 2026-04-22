"""Deterministic MCP E2E tests — mirrors test_mvp_loopback_e2e.py.

These replace the Slack Socket Mode trigger with direct OpenClaw agent calls
via the gateway ACP (Agent Control Protocol) path:

  openclaw agent --agent main --message <text> --json

This talks to OpenClaw directly through its gateway — no Slack Socket Mode
required on the INPUT side. Output notifications still post real Slack DMs
and real threaded replies.

No mocks, no monkeypatching, no stubs. All Slack/tmux/git interactions are real.

test_mcp_agent_send_receive
  Deterministic smoke: send a real message to OpenClaw agent via ACP, verify
  we get a response back. No agent spawn. Fast (< 30 s).

test_mcp_dispatch_roundtrip  [REAL — requires ai_orch, Slack tokens, openclaw running]
  Full E2E identical to test_slack_loopback_roundtrip except:
    - INPUT trigger is `openclaw agent` ACP call (not Slack Socket Mode)
    - We still create a real Slack anchor so threaded reply behavior remains covered
    - Deterministic input: direct subprocess call, no Socket Mode dependency
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

import pytest

from orchestration.dispatch_task import dispatch
from orchestration.smartclaw_notifier import drain_outbox
from orchestration.reconciliation import reconcile_registry_once
from orchestration.session_registry import get_mapping

MCTRL_ROOT = Path(__file__).resolve().parent.parent
OPENCLAW_AGENT_ID = "main"
_DM_CHANNEL = os.environ.get("MCTRL_TEST_DM_CHANNEL", "")
_AI_GENERAL = os.environ.get("MCTRL_TEST_TRIGGER_CHANNEL", "")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_json_payload(stdout: str) -> dict[str, Any]:
    """Parse the first JSON object from mixed CLI stdout."""
    decoder = json.JSONDecoder()
    for idx, ch in enumerate(stdout):
        if ch != "{":
            continue
        try:
            payload, _ = decoder.raw_decode(stdout[idx:])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    raise json.JSONDecodeError("No JSON object found in stdout", stdout, 0)


def _openclaw_agent(message: str, timeout: int = 120) -> dict[str, Any]:
    """Run `openclaw agent --agent main --message <text> --json`.

    Returns the parsed JSON response dict.
    Raises RuntimeError if the command fails.
    """
    session_id = f"mctrl-mcp-{uuid.uuid4().hex[:12]}"
    result = subprocess.run(
        [
            "openclaw",
            "agent",
            "--agent",
            OPENCLAW_AGENT_ID,
            "--session-id",
            session_id,
            "--message",
            message,
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"openclaw agent failed (rc={result.returncode}):\n"
            f"  stderr: {result.stderr!r}"
        )
    return _extract_json_payload(result.stdout)


def _agent_reply_text(resp: dict[str, Any]) -> str:
    """Extract the text reply from an openclaw agent --json response."""
    payloads = (resp.get("result") or {}).get("payloads")
    if not isinstance(payloads, list):
        payloads = resp.get("payloads") or []
    for p in payloads:
        if isinstance(p, dict) and p.get("text"):
            return str(p["text"])
    return ""


def _agent_call_succeeded(resp: dict[str, Any]) -> bool:
    """Treat the current OpenClaw JSON contract as success if it returned payloads."""
    if not isinstance(resp, dict):
        return False
    if resp.get("error"):
        return False
    meta = resp.get("meta")
    if isinstance(meta, dict) and meta.get("aborted") is True:
        return False
    return bool(resp.get("payloads") or (resp.get("result") or {}).get("payloads"))


def _slack_history(token: str, channel: str, oldest: str, limit: int = 20) -> list[dict[str, Any]]:
    url = (
        f"https://slack.com/api/conversations.history"
        f"?channel={channel}&oldest={oldest}&limit={limit}&inclusive=false"
    )
    req = Request(url, headers={"Authorization": f"Bearer {token}"})
    with urlopen(req, timeout=10) as resp:
        return json.loads(resp.read()).get("messages") or []


def _poll_for_text(
    token: str, channel: str, needle: str, oldest: str,
    timeout: float = 30.0, interval: float = 2.0,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            msgs = _slack_history(token, channel, oldest)
            if any(needle in m.get("text", "") for m in msgs):
                return True
        except URLError:
            pass
        time.sleep(interval)
    return False


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


async def _openclaw_agent_async(message: str, timeout: int = 120) -> dict[str, Any]:
    return await asyncio.to_thread(_openclaw_agent, message, timeout)


async def _run_sync_cmd_async(cmd: list[str], **kwargs):
    return await asyncio.to_thread(subprocess.run, cmd, **kwargs)


async def _poll_for_text_async(
    token: str, channel: str, needle: str, oldest: str,
    timeout: float = 30.0, interval: float = 2.0,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            msgs = await asyncio.to_thread(_slack_history, token, channel, oldest)
            if any(needle in m.get("text", "") for m in msgs):
                return True
        except URLError:
            pass
        await _sleep(interval)
    return False


async def _poll_for_text_with_tokens_async(
    tokens: list[str], channel: str, needle: str, oldest: str,
    timeout: float = 30.0, interval: float = 2.0,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for token in tokens:
            if not token:
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if await _poll_for_text_async(
                token,
                channel,
                needle,
                oldest,
                timeout=remaining,
                interval=interval,
            ):
                return True
        await _sleep(interval)
    return False


async def _poll_for_thread_reply_async(
    token: str,
    channel: str,
    thread_ts: str,
    needle: str,
    timeout: float = 30.0,
    interval: float = 2.0,
) -> bool:
    def _fetch_replies() -> list[dict[str, Any]]:
        url = (
            "https://slack.com/api/conversations.replies"
            f"?channel={channel}&ts={thread_ts}&limit=20"
        )
        req = Request(url, headers={"Authorization": f"Bearer {token}"})
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read()).get("messages") or []

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            msgs = await asyncio.to_thread(_fetch_replies)
            if any(needle in m.get("text", "") for m in msgs[1:]):
                return True
        except URLError:
            pass
        await _sleep(interval)
    return False



# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_slack_history_handles_null_messages(monkeypatch) -> None:
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return b'{"ok":true,"messages":null}'

    monkeypatch.setattr("tests.test_mcp_dispatch_roundtrip.urlopen", lambda req, timeout=10: _Resp())

    assert _slack_history("xoxb-test", "C123", "0") == []


@pytest.mark.skipif(
    not os.environ.get("SLACK_USER_TOKEN") or not os.environ.get("SLACK_BOT_TOKEN"),
    reason="Requires SLACK_USER_TOKEN and SLACK_BOT_TOKEN (source ~/.profile and ~/.smartclaw/set-slack-env.sh)",
)
def test_mcp_agent_send_receive() -> None:
    """Smoke: send a real message to OpenClaw via ACP gateway, get a reply.

    Proves the openclaw agent ACP path is alive end-to-end.
    Asserts: exit 0, status "ok", non-empty reply. Does NOT assert LLM echo
    content — that is fragile across sessions with accumulated context.
    No agent spawn. Completes in < 30 s.
    """
    assert subprocess.run(["which", "openclaw"], capture_output=True).returncode == 0, (
        "openclaw must be in PATH"
    )

    run_id = uuid.uuid4().hex[:8]
    resp = _openclaw_agent(f"ACP smoke check {run_id}. Acknowledge with any reply.")
    reply = _agent_reply_text(resp)

    print(f"\n[openclaw-agent] run_id={run_id} ok={_agent_call_succeeded(resp)} reply={reply!r}")
    assert _agent_call_succeeded(resp), f"Agent ACP call failed: {resp}"
    assert reply, "Empty reply from openclaw agent — ACP path returned no text"
    print(f"\n[evidence] ACP path alive: run_id={run_id} reply non-empty")


@pytest.mark.skipif(
    not os.environ.get("SLACK_USER_TOKEN") or not os.environ.get("SLACK_BOT_TOKEN"),
    reason="Requires SLACK_USER_TOKEN and SLACK_BOT_TOKEN (source ~/.profile and ~/.smartclaw/set-slack-env.sh)",
)
def test_mcp_dispatch_roundtrip(tmp_path: Path) -> None:
    asyncio.run(_test_mcp_dispatch_roundtrip(tmp_path))


async def _test_mcp_dispatch_roundtrip(tmp_path: Path) -> None:
    """Full E2E loopback via OpenClaw ACP gateway instead of Slack Socket Mode trigger.

    Proves the complete chain:
      1. openclaw agent ACP call → trigger to OpenClaw (no Slack Socket Mode input)
      2. dispatch() spawns real ai_orch in real tmux worktree
      3. Agent commits and exits — tmux session dies for real
      4. reconcile_registry_once runs with ZERO monkeypatching
      5. OpenClaw notification handling posts real Slack DM + threaded reply
      6. Poll Slack DM and trigger thread to confirm both landed

    Key difference from test_slack_loopback_roundtrip:
      - INPUT trigger is `openclaw agent` ACP (not Slack Socket Mode)
      - A real Slack anchor message is posted before dispatch; completion must reply in-thread
      - Deterministic trigger: direct subprocess call, no Socket Mode dependency

    Requires: SLACK_USER_TOKEN, SLACK_BOT_TOKEN, ai_orch in PATH, openclaw CLI in PATH
    Duration: 3–10 minutes (real agent execution)
    """
    user_token = os.environ.get("SLACK_USER_TOKEN", "")
    bot_token = (
        os.environ.get("SLACK_BOT_TOKEN")
        or os.environ.get("SLACK_BOT_TOKEN")
        or ""
    )
    assert user_token, "SLACK_USER_TOKEN must be set (source ~/.profile)"
    assert bot_token, (
        "SLACK_BOT_TOKEN must be set "
        "(source ~/.smartclaw/set-slack-env.sh)"
    )
    assert (await _run_sync_cmd_async(["which", "ai_orch"], capture_output=True)).returncode == 0, (
        "ai_orch must be in PATH"
    )
    assert (await _run_sync_cmd_async(["which", "openclaw"], capture_output=True)).returncode == 0, (
        "openclaw must be in PATH"
    )

    bead_id = f"ORCH-mcp-{uuid.uuid4().hex[:6]}"
    registry = tmp_path / "registry.jsonl"
    outbox = tmp_path / "outbox.jsonl"
    ts_before = str(time.time() - 2)

    # Step 0: Real Slack anchor so threaded completion replies remain covered.
    trigger_text = f"[mctrl-mcp-e2e-anchor] {bead_id}"
    trigger_post = await asyncio.to_thread(_slack_post, user_token, _AI_GENERAL, trigger_text)
    assert trigger_post.get("ok") is True, f"Slack anchor post failed: {trigger_post}"
    trigger_ts = str(trigger_post.get("ts") or "")
    assert trigger_ts, f"Slack anchor missing ts: {trigger_post}"

    # Step 1: Real ACP trigger to OpenClaw — bypasses Slack Socket Mode entirely.
    trigger_msg = (
        f"[mctrl-mcp-e2e] Acknowledge receipt of dispatch task {bead_id}. "
        f"Reply with: ACK {bead_id}"
    )
    gw_resp = await _openclaw_agent_async(trigger_msg, timeout=120)
    gw_reply = _agent_reply_text(gw_resp)
    assert _agent_call_succeeded(gw_resp), f"Gateway trigger failed: {gw_resp}"
    print(f"\n[acp-trigger] ok=true reply={gw_reply!r}")

    # Step 2: Real dispatch via ai_orch.
    task = (
        f"Create a file named e2e-done.txt containing the text '{bead_id} done'. "
        "Then run: git add e2e-done.txt && git commit -m 'e2e: done'. Then stop."
    )
    mapping = dispatch(
        bead_id=bead_id,
        task=task,
        slack_trigger_ts=trigger_ts,
        slack_trigger_channel=_AI_GENERAL,
        agent_cli="minimax",
        registry_path=str(registry),
    )
    session_name = mapping.session_name
    print(f"\n[dispatch] session={session_name} worktree={mapping.worktree_path}")

    # Step 3: Wait for real tmux session to exit (8-minute timeout).
    deadline = time.monotonic() + 480
    while time.monotonic() < deadline:
        if (
            await _run_sync_cmd_async(
                ["tmux", "has-session", "-t", session_name],
                capture_output=True,
            )
        ).returncode != 0:
            break
        await _sleep(5)
    else:
        pytest.fail(f"tmux session {session_name!r} still alive after 8 minutes")

    # Step 4: Real reconciler — ZERO monkeypatching.
    emitted = await asyncio.to_thread(
        reconcile_registry_once,
        registry_path=str(registry),
        outbox_path=str(outbox),
        dead_letter_path=str(tmp_path / "outbox_dead_letter.jsonl"),
    )
    assert len(emitted) == 1, f"Expected 1 event, got {len(emitted)}: {emitted}"
    assert emitted[0]["event"] == "task_finished", (
        f"Expected task_finished but got {emitted[0]['event']} — "
        "agent may not have committed; check session logs"
    )
    assert emitted[0]["bead_id"] == bead_id

    # Capture git log evidence.
    final_mapping = get_mapping(bead_id, registry_path=str(registry))
    assert final_mapping is not None
    assert final_mapping.status == "finished"

    git_log = await _run_sync_cmd_async(
        ["git", "log", "--oneline", f"{final_mapping.start_sha}..HEAD"],
        cwd=final_mapping.worktree_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    agent_commits = git_log.stdout.strip()
    assert agent_commits, (
        f"git log {final_mapping.start_sha}..HEAD in {final_mapping.worktree_path} "
        "returned no commits — contradicts task_finished classification"
    )
    print(f"\n[evidence] agent commits:\n{agent_commits}")

    # Step 5: Drain outbox → deliver real Slack DM.
    delivered = await asyncio.to_thread(drain_outbox, outbox_path=str(outbox))
    print(f"\n[outbox] drained {delivered} messages")

    # Step 6: Poll Slack DM and thread reply — proof of notification delivery.
    dm_found = await _poll_for_text_with_tokens_async(
        [bot_token],
        _DM_CHANNEL,
        bead_id,
        ts_before,
        timeout=300,
    )
    assert dm_found, f"No DM mentioning {bead_id} in {_DM_CHANNEL} within 300s"
    print(f"\n[evidence] Slack DM confirmed in {_DM_CHANNEL} mentioning {bead_id}")

    thread_found = await _poll_for_thread_reply_async(
        bot_token,
        _AI_GENERAL,
        trigger_ts,
        bead_id,
        timeout=300,
    )
    assert thread_found, f"No thread reply mentioning {bead_id} under {_AI_GENERAL}/{trigger_ts} within 300s"
    print(f"\n[evidence] Slack thread reply confirmed in {_AI_GENERAL} for {bead_id}")
