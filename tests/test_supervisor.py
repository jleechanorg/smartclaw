from __future__ import annotations

import importlib
import sys
from unittest.mock import MagicMock
from pathlib import Path


def test_invalid_archive_after_days_falls_back_to_default(monkeypatch) -> None:
    monkeypatch.setenv("MCTRL_ARCHIVE_AFTER_DAYS", "seven")
    sys.modules.pop("orchestration.supervisor", None)

    supervisor = importlib.import_module("orchestration.supervisor")

    assert supervisor.ARCHIVE_AFTER_DAYS == 7


def test_invalid_outbox_alert_env_falls_back_to_defaults(monkeypatch) -> None:
    monkeypatch.setenv("MCTRL_OUTBOX_ALERT_THRESHOLD", "many")
    monkeypatch.setenv("MCTRL_OUTBOX_AGE_ALERT_SECONDS", "old")
    monkeypatch.setenv("MCTRL_OUTBOX_ALERT_COOLDOWN_SECONDS", "someday")
    sys.modules.pop("orchestration.supervisor", None)

    supervisor = importlib.import_module("orchestration.supervisor")

    assert supervisor.OUTBOX_ALERT_THRESHOLD == 10
    assert supervisor.OUTBOX_AGE_ALERT_SECONDS == 3600
    assert supervisor.OUTBOX_ALERT_COOLDOWN_SECONDS == 3600


def test_outbox_alert_cooldown_suppresses_repeat_alerts(monkeypatch) -> None:
    sys.modules.pop("orchestration.supervisor", None)
    supervisor = importlib.import_module("orchestration.supervisor")
    supervisor._last_outbox_alert_at = 0.0
    alert = MagicMock(return_value=True)

    monkeypatch.setattr(supervisor, "time", MagicMock(monotonic=MagicMock(return_value=10_000.0)))

    fired_first = supervisor.maybe_alert_outbox_health(
        pending_count=20,
        dead_letter_count=0,
        oldest_age_seconds=8000,
        notify_fn=alert,
        threshold=10,
        age_threshold=3600,
        cooldown_seconds=3600,
    )
    fired_second = supervisor.maybe_alert_outbox_health(
        pending_count=21,
        dead_letter_count=0,
        oldest_age_seconds=8100,
        notify_fn=alert,
        threshold=10,
        age_threshold=3600,
        cooldown_seconds=3600,
    )

    assert fired_first is True
    assert fired_second is False
    assert alert.call_count == 1


def test_first_outbox_alert_bypasses_cooldown(monkeypatch) -> None:
    sys.modules.pop("orchestration.supervisor", None)
    supervisor = importlib.import_module("orchestration.supervisor")
    supervisor._last_outbox_alert_at = None
    alert = MagicMock(return_value=True)
    monkeypatch.setattr(supervisor, "time", MagicMock(monotonic=MagicMock(return_value=5.0)))

    fired = supervisor.maybe_alert_outbox_health(
        pending_count=50,
        dead_letter_count=0,
        oldest_age_seconds=7200,
        notify_fn=alert,
        threshold=10,
        age_threshold=3600,
        cooldown_seconds=3600,
    )

    assert fired is True
    assert alert.call_count == 1


def test_outbox_alert_fires_when_dead_letter_present(monkeypatch) -> None:
    sys.modules.pop("orchestration.supervisor", None)
    supervisor = importlib.import_module("orchestration.supervisor")
    supervisor._last_outbox_alert_at = 0.0
    alert = MagicMock(return_value=True)
    monkeypatch.setattr(supervisor, "time", MagicMock(monotonic=MagicMock(return_value=1000.0)))

    fired = supervisor.maybe_alert_outbox_health(
        pending_count=0,
        dead_letter_count=1,
        oldest_age_seconds=None,
        notify_fn=alert,
        threshold=10,
        age_threshold=3600,
        cooldown_seconds=10,
    )

    assert fired is True
    assert alert.call_count == 1


