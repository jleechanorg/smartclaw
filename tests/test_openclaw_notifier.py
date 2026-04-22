from __future__ import annotations

import json
import os
import time
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired
from unittest.mock import MagicMock, patch

from orchestration.smartclaw_notifier import (
    completion_notification_max_runtime_seconds,
    DEFAULT_DEAD_LETTER_PATH,
    SLACK_DM_CHANNEL,
    SLACK_TRIGGER_CHANNEL,
    drain_outbox,
    enqueue_dead_letter,
    enqueue_outbox,
    notify_openclaw,
    openclaw_notification_max_runtime_seconds,
    outbox_health_snapshot,
    read_dead_letter,
    notify_slack_started,
    notify_slack_done,
    read_outbox,
)


def test_notify_openclaw_success_does_not_enqueue(tmp_path: Path) -> None:
    outbox = tmp_path / "outbox.jsonl"
    payload = {"event": "task_finished", "bead_id": "ORCH-1"}

    delivered = notify_openclaw(
        payload,
        send_fn=lambda _: True,
        outbox_path=str(outbox),
    )

    assert delivered is True
    assert read_outbox(outbox_path=str(outbox)) == []


def test_notify_openclaw_failure_enqueues(tmp_path: Path) -> None:
    outbox = tmp_path / "outbox.jsonl"
    payload = {"event": "task_needs_human", "bead_id": "ORCH-2"}

    delivered = notify_openclaw(
        payload,
        send_fn=lambda _: False,
        outbox_path=str(outbox),
    )

    assert delivered is False
    queued = read_outbox(outbox_path=str(outbox))
    assert len(queued) == 1
    assert queued[0]["event"] == payload["event"]
    assert queued[0]["bead_id"] == payload["bead_id"]
    assert queued[0]["_retry_count"] == 0
    assert queued[0]["_first_queued_at"]


def test_enqueue_outbox_normalizes_none_like_slack_thread_fields(tmp_path: Path) -> None:
    outbox = tmp_path / "outbox.jsonl"

    enqueue_outbox(
        {
            "event": "task_finished",
            "bead_id": "ORCH-2b",
            "slack_trigger_ts": "None",
            "slack_trigger_channel": "None",
        },
        outbox_path=str(outbox),
    )

    queued = read_outbox(outbox_path=str(outbox))
    assert len(queued) == 1
    assert queued[0]["event"] == "task_finished"
    assert queued[0]["bead_id"] == "ORCH-2b"
    assert queued[0]["slack_trigger_ts"] == ""
    assert queued[0]["slack_trigger_channel"] == ""
    assert queued[0]["_retry_count"] == 0
    assert queued[0]["_first_queued_at"]


def test_drain_outbox_delivers_and_clears(tmp_path: Path) -> None:
    outbox = tmp_path / "outbox.jsonl"
    payloads = [
        {"event": "task_needs_human", "bead_id": "ORCH-1"},
        {"event": "task_finished", "bead_id": "ORCH-2"},
    ]
    for payload in payloads:
        notify_openclaw(payload, send_fn=lambda _: False, outbox_path=str(outbox))

    delivered = drain_outbox(send_fn=lambda _: True, outbox_path=str(outbox))

    assert delivered == 2
    assert read_outbox(outbox_path=str(outbox)) == []


def test_drain_outbox_increments_retry_count_on_failure(tmp_path: Path) -> None:
    outbox = tmp_path / "outbox.jsonl"
    payload = {"event": "task_needs_human", "bead_id": "ORCH-retry-1"}
    notify_openclaw(payload, send_fn=lambda _: False, outbox_path=str(outbox))

    delivered = drain_outbox(
        send_fn=lambda _: False,
        outbox_path=str(outbox),
        dead_letter_path=str(tmp_path / DEFAULT_DEAD_LETTER_PATH),
        retry_limit=3,
    )

    assert delivered == 0
    queued = read_outbox(outbox_path=str(outbox))
    assert len(queued) == 1
    assert queued[0]["_retry_count"] == 1
    assert queued[0]["bead_id"] == "ORCH-retry-1"
    assert read_dead_letter(dead_letter_path=str(tmp_path / DEFAULT_DEAD_LETTER_PATH)) == []


