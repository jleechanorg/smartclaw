from __future__ import annotations

from datetime import datetime, timedelta, timezone

from orchestration.session_registry import list_mappings

STALE_IN_PROGRESS_THRESHOLD = timedelta(hours=24)


def check_stale_beads(registry_path: str) -> list[str]:
    """Return in-progress bead IDs whose updated_at is older than 24 hours."""
    current_time = _utcnow()
    stale_bead_ids: list[str] = []

    for mapping in list_mappings(registry_path=registry_path):
        if mapping.status != "in_progress":
            continue

        updated_at = _parse_timestamp(mapping.updated_at)
        if updated_at is None:
            stale_bead_ids.append(mapping.bead_id)
            continue
        if current_time - updated_at < STALE_IN_PROGRESS_THRESHOLD:
            continue

        stale_bead_ids.append(mapping.bead_id)

    return stale_bead_ids


def _parse_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)
