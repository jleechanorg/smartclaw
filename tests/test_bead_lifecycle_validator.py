from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from orchestration.bead_lifecycle_validator import check_stale_beads
from orchestration.session_registry import BeadSessionMapping, upsert_mapping


def _mapping(*, bead_id: str, status: str, updated_at: str) -> BeadSessionMapping:
    return replace(
        BeadSessionMapping.create(
            bead_id=bead_id,
            session_name=f"session-{bead_id}",
            worktree_path=f"/tmp/wt-{bead_id}",
            branch=f"feat/{bead_id}",
            agent_cli="codex",
            status=status,
        ),
        updated_at=updated_at,
    )


def test_returns_in_progress_beads_older_than_24h(tmp_path: Path) -> None:
    registry = tmp_path / "registry.jsonl"
    now = datetime(2026, 3, 7, 20, 0, 0, tzinfo=timezone.utc)
    stale_time = (now - timedelta(hours=25)).isoformat(timespec="seconds")

    upsert_mapping(
        _mapping(bead_id="ORCH-stale", status="in_progress", updated_at=stale_time),
        registry_path=str(registry),
    )

    with patch("orchestration.bead_lifecycle_validator._utcnow", return_value=now):
        stale_beads = check_stale_beads(str(registry))

    assert stale_beads == ["ORCH-stale"]


def test_skips_recent_or_non_in_progress_beads(tmp_path: Path) -> None:
    registry = tmp_path / "registry.jsonl"
    now = datetime(2026, 3, 7, 20, 0, 0, tzinfo=timezone.utc)

    upsert_mapping(
        _mapping(
            bead_id="ORCH-recent",
            status="in_progress",
            updated_at=(now - timedelta(hours=23)).isoformat(timespec="seconds"),
        ),
        registry_path=str(registry),
    )
    upsert_mapping(
        _mapping(
            bead_id="ORCH-finished",
            status="finished",
            updated_at=(now - timedelta(hours=30)).isoformat(timespec="seconds"),
        ),
        registry_path=str(registry),
    )

    with patch("orchestration.bead_lifecycle_validator._utcnow", return_value=now):
        stale_beads = check_stale_beads(str(registry))

    assert stale_beads == []
