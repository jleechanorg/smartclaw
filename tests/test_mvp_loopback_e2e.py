"""MVP loopback E2E tests.

test_e2e_registry_to_outbox_to_delivery
  Pure unit-level flow: registry → reconciler → outbox → drain.
  No network calls; fully deterministic.

test_slack_loopback_roundtrip  [REAL — always runs, requires Slack tokens + ai_orch in env]
  Full end-to-end proof through every real system path:
  1. jleechan posts trigger to #ai-slack-test (SLACK_USER_TOKEN)
  2. dispatch() spawns a real ai_orch agent in a real git worktree via real tmux
  3. Agent commits and exits — supervisor detects the real tmux session death
  4. reconcile_registry_once runs with ZERO monkeypatching
  5. OpenClaw notification handling posts real DM + threaded reply under trigger_ts
  6. Test polls DM + conversations.replies to assert both landed

  No monkeypatching of any kind.
  Real: ai_orch, tmux, git, Slack inbound, Slack outbound, OpenClaw notifier.
"""
from __future__ import annotations

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
from orchestration.smartclaw_notifier import (
    drain_outbox,
    read_outbox,
)
from orchestration.reconciliation import reconcile_registry_once
from orchestration.session_registry import BeadSessionMapping, get_mapping, upsert_mapping

MCTRL_ROOT = Path(__file__).resolve().parent.parent.parent
_DM_CHANNEL = os.environ.get("MCTRL_TEST_DM_CHANNEL", "")
_AI_GENERAL = os.environ.get("MCTRL_TEST_TRIGGER_CHANNEL", "")


def _slack_post(token: str, channel: str, text: str) -> dict[str, Any]:
    body = json.dumps({"channel": channel, "text": text}).encode()
    req = Request(
        "https://slack.com/api/chat.postMessage",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    with urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


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
    timeout: float = 20.0, interval: float = 2.0,
) -> bool:
    return _poll_for_matching_message(
        token, channel, needle, oldest, timeout=timeout, interval=interval
    ) is not None


def _poll_for_matching_message(
    token: str, channel: str, needle: str, oldest: str,
    timeout: float = 20.0, interval: float = 2.0,
) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            msgs = _slack_history(token, channel, oldest, limit=100)
            for message in msgs:
                if needle in message.get("text", ""):
                    return message
        except URLError:
            pass
        time.sleep(interval)
    return None


def _latest_matching_message(
    token: str, channel: str, needle: str, *, oldest: str = "0", limit: int = 200
) -> dict[str, Any] | None:
    try:
        msgs = _slack_history(token, channel, oldest, limit=limit)
    except URLError:
        return None
    for message in msgs:
        if needle in message.get("text", ""):
            return message
    return None


def _poll_for_thread_reply(
    token: str, channel: str, thread_ts: str, needle: str,
    timeout: float = 20.0, interval: float = 2.0,
) -> bool:
    """Poll conversations.replies for a message containing needle in the given thread."""
    return _poll_for_thread_reply_message(
        token, channel, thread_ts, needle, timeout=timeout, interval=interval
    ) is not None


def _poll_for_thread_reply_message(
    token: str, channel: str, thread_ts: str, needle: str,
    required_text: str | None = None,
    timeout: float = 20.0, interval: float = 2.0,
) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            url = (
                f"https://slack.com/api/conversations.replies"
                f"?channel={channel}&ts={thread_ts}&limit=20"
            )
            req = Request(url, headers={"Authorization": f"Bearer {token}"})
            with urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            msgs = data.get("messages") or []
            # Skip index 0 (the parent message itself); check replies
            for message in msgs[1:]:
                text = message.get("text", "")
                if needle in text and (required_text is None or required_text in text):
                    return message
        except URLError:
            pass
        time.sleep(interval)
    return None