def test_drain_outbox_routes_to_dead_letter_after_retry_limit(tmp_path: Path) -> None:
    outbox = tmp_path / "outbox.jsonl"
    dead_letter = tmp_path / DEFAULT_DEAD_LETTER_PATH
    enqueue_outbox(
        {
            "event": "task_needs_human",
            "bead_id": "ORCH-retry-max",
            "_retry_count": 3,
            "_first_queued_at": "2026-03-01T00:00:00+00:00",
        },
        outbox_path=str(outbox),
    )

    delivered = drain_outbox(
        send_fn=lambda _: False,
        outbox_path=str(outbox),
        dead_letter_path=str(dead_letter),
        retry_limit=3,
    )

    assert delivered == 0
    assert read_outbox(outbox_path=str(outbox)) == []
    dead = read_dead_letter(dead_letter_path=str(dead_letter))
    assert len(dead) == 1
    assert dead[0]["bead_id"] == "ORCH-retry-max"
    assert dead[0]["_retry_count"] == 4


def test_drain_outbox_derives_dead_letter_path_from_outbox_path(tmp_path: Path) -> None:
    outbox = tmp_path / "custom_outbox.jsonl"
    expected_dead_letter = tmp_path / "outbox_dead_letter.jsonl"
    enqueue_outbox(
        {
            "event": "task_needs_human",
            "bead_id": "ORCH-derived-dead",
            "_retry_count": 3,
        },
        outbox_path=str(outbox),
    )

    drain_outbox(
        send_fn=lambda _: False,
        outbox_path=str(outbox),
        retry_limit=3,
    )

    dead = read_dead_letter(dead_letter_path=str(expected_dead_letter))
    assert len(dead) == 1
    assert dead[0]["bead_id"] == "ORCH-derived-dead"


def test_drain_outbox_dead_letter_alert_includes_total_count_and_oldest_age(tmp_path: Path) -> None:
    outbox = tmp_path / "outbox.jsonl"
    dead_letter = tmp_path / "dead_letter.jsonl"

    enqueue_dead_letter(
        {
            "event": "task_needs_human",
            "bead_id": "ORCH-existing-dead",
            "_retry_count": 4,
            "_first_queued_at": "2026-03-01T00:00:00+00:00",
        },
        dead_letter_path=str(dead_letter),
    )
    enqueue_outbox(
        {
            "event": "task_needs_human",
            "bead_id": "ORCH-new-dead",
            "_retry_count": 3,
            "_first_queued_at": "2026-03-01T00:00:00+00:00",
        },
        outbox_path=str(outbox),
    )

    captured: list[dict] = []

    with patch("orchestration.smartclaw_notifier.notify_slack_outbox_alert", side_effect=lambda p: captured.append(dict(p)) or True):
        drain_outbox(
            send_fn=lambda _: False,
            outbox_path=str(outbox),
            dead_letter_path=str(dead_letter),
            retry_limit=3,
        )

    assert len(captured) == 1
    assert captured[0]["dead_letter_count"] == 2
    assert captured[0]["oldest_age_seconds"] is not None


def test_drain_outbox_uses_snapshot_mtime_for_legacy_first_queued_at(tmp_path: Path) -> None:
    outbox = tmp_path / "outbox.jsonl"
    enqueue_outbox(
        {
            "event": "task_needs_human",
            "bead_id": "ORCH-legacy-age",
            "_retry_count": 0,
            "_first_queued_at": "",
        },
        outbox_path=str(outbox),
    )
    stale_ts = time.time() - 7200
    os.utime(outbox, (stale_ts, stale_ts))

    drain_outbox(
        send_fn=lambda _: False,
        outbox_path=str(outbox),
        retry_limit=10,
    )
    queued = read_outbox(outbox_path=str(outbox))
    assert len(queued) == 1
    first_queued = queued[0]["_first_queued_at"]
    assert first_queued
    # Should preserve stale age signal rather than resetting to "now".
    assert "T" in first_queued


def test_outbox_health_snapshot_reports_pending_dead_letter_and_histogram(tmp_path: Path) -> None:
    outbox = tmp_path / "outbox.jsonl"
    dead_letter = tmp_path / "outbox_dead_letter.jsonl"

    enqueue_outbox({"event": "task_finished", "bead_id": "ORCH-h1"}, outbox_path=str(outbox))
    enqueue_outbox(
        {
            "event": "task_finished",
            "bead_id": "ORCH-h2",
            "_retry_count": 2,
            "_first_queued_at": "2026-03-01T00:00:00+00:00",
        },
        outbox_path=str(outbox),
    )
    enqueue_dead_letter(
        {"event": "task_finished", "bead_id": "ORCH-dead"},
        dead_letter_path=str(dead_letter),
    )

    snapshot = outbox_health_snapshot(
        outbox_path=str(outbox),
        dead_letter_path=str(dead_letter),
    )

    assert snapshot["pending_count"] == 2
    assert snapshot["dead_letter_count"] == 1
    assert snapshot["oldest_age_seconds"] is not None
    assert snapshot["retry_histogram"]["0"] == 1
    assert snapshot["retry_histogram"]["2"] == 1


