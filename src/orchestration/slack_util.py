"""Slack utility functions for normalizing Slack-related values."""

from __future__ import annotations


def normalize_slack_trigger_ts(value: object) -> str:
    """Return a usable Slack thread ts, treating None-like values as missing."""
    if value is None:
        return ""
    trigger_ts = str(value).strip()
    if not trigger_ts or trigger_ts.lower() == "none":
        return ""
    return trigger_ts


def normalize_slack_channel(value: object) -> str:
    """Return a usable Slack channel id, treating None-like values as missing."""
    if value is None:
        return ""
    trigger_channel = str(value).strip()
    if not trigger_channel or trigger_channel.lower() == "none":
        return ""
    return trigger_channel


# Backward compatibility aliases
_normalize_slack_trigger_ts = normalize_slack_trigger_ts
_normalize_slack_trigger_channel = normalize_slack_channel