def _write_json_artifact(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def test_slack_history_handles_null_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return b'{"ok":true,"messages":null}'

    monkeypatch.setattr("tests.test_mvp_loopback_e2e.urlopen", lambda req, timeout=10: _Resp())

    assert _slack_history("xoxb-test", _AI_GENERAL, "0") == []


def test_e2e_registry_to_outbox_to_delivery(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    registry = tmp_path / "registry.jsonl"
    outbox = tmp_path / "outbox.jsonl"

    upsert_mapping(
        BeadSessionMapping.create(
            bead_id="ORCH-e2e",
            session_name="session-e2e",
            worktree_path="/tmp/wt-e2e",
            branch="feat/orch-e2e",
            agent_cli="codex",
            status="in_progress",
        ),
        registry_path=str(registry),
    )

    # Supervisor sees missing session, so it should transition and queue loopback.
    # Unit test must not post real Slack or OpenClaw messages.
    # Stub the underlying sender (not notify_openclaw itself) so enqueue_outbox
    # still runs on failure — that is what this test exercises.
    monkeypatch.setattr("orchestration.reconciliation.run_tmux_sessions", lambda: set())
    monkeypatch.setattr("orchestration.smartclaw_notifier._send_via_mcp_agent_mail", lambda p: False)
    emitted = reconcile_registry_once(
        registry_path=str(registry),
        outbox_path=str(outbox),
        dead_letter_path=str(tmp_path / "outbox_dead_letter.jsonl"),
    )

    assert len(emitted) == 1
    assert emitted[0]["event"] == "task_needs_human"
    assert emitted[0]["bead_id"] == "ORCH-e2e"

    mapping = get_mapping("ORCH-e2e", registry_path=str(registry))
    assert mapping is not None
    assert mapping.status == "needs_human"

    queued = read_outbox(outbox_path=str(outbox))
    assert len(queued) == 1
    assert queued[0]["bead_id"] == "ORCH-e2e"

    delivered = drain_outbox(send_fn=lambda _: True, outbox_path=str(outbox))
    assert delivered == 1
    assert read_outbox(outbox_path=str(outbox)) == []


@pytest.mark.skipif(
    not os.environ.get("SLACK_USER_TOKEN") or not os.environ.get("SLACK_BOT_TOKEN"),
    reason="Requires SLACK_USER_TOKEN and SLACK_BOT_TOKEN (source ~/.profile and ~/.smartclaw/set-slack-env.sh)",
)
def test_slack_loopback_roundtrip(tmp_path: Path) -> None:
    """Full real-system loopback proof. No monkeypatching of any kind.

    Proves the complete chain through every real system path:
      1. jleechan posts trigger to #ai-slack-test (real Slack, SLACK_USER_TOKEN)
      2. dispatch() calls ai_orch → real tmux session + real git worktree
      3. Agent makes a real git commit and exits — tmux session dies for real
      4. reconcile_registry_once detects the real dead session (no patches)
      5. OpenClaw notification handling posts real DM + real threaded reply
      6. Poll Slack API to confirm DM and thread reply both landed

    Requires: SLACK_USER_TOKEN, SLACK_BOT_TOKEN, ai_orch in PATH
    Duration: 3–10 minutes (real agent execution time)
    """
    user_token = os.environ.get("SLACK_USER_TOKEN", "")
    bot_token = (
        os.environ.get("SLACK_BOT_TOKEN")
        or os.environ.get("SLACK_BOT_TOKEN")
        or ""
    )
    assert user_token, "SLACK_USER_TOKEN must be set (source ~/.profile)"
    assert bot_token, "SLACK_BOT_TOKEN must be set — bot token required to verify bot posted"
    assert subprocess.run(["which", "ai_orch"], capture_output=True).returncode == 0, (
        "ai_orch must be in PATH"
    )

    bead_id = f"ORCH-e2e-{uuid.uuid4().hex[:6]}"
    registry = tmp_path / "registry.jsonl"
    outbox = tmp_path / "outbox.jsonl"

    # Step 1: Post real trigger as jleechan.
    ts_before = str(time.time() - 2)
    trigger_text = f"[mctrl-e2e] dispatch test {bead_id}"
    result = _slack_post(user_token, _AI_GENERAL, trigger_text)
    assert result.get("ok"), f"Trigger post failed: {result.get('error')}"
    trigger_ts = result["ts"]

    # Step 2: Real dispatch via ai_orch. Creates real tmux session + real git worktree.
    # Task is minimal: write one file and commit so _worktree_has_commits detects it.
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

    # Step 3: Wait for the real tmux session to exit (agent finished).
    # Timeout 8 minutes — real agent execution for a trivial task.
    deadline = time.monotonic() + 480
    while time.monotonic() < deadline:
        if subprocess.run(
            ["tmux", "has-session", "-t", session_name],
            capture_output=True,
        ).returncode != 0:
            break
        time.sleep(5)
    else:
        pytest.fail(f"tmux session {session_name!r} still alive after 8 minutes")

    # Step 4: Run the real reconciler — ZERO monkeypatching.
    # It reads the real registry, checks real tmux (session is dead), checks real git.
    emitted = reconcile_registry_once(
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

    # Capture git log evidence: commits the agent made since spawn.
    final_mapping = get_mapping(bead_id, registry_path=str(registry))
    assert final_mapping is not None
    assert final_mapping.status == "finished"

    git_log_result = subprocess.run(
        ["git", "log", "--oneline", f"{final_mapping.start_sha}..HEAD"],
        cwd=final_mapping.worktree_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    agent_commits = git_log_result.stdout.strip()
    assert agent_commits, (
        f"git log {final_mapping.start_sha}..HEAD in {final_mapping.worktree_path} "
        "returned no commits — contradicts task_finished classification"
    )
    # Print for evidence capture in CI / test output
    print(f"\n[evidence] agent commits:\n{agent_commits}")

    # Step 5: Verify real Slack DM landed.
    dm_message = _poll_for_matching_message(bot_token, _DM_CHANNEL, bead_id, ts_before, timeout=360)
    if dm_message is None:
        dm_message = _latest_matching_message(bot_token, _DM_CHANNEL, bead_id, oldest="0", limit=200)
    assert dm_message is not None, f"No DM mentioning {bead_id} in {_DM_CHANNEL} within 360s"
    _write_json_artifact(
        tmp_path / "slack_dm_evidence.json",
        {
            "bead_id": bead_id,
            "channel": _DM_CHANNEL,
            "oldest": ts_before,
            "matched_ts": dm_message.get("ts", ""),
            "matched_text": dm_message.get("text", ""),
        },
    )

    # Step 6: Verify real threaded reply under original trigger.
    thread_message = _poll_for_thread_reply_message(
        bot_token,
        _AI_GENERAL,
        trigger_ts,
        bead_id,
        required_text="Ready for review.",
        timeout=360,
    )
    assert thread_message is not None, (
        f"No threaded reply mentioning {bead_id} under trigger {trigger_ts} "
        f"in {_AI_GENERAL} within 360s"
    )
    _write_json_artifact(
        tmp_path / "slack_thread_evidence.json",
        {
            "bead_id": bead_id,
            "channel": _AI_GENERAL,
            "thread_ts": trigger_ts,
            "matched_ts": thread_message.get("ts", ""),
            "matched_text": thread_message.get("text", ""),
        },
    )