def test_outbox_alert_payload_uses_passed_paths(monkeypatch) -> None:
    sys.modules.pop("orchestration.supervisor", None)
    supervisor = importlib.import_module("orchestration.supervisor")
    supervisor._last_outbox_alert_at = None
    payloads: list[dict] = []

    def _notify(payload: dict) -> bool:
        payloads.append(payload)
        return True

    monkeypatch.setattr(supervisor, "time", MagicMock(monotonic=MagicMock(return_value=42.0)))

    fired = supervisor.maybe_alert_outbox_health(
        pending_count=12,
        dead_letter_count=0,
        oldest_age_seconds=4000,
        notify_fn=_notify,
        threshold=10,
        age_threshold=3600,
        cooldown_seconds=3600,
        outbox_path="/tmp/custom-outbox.jsonl",
        dead_letter_path="/tmp/custom-dead.jsonl",
    )

    assert fired is True
    assert len(payloads) == 1
    assert payloads[0]["outbox_path"] == "/tmp/custom-outbox.jsonl"
    assert payloads[0]["dead_letter_path"] == "/tmp/custom-dead.jsonl"


def test_registry_paths_to_reconcile_uses_env_paths(monkeypatch, tmp_path: Path) -> None:
    sys.modules.pop("orchestration.supervisor", None)
    supervisor = importlib.import_module("orchestration.supervisor")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)

    paths = supervisor._registry_paths_to_reconcile(
        registry_path=".tracking/bead_session_registry.jsonl",
        registry_paths_env=" /tmp/a.jsonl, /tmp/b.jsonl ",
        outbox_path="/tmp/messages/outbox.jsonl",
    )

    assert paths[0] == str((workspace / ".tracking" / "bead_session_registry.jsonl").resolve())
    assert str(Path("/tmp/a.jsonl").resolve()) in paths
    assert str(Path("/tmp/b.jsonl").resolve()) in paths


def test_registry_paths_to_reconcile_prefers_colon_separator_when_present(
    monkeypatch, tmp_path: Path
) -> None:
    sys.modules.pop("orchestration.supervisor", None)
    supervisor = importlib.import_module("orchestration.supervisor")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)

    paths = supervisor._registry_paths_to_reconcile(
        registry_path=".tracking/bead_session_registry.jsonl",
        registry_paths_env="/tmp/path,with,commas.jsonl:/tmp/ok.jsonl",
        outbox_path="/tmp/messages/outbox.jsonl",
    )

    assert str(Path("/tmp/path,with,commas.jsonl").resolve()) in paths
    assert str(Path("/tmp/ok.jsonl").resolve()) in paths


def test_registry_paths_to_reconcile_auto_discovers_sibling_registry(
    monkeypatch, tmp_path: Path
) -> None:
    sys.modules.pop("orchestration.supervisor", None)
    supervisor = importlib.import_module("orchestration.supervisor")

    workspace_root = tmp_path / "project"
    mctrl_repo = workspace_root / "mctrl"
    app_repo = workspace_root / "jleechanclaw"
    messages = tmp_path / "shared" / "messages"
    messages.mkdir(parents=True)

    for repo in (mctrl_repo, app_repo):
        (repo / ".tracking").mkdir(parents=True)
        (repo / ".tracking" / "bead_session_registry.jsonl").write_text("", encoding="utf-8")
        (repo / ".messages").symlink_to(messages, target_is_directory=True)

    monkeypatch.chdir(mctrl_repo)

    paths = supervisor._registry_paths_to_reconcile(
        registry_path=".tracking/bead_session_registry.jsonl",
        registry_paths_env="",
        outbox_path=str(messages / "outbox.jsonl"),
    )

    assert str((mctrl_repo / ".tracking" / "bead_session_registry.jsonl").resolve()) in paths
    assert str((app_repo / ".tracking" / "bead_session_registry.jsonl").resolve()) in paths


