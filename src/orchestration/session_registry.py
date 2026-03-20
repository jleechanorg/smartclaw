from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from orchestration.datetime_util import parse_iso8601, utcnow_iso_seconds
from orchestration.slack_util import normalize_slack_channel, normalize_slack_trigger_ts

TaskLifecycleStatus = Literal["queued", "in_progress", "needs_human", "finished"]

DEFAULT_REGISTRY_PATH = ".tracking/bead_session_registry.jsonl"
DEFAULT_ARCHIVE_AFTER_DAYS = 7

# Process-local lock for registry updates (not needed for cross-process atomicity
# since os.replace is atomic, but prevents race conditions within a process)
_registry_lock = threading.Lock()


@dataclass(frozen=True)
class BeadSessionMapping:
    bead_id: str
    session_name: str
    worktree_path: str
    branch: str
    agent_cli: str
    status: TaskLifecycleStatus
    updated_at: str
    # SHA of HEAD at spawn time — used to detect new commits by the agent.
    # Empty string for legacy entries that predate this field.
    start_sha: str = ""
    # Slack ts of the original trigger message — used to thread the completion
    # reply under that message. Empty for non-Slack tasks.
    slack_trigger_ts: str = ""
    # Slack channel ID where the trigger message was posted — used to ensure
    # thread replies go to the originating channel. Empty falls back to the
    # default SLACK_TRIGGER_CHANNEL constant in the notifier.
    slack_trigger_channel: str = ""

    @classmethod
    def create(
        cls,
        *,
        bead_id: str,
        session_name: str,
        worktree_path: str,
        branch: str,
        agent_cli: str,
        status: TaskLifecycleStatus,
        start_sha: str = "",
        slack_trigger_ts: str = "",
        slack_trigger_channel: str = "",
    ) -> BeadSessionMapping:
        return cls(
            bead_id=bead_id,
            session_name=session_name,
            worktree_path=worktree_path,
            branch=branch,
            agent_cli=agent_cli,
            status=status,
            updated_at=utcnow_iso_seconds(),
            start_sha=start_sha,
            slack_trigger_ts=normalize_slack_trigger_ts(slack_trigger_ts),
            slack_trigger_channel=normalize_slack_channel(slack_trigger_channel),
        )

    @classmethod
    def from_dict(cls, payload: dict[str, str]) -> BeadSessionMapping:
        return cls(
            bead_id=str(payload["bead_id"]),
            session_name=str(payload["session_name"]),
            worktree_path=str(payload["worktree_path"]),
            branch=str(payload["branch"]),
            agent_cli=str(payload["agent_cli"]),
            status=str(payload["status"]),  # type: ignore[arg-type]
            updated_at=str(payload["updated_at"]),
            start_sha=str(payload.get("start_sha", "")),
            slack_trigger_ts=normalize_slack_trigger_ts(payload.get("slack_trigger_ts", "")),
            slack_trigger_channel=normalize_slack_channel(payload.get("slack_trigger_channel", "")),
        )


def upsert_mapping(
    mapping: BeadSessionMapping,
    *,
    registry_path: str = DEFAULT_REGISTRY_PATH,
) -> None:
    with _registry_lock:
        by_bead: dict[str, BeadSessionMapping] = {
            item.bead_id: item for item in list_mappings(registry_path=registry_path)
        }
        by_bead[mapping.bead_id] = mapping
        _write_all(list(by_bead.values()), registry_path=registry_path)


def update_mapping_status(
    bead_id: str,
    status: TaskLifecycleStatus,
    *,
    from_status: TaskLifecycleStatus | None = None,
    registry_path: str = DEFAULT_REGISTRY_PATH,
) -> bool:
    with _registry_lock:
        items = list_mappings(registry_path=registry_path)
        found = False
        updated_items: list[BeadSessionMapping] = []

        for item in items:
            if item.bead_id == bead_id:
                # CAS guard: if from_status is given, only update if current
                # status matches — prevents double-notification when two
                # reconciler processes overlap.
                if from_status is not None and item.status != from_status:
                    return False
                found = True
                updated_items.append(
                    replace(item, status=status, updated_at=utcnow_iso_seconds())
                )
            else:
                updated_items.append(item)

        if not found:
            return False
        _write_all(updated_items, registry_path=registry_path)
        return True


def get_mapping(
    bead_id: str,
    *,
    registry_path: str = DEFAULT_REGISTRY_PATH,
) -> BeadSessionMapping | None:
    for item in list_mappings(registry_path=registry_path):
        if item.bead_id == bead_id:
            return item
    return None


def list_mappings(*, registry_path: str = DEFAULT_REGISTRY_PATH) -> list[BeadSessionMapping]:
    path = Path(registry_path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []

    results: list[BeadSessionMapping] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
            results.append(BeadSessionMapping.from_dict(payload))
        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
            # Skip malformed lines to allow reconciliation to continue
            continue
    return results


def _write_all(
    items: list[BeadSessionMapping],
    *,
    registry_path: str,
) -> None:
    target = Path(registry_path)
    target.parent.mkdir(parents=True, exist_ok=True)

    temp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=f"{target.name}.",
            suffix=".tmp",
            dir=str(target.parent),
            delete=False,
        ) as tmp:
            for item in items:
                tmp.write(json.dumps(asdict(item), sort_keys=True))
                tmp.write("\n")
            temp_path = tmp.name
        os.replace(temp_path, target)
        temp_path = None
    finally:
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def archive_terminal_mappings(
    *,
    registry_path: str = DEFAULT_REGISTRY_PATH,
    archive_after_days: int = DEFAULT_ARCHIVE_AFTER_DAYS,
    now: datetime | None = None,
) -> int:
    """Archive old terminal mappings whose worktrees no longer exist."""
    if archive_after_days < 0:
        return 0

    cutoff = (now or datetime.now(tz=timezone.utc)) - timedelta(days=archive_after_days)
    archive_path = _archive_path_for(registry_path)

    with _registry_lock:
        items = list_mappings(registry_path=registry_path)
        keep: list[BeadSessionMapping] = []
        archive: list[BeadSessionMapping] = []

        for item in items:
            if (
                item.status in {"finished", "needs_human"}
                and parse_iso8601(item.updated_at) <= cutoff
                and not Path(item.worktree_path).exists()
            ):
                archive.append(item)
            else:
                keep.append(item)

        if not archive:
            return 0

        archived_items = _read_archive(archive_path)
        archived_items.extend(archive)
        _write_all(keep, registry_path=registry_path)
        _write_all(archived_items, registry_path=str(archive_path))
        return len(archive)


def _archive_path_for(registry_path: str) -> Path:
    path = Path(registry_path)
    if path.suffix == ".jsonl":
        return path.with_name(f"{path.stem}.archive{path.suffix}")
    return path.with_name(f"{path.name}.archive")


def _read_archive(path: Path) -> list[BeadSessionMapping]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    results: list[BeadSessionMapping] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            results.append(BeadSessionMapping.from_dict(json.loads(line)))
        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
            continue
    return results
