"""Path utility functions for state directory management."""

from __future__ import annotations

from pathlib import Path


def ensure_state_dir(path: str | Path) -> Path:
    """Ensure a state directory exists, creating parents if needed.

    Args:
        path: The directory path to ensure exists.

    Returns:
        The Path object for the directory.
    """
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def ensure_parent_dir(file_path: str | Path) -> Path:
    """Ensure a parent directory exists for a file path.

    Args:
        file_path: The file path whose parent directory should exist.

    Returns:
        The Path object for the parent directory.
    """
    p = Path(file_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p.parent


# Backward compatibility alias
_ensure_state_dir = ensure_state_dir