def test_registry_paths_auto_discovery_with_absolute_default_registry_path(
    monkeypatch, tmp_path: Path
) -> None:
    sys.modules.pop("orchestration.supervisor", None)
    supervisor = importlib.import_module("orchestration.supervisor")

    workspace_root = tmp_path / "project"
    mctrl_repo = workspace_root / "mctrl"
    app_repo = workspace_root / "jleechanclaw"
    messages = tmp_path / "shared" / "messages"
    messages.mkdir(parents=True)

    for repo in (mctrl_repo, app_repo):
        (repo / ".tracking").mkdir(parents=True)
        (repo / ".tracking" / "bead_session_registry.jsonl").write_text("", encoding="utf-8")
        (repo / ".messages").symlink_to(messages, target_is_directory=True)

    monkeypatch.chdir(mctrl_repo)
    absolute_default = str((mctrl_repo / ".tracking" / "bead_session_registry.jsonl").resolve())

    paths = supervisor._registry_paths_to_reconcile(
        registry_path=absolute_default,
        registry_paths_env="",
        outbox_path=str(messages / "outbox.jsonl"),
    )

    assert str((app_repo / ".tracking" / "bead_session_registry.jsonl").resolve()) in paths


def test_run_once_reconciles_each_registry(monkeypatch) -> None:
    sys.modules.pop("orchestration.supervisor", None)
    supervisor = importlib.import_module("orchestration.supervisor")

    monkeypatch.setattr(
        supervisor,
        "_registry_paths_to_reconcile",
        lambda **_: ["/tmp/reg-a.jsonl", "/tmp/reg-b.jsonl"],
    )

    reconcile_calls: list[str] = []
    archive_calls: list[str] = []

    def _reconcile(*, registry_path: str, outbox_path: str, dead_letter_path: str):
        reconcile_calls.append(registry_path)
        return [{"event": "task_finished", "bead_id": f"b-{len(reconcile_calls)}"}]

    def _archive(*, registry_path: str, archive_after_days: int):
        archive_calls.append(registry_path)
        return 1

    monkeypatch.setattr("orchestration.reconciliation.reconcile_registry_once", _reconcile)
    monkeypatch.setattr("orchestration.session_registry.archive_terminal_mappings", _archive)
    monkeypatch.setattr(
        "orchestration.openclaw_notifier.outbox_health_snapshot",
        lambda **_: {"pending_count": 0, "dead_letter_count": 0, "oldest_age_seconds": None},
    )
    monkeypatch.setattr(
        "orchestration.openclaw_notifier.notify_slack_outbox_alert",
        lambda payload: True,
    )

    emitted = supervisor.run_once()

    assert reconcile_calls == ["/tmp/reg-a.jsonl", "/tmp/reg-b.jsonl"]
    assert archive_calls == ["/tmp/reg-a.jsonl", "/tmp/reg-b.jsonl"]
    assert len(emitted) == 2


def test_run_once_continues_after_single_registry_failure(monkeypatch) -> None:
    sys.modules.pop("orchestration.supervisor", None)
    supervisor = importlib.import_module("orchestration.supervisor")

    monkeypatch.setattr(
        supervisor,
        "_registry_paths_to_reconcile",
        lambda **_: ["/tmp/reg-bad.jsonl", "/tmp/reg-good.jsonl"],
    )

    def _reconcile(*, registry_path: str, outbox_path: str, dead_letter_path: str):
        if registry_path == "/tmp/reg-bad.jsonl":
            raise RuntimeError("boom")
        return [{"event": "task_finished", "bead_id": "good"}]

    monkeypatch.setattr("orchestration.reconciliation.reconcile_registry_once", _reconcile)
    monkeypatch.setattr(
        "orchestration.session_registry.archive_terminal_mappings",
        lambda **_: 0,
    )
    monkeypatch.setattr(
        "orchestration.openclaw_notifier.outbox_health_snapshot",
        lambda **_: {"pending_count": 0, "dead_letter_count": 0, "oldest_age_seconds": None},
    )
    monkeypatch.setattr(
        "orchestration.openclaw_notifier.notify_slack_outbox_alert",
        lambda payload: True,
    )

    emitted = supervisor.run_once()

    assert len(emitted) == 1
    assert emitted[0]["bead_id"] == "good"