def test_openclaw_notification_max_runtime_seconds_matches_single_attempt_budget() -> None:
    assert openclaw_notification_max_runtime_seconds() == 60


def test_completion_notification_max_runtime_seconds_includes_slack_and_openclaw() -> None:
    assert completion_notification_max_runtime_seconds() == 70


def _make_urlopen_mock(ok: bool = True):
    """Return a mock urlopen context manager that returns Slack ok/error JSON."""
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps({"ok": ok}).encode()
    mock_resp.__enter__ = lambda s: mock_resp
    mock_resp.__exit__ = MagicMock(return_value=False)
    return MagicMock(return_value=mock_resp)


@patch.dict("os.environ", {"SLACK_BOT_TOKEN": "xoxb-test"}, clear=False)
@patch("orchestration.smartclaw_notifier.urlopen")
def test_notify_slack_started_posts_dm(mock_urlopen) -> None:
    mock_urlopen.side_effect = _make_urlopen_mock()
    payload = {
        "bead_id": "ORCH-1",
        "session": "ai-test-abc",
        "branch": "feat/x",
        "worktree_path": "/tmp/wt-x",
        "agent_cli": "minimax",
        "slack_trigger_ts": "",
    }

    result = notify_slack_started(payload)

    assert result is True
    assert mock_urlopen.call_count == 1  # DM only (no trigger_ts)
    body = json.loads(mock_urlopen.call_args.args[0].data)
    assert body["channel"] == SLACK_DM_CHANNEL
    assert "ORCH-1" in body["text"]
    assert ":rocket:" in body["text"]


@patch.dict("os.environ", {"SLACK_BOT_TOKEN": "xoxb-test"}, clear=False)
@patch("orchestration.smartclaw_notifier.urlopen")
def test_notify_slack_outbox_alert_uses_dead_letter_message_when_count_present(mock_urlopen) -> None:
    from orchestration.smartclaw_notifier import notify_slack_outbox_alert

    mock_urlopen.side_effect = _make_urlopen_mock()
    result = notify_slack_outbox_alert(
        {
            "pending_count": 1,
            "dead_letter_count": 3,
            "outbox_path": "/tmp/outbox.jsonl",
            "dead_letter_path": "/tmp/dead.jsonl",
        }
    )

    assert result is True
    body = json.loads(mock_urlopen.call_args.args[0].data)
    assert "dead-lettered events" in body["text"]
    assert "Dead-letter queue count: `3`" in body["text"]


@patch.dict("os.environ", {"SLACK_BOT_TOKEN": "xoxb-test"}, clear=False)
@patch("orchestration.smartclaw_notifier.urlopen")
def test_notify_slack_started_threads_under_trigger(mock_urlopen) -> None:
    mock_urlopen.side_effect = _make_urlopen_mock()
    payload = {
        "bead_id": "ORCH-2",
        "session": "ai-test-def",
        "branch": "feat/y",
        "worktree_path": "/tmp/wt-y",
        "agent_cli": "minimax",
        "slack_trigger_ts": "1234567890.123456",
        "slack_trigger_channel": "C999TRIGGER",
    }

    result = notify_slack_started(payload)

    assert result is True
    assert mock_urlopen.call_count == 2  # DM + thread reply
    calls = [json.loads(c.args[0].data) for c in mock_urlopen.call_args_list]
    assert any(c.get("thread_ts") == "1234567890.123456" for c in calls)
    assert any(c["channel"] == "C999TRIGGER" for c in calls)


@patch.dict("os.environ", {"SLACK_BOT_TOKEN": "xoxb-test"}, clear=False)
@patch("orchestration.smartclaw_notifier.urlopen")
def test_notify_slack_started_skips_thread_reply_without_trigger_channel(mock_urlopen) -> None:
    mock_urlopen.side_effect = _make_urlopen_mock()

    result = notify_slack_started({
        "bead_id": "ORCH-no-channel-start",
        "session": "ai-test-no-channel",
        "branch": "feat/no-channel",
        "worktree_path": "/tmp/wt-no-channel",
        "agent_cli": "claude",
        "slack_trigger_ts": "1234567890.123456",
        "slack_trigger_channel": "",
    })

    assert result is True
    assert mock_urlopen.call_count == 1
    body = json.loads(mock_urlopen.call_args.args[0].data)
    assert body["channel"] == SLACK_DM_CHANNEL


