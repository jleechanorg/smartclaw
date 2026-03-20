"""JSON file utilities for atomic writes using temp+rename pattern."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_json_write(
    items: list[dict[str, Any]],
    target_path: str | Path,
    *,
    sort_keys: bool = True,
) -> None:
    """Write JSON lines atomically using temp file + rename.

    Uses os.replace for atomic operation on POSIX systems.

    Args:
        items: List of dictionaries to write as JSONL.
        target_path: Path to the target JSONL file.
        sort_keys: Whether to sort keys in JSON output.
    """
    target = Path(target_path)
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
                tmp.write(json.dumps(item, sort_keys=sort_keys))
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


def atomic_json_write_single(
    data: dict[str, Any],
    target_path: str | Path,
    *,
    sort_keys: bool = True,
) -> None:
    """Write a single JSON object atomically using temp file + rename.

    Args:
        data: Dictionary to write as JSON.
        target_path: Path to the target JSON file.
        sort_keys: Whether to sort keys in JSON output.
    """
    target = Path(target_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_fd, temp_path = tempfile.mkstemp(
        prefix=f"{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
        text=True,
    )
    try:
        with os.fdopen(temp_fd, "w", encoding="utf-8") as tmp:
            json.dump(data, tmp, sort_keys=sort_keys)
        os.replace(temp_path, target)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    """Read a JSONL file, returning a list of parsed dicts.

    Args:
        path: Path to the JSONL file.

    Returns:
        List of dictionaries parsed from each line.
    """
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []

    results: list[dict[str, Any]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            results.append(json.loads(line))
        except (json.JSONDecodeError, ValueError):
            continue
    return results
