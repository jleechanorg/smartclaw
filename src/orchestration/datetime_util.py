"""Datetime utility functions for UTC timestamps and ISO8601 parsing."""

from __future__ import annotations

import time
from datetime import datetime, timezone


def utcnow_iso() -> str:
    """Return current UTC time as ISO format string."""
    return datetime.now(tz=timezone.utc).isoformat()


def utcnow_iso_seconds() -> str:
    """Return current UTC time as ISO format string with seconds precision."""
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def age_seconds_from_iso(value: object) -> int | None:
    """Calculate age in seconds from an ISO8601 timestamp string."""
    try:
        text = str(value).strip()
        if not text:
            return None
        ts = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return max(0, int(time.time() - ts.timestamp()))
    except (TypeError, ValueError):
        return None


def parse_iso8601(value: str) -> datetime:
    """Parse ISO8601 string to datetime, handling naive datetimes as UTC."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def parse_ts(value: object) -> datetime | None:
    """Parse a timestamp value to datetime, or None if invalid."""
    if value is None:
        return None
    try:
        text = str(value).strip()
        if not text:
            return None
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


# Backward compatibility aliases
_utcnow_iso = utcnow_iso
_age_seconds_from_iso = age_seconds_from_iso
_parse_iso8601 = parse_iso8601