@patch.dict("os.environ", {"SLACK_BOT_TOKEN": "xoxb-test"}, clear=False)
@patch("orchestration.smartclaw_notifier.urlopen")
def test_notify_slack_started_ignores_none_trigger(mock_urlopen) -> None:
    mock_urlopen.side_effect = _make_urlopen_mock()

    result = notify_slack_started({
        "bead_id": "ORCH-none-start",
        "session": "ai-test-none",
        "branch": "feat/none",
        "worktree_path": "/tmp/wt-none",
        "agent_cli": "claude",
        "slack_trigger_ts": None,
    })

    assert result is True
    assert mock_urlopen.call_count == 1
    body = json.loads(mock_urlopen.call_args.args[0].data)
    assert body["channel"] == SLACK_DM_CHANNEL


@patch.dict("os.environ", {}, clear=True)
def test_notify_slack_started_no_token_returns_false() -> None:
    result = notify_slack_started({"bead_id": "ORCH-3"})
    assert result is False


@patch.dict("os.environ", {"SLACK_BOT_TOKEN": "xoxb-test"}, clear=False)
@patch("orchestration.smartclaw_notifier.urlopen")
def test_notify_slack_started_includes_agent_cli_and_session(mock_urlopen) -> None:
    mock_urlopen.side_effect = _make_urlopen_mock()
    payload = {
        "bead_id": "ORCH-4",
        "session": "ai-minimax-xyz",
        "branch": "feat/z",
        "worktree_path": "/tmp/wt-z",
        "agent_cli": "minimax",
        "slack_trigger_ts": "",
    }

    notify_slack_started(payload)

    body = json.loads(mock_urlopen.call_args.args[0].data)
    assert "ai-minimax-xyz" in body["text"]
    assert "minimax" in body["text"]


@patch.dict("os.environ", {"SLACK_BOT_TOKEN": "xoxb-test"}, clear=False)
@patch("orchestration.smartclaw_notifier.urlopen")
def test_notify_slack_done_ignores_none_trigger(mock_urlopen) -> None:
    mock_urlopen.side_effect = _make_urlopen_mock()

    result = notify_slack_done({
        "event": "task_finished",
        "bead_id": "ORCH-none-done",
        "branch": "feat/none",
        "worktree_path": "/tmp/wt-none",
        "session": "ai-test-none",
        "slack_trigger_ts": None,
    })

    assert result is True
    assert mock_urlopen.call_count == 1
    body = json.loads(mock_urlopen.call_args.args[0].data)
    assert body["channel"] == SLACK_DM_CHANNEL


@patch.dict("os.environ", {"SLACK_BOT_TOKEN": "xoxb-test"}, clear=False)
@patch("orchestration.smartclaw_notifier.urlopen")
def test_notify_slack_done_reports_local_only_commits(mock_urlopen) -> None:
    mock_urlopen.side_effect = _make_urlopen_mock()

    result = notify_slack_done({
        "event": "task_needs_human",
        "bead_id": "ORCH-stranded",
        "branch": "feat/stranded",
        "worktree_path": "/tmp/wt-stranded",
        "session": "ai-test-stranded",
        "action_required": "push_or_salvage",
        "slack_trigger_ts": "1234567890.123456",
        "slack_trigger_channel": "C999TRIGGER",
    })

    assert result is True
    assert mock_urlopen.call_count == 2
    bodies = [json.loads(c.args[0].data) for c in mock_urlopen.call_args_list]
    assert any("did not push to a configured remote" in body["text"] for body in bodies)
    assert any(body.get("thread_ts") == "1234567890.123456" for body in bodies)
    assert any(body.get("channel") == "C999TRIGGER" for body in bodies)


@patch.dict("os.environ", {"SLACK_BOT_TOKEN": "xoxb-test"}, clear=False)
@patch("orchestration.smartclaw_notifier.urlopen")
def test_notify_slack_done_skips_thread_reply_without_trigger_channel(mock_urlopen) -> None:
    mock_urlopen.side_effect = _make_urlopen_mock()

    result = notify_slack_done({
        "event": "task_finished",
        "bead_id": "ORCH-no-channel-done",
        "branch": "feat/no-channel",
        "worktree_path": "/tmp/wt-no-channel",
        "session": "ai-test-no-channel",
        "slack_trigger_ts": "1234567890.123456",
        "slack_trigger_channel": "",
    })

    assert result is True
    assert mock_urlopen.call_count == 1
    body = json.loads(mock_urlopen.call_args.args[0].data)
    assert body["channel"] == SLACK_DM_CHANNEL


