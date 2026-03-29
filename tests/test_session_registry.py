from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from orchestration.session_registry import (
    BeadSessionMapping,
    TaskLifecycleStatus,
    archive_terminal_mappings,
    get_mapping,
    list_mappings,
    update_mapping_status,
    upsert_mapping,
)


def _mapping(
    bead_id: str, status: TaskLifecycleStatus = "queued"
) -> BeadSessionMapping:
    return BeadSessionMapping.create(
        bead_id=bead_id,
        session_name=f"session-{bead_id}",
        worktree_path=f"/tmp/wt-{bead_id}",
        branch=f"feat/{bead_id}",
        agent_cli="codex",
        status=status,
    )


def test_upsert_and_get_mapping(tmp_path: Path) -> None:
    registry = tmp_path / "registry.jsonl"
    upsert_mapping(_mapping("ORCH-1"), registry_path=str(registry))

    found = get_mapping("ORCH-1", registry_path=str(registry))
    assert found is not None
    assert found.bead_id == "ORCH-1"
    assert found.session_name == "session-ORCH-1"


def test_upsert_replaces_existing_bead_id(tmp_path: Path) -> None:
    registry = tmp_path / "registry.jsonl"
    upsert_mapping(_mapping("ORCH-1", "queued"), registry_path=str(registry))
    upsert_mapping(_mapping("ORCH-1", "in_progress"), registry_path=str(registry))

    all_items = list_mappings(registry_path=str(registry))
    assert len(all_items) == 1
    assert all_items[0].status == "in_progress"


def test_update_mapping_status_returns_false_when_missing(tmp_path: Path) -> None:
    registry = tmp_path / "registry.jsonl"
    changed = update_mapping_status(
        "ORCH-missing",
        "needs_human",
        registry_path=str(registry),
    )
    assert changed is False


def test_update_mapping_status_updates_existing_record(tmp_path: Path) -> None:
    registry = tmp_path / "registry.jsonl"
    upsert_mapping(_mapping("ORCH-2", "queued"), registry_path=str(registry))

    changed = update_mapping_status(
        "ORCH-2",
        "done",
        registry_path=str(registry),
    )
    assert changed is True

    found = get_mapping("ORCH-2", registry_path=str(registry))
    assert found is not None
    assert found.status == "done"


def test_update_mapping_status_preserves_start_sha(tmp_path: Path) -> None:
    registry = tmp_path / "registry.jsonl"
    mapping = BeadSessionMapping.create(
        bead_id="ORCH-3",
        session_name="session-ORCH-3",
        worktree_path="/tmp/wt-ORCH-3",
        branch="feat/ORCH-3",
        agent_cli="codex",
        status="in_progress",
        start_sha="abc123def456",
    )
    upsert_mapping(mapping, registry_path=str(registry))

    update_mapping_status("ORCH-3", "done", registry_path=str(registry))

    found = get_mapping("ORCH-3", registry_path=str(registry))
    assert found is not None
    assert found.status == "done"
    assert found.start_sha == "abc123def456"


def test_update_mapping_status_cas_guard_blocks_second_writer(tmp_path: Path) -> None:
    """from_status acts as a compare-and-swap: second caller loses if status already changed."""
    registry = tmp_path / "registry.jsonl"
    upsert_mapping(_mapping("ORCH-4", "in_progress"), registry_path=str(registry))

    # First writer succeeds
    changed = update_mapping_status(
        "ORCH-4", "done", from_status="in_progress", registry_path=str(registry)
    )
    assert changed is True

    # Second writer (simulating a second reconciler process) fails the CAS guard
    changed = update_mapping_status(
        "ORCH-4", "done", from_status="in_progress", registry_path=str(registry)
    )
    assert changed is False


def test_slack_trigger_ts_none_is_normalized_on_create(tmp_path: Path) -> None:
    registry = tmp_path / "registry.jsonl"
    mapping = BeadSessionMapping.create(
        bead_id="ORCH-5",
        session_name="session-ORCH-5",
        worktree_path="/tmp/wt-ORCH-5",
        branch="feat/ORCH-5",
        agent_cli="codex",
        status="queued",
        slack_trigger_ts="None",
    )
    upsert_mapping(mapping, registry_path=str(registry))

    found = get_mapping("ORCH-5", registry_path=str(registry))
    assert found is not None
    assert found.slack_trigger_ts == ""


def test_archive_terminal_mappings_moves_old_dead_entries(tmp_path: Path) -> None:
    registry = tmp_path / "registry.jsonl"
    dead_worktree = tmp_path / "missing-worktree"
    mapping = BeadSessionMapping(
        bead_id="ORCH-old",
        session_name="session-ORCH-old",
        worktree_path=str(dead_worktree),
        branch="feat/ORCH-old",
        agent_cli="codex",
        status="finished",
        updated_at="2026-03-01T00:00:00+00:00",
        start_sha="abc123",
        slack_trigger_ts="",
    )
    upsert_mapping(mapping, registry_path=str(registry))

    archived = archive_terminal_mappings(
        registry_path=str(registry),
        archive_after_days=7,
        now=datetime(2026, 3, 10, tzinfo=timezone.utc),
    )

    assert archived == 1
    assert list_mappings(registry_path=str(registry)) == []

    archive_path = registry.with_name("registry.archive.jsonl")
    archived_items = list_mappings(registry_path=str(archive_path))
    assert len(archived_items) == 1
    assert archived_items[0].bead_id == "ORCH-old"


def test_archive_terminal_mappings_keeps_live_or_recent_entries(tmp_path: Path) -> None:
    registry = tmp_path / "registry.jsonl"
    live_worktree = tmp_path / "wt-live"
    live_worktree.mkdir()
    mapping = BeadSessionMapping(
        bead_id="ORCH-keep",
        session_name="session-ORCH-keep",
        worktree_path=str(live_worktree),
        branch="feat/ORCH-keep",
        agent_cli="codex",
        status="needs_human",
        updated_at="2026-03-09T00:00:00+00:00",
        start_sha="abc123",
        slack_trigger_ts="",
    )
    upsert_mapping(mapping, registry_path=str(registry))

    archived = archive_terminal_mappings(
        registry_path=str(registry),
        archive_after_days=7,
        now=datetime(2026, 3, 10, tzinfo=timezone.utc),
    )

    assert archived == 0
    kept = list_mappings(registry_path=str(registry))
    assert len(kept) == 1
    assert kept[0].bead_id == "ORCH-keep"