@patch.dict("os.environ", {"OPENCLAW_NOTIFY_AGENT": "smartclaw"}, clear=False)
@patch("orchestration.smartclaw_notifier.subprocess.run")
def test_notify_openclaw_uses_openclaw_agent_when_configured(mock_run, tmp_path: Path) -> None:
    outbox = tmp_path / "outbox.jsonl"
    payload = {"event": "task_finished", "bead_id": "ORCH-9"}
    mock_run.return_value = CompletedProcess(args=["openclaw"], returncode=0)

    delivered = notify_openclaw(payload, outbox_path=str(outbox))

    assert delivered is True
    assert mock_run.call_args.args[0][:4] == [
        "openclaw",
        "agent",
        "--agent",
        "smartclaw",
    ]
    assert read_outbox(outbox_path=str(outbox)) == []


@patch.dict("os.environ", {}, clear=True)
@patch("orchestration.smartclaw_notifier.subprocess.run")
def test_notify_openclaw_defaults_agent_name_to_main(mock_run, tmp_path: Path) -> None:
    outbox = tmp_path / "outbox.jsonl"
    payload = {"event": "task_finished", "bead_id": "ORCH-default-agent"}
    mock_run.return_value = CompletedProcess(
        args=["openclaw", "agent", "--agent", "main"], returncode=0
    )

    delivered = notify_openclaw(payload, outbox_path=str(outbox))

    assert delivered is True
    assert mock_run.call_args.args[0][:4] == ["openclaw", "agent", "--agent", "main"]
    assert read_outbox(outbox_path=str(outbox)) == []


@patch.dict(
    "os.environ",
    {
        "OPENCLAW_PROJECT_KEY": "project-x",
        "OPENCLAW_SENDER_NAME": "sender-x",
        "OPENCLAW_TO": "receiver-x",
    },
    clear=False,
)
@patch("orchestration.smartclaw_notifier._send_via_openclaw_agent", return_value=False)
@patch("orchestration.smartclaw_notifier.subprocess.run")
def test_notify_openclaw_mcp_fallback_handles_timeout(mock_run, _mock_agent, tmp_path: Path) -> None:
    outbox = tmp_path / "outbox.jsonl"
    payload = {"event": "task_finished", "bead_id": "ORCH-timeout"}
    mock_run.side_effect = TimeoutExpired(cmd=["openclaw"], timeout=30)

    delivered = notify_openclaw(payload, outbox_path=str(outbox))

    assert delivered is False
    queued = read_outbox(outbox_path=str(outbox))
    assert len(queued) == 1
    assert queued[0]["bead_id"] == "ORCH-timeout"


@patch.dict(
    "os.environ",
    {
        "OPENCLAW_PROJECT_KEY": "project-x",
        "OPENCLAW_SENDER_NAME": "sender-x",
        "OPENCLAW_TO": "receiver-x",
    },
    clear=False,
)
@patch("orchestration.smartclaw_notifier._send_via_openclaw_agent")
@patch("orchestration.smartclaw_notifier.subprocess.run")
def test_notify_openclaw_mcp_fallback_runs_when_agent_call_raises(
    mock_run, mock_agent_call, tmp_path: Path
) -> None:
    outbox = tmp_path / "outbox.jsonl"
    payload = {"event": "task_finished", "bead_id": "ORCH-agent-exc"}
    mock_agent_call.side_effect = FileNotFoundError("openclaw")
    mock_run.return_value = CompletedProcess(args=["openclaw", "mcp"], returncode=0)

    delivered = notify_openclaw(payload, outbox_path=str(outbox))

    assert delivered is True
    assert mock_run.called
    assert read_outbox(outbox_path=str(outbox)) == []


@patch.dict(
    "os.environ",
    {
        "OPENCLAW_PROJECT_KEY": "project-x",
        "OPENCLAW_SENDER_NAME": "sender-x",
        "OPENCLAW_TO": "receiver-x",
    },
    clear=False,
)
@patch("orchestration.smartclaw_notifier._send_via_openclaw_agent")
@patch("orchestration.smartclaw_notifier.subprocess.run")
def test_notify_openclaw_mcp_fallback_runs_when_agent_call_raises_runtime_error(
    mock_run, mock_agent_call, tmp_path: Path
) -> None:
    outbox = tmp_path / "outbox.jsonl"
    payload = {"event": "task_finished", "bead_id": "ORCH-agent-runtime-exc"}
    mock_agent_call.side_effect = RuntimeError("agent parse failure")
    mock_run.return_value = CompletedProcess(args=["openclaw", "mcp"], returncode=0)

    delivered = notify_openclaw(payload, outbox_path=str(outbox))

    assert delivered is True
    assert mock_run.called
    assert read_outbox(outbox_path=str(outbox)) == []
