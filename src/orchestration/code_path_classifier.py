"""Shared path classification helpers."""


from __future__ import annotations


def is_code_path(filepath: str) -> bool:
    """Return True if a path requires evidence review."""
    code_prefixes = ("src/orchestration/", "scripts/", "lib/")
    code_files = ("SOUL.md", "TOOLS.md", "workspace/SOUL.md", "workspace/TOOLS.md")
    return any(filepath.startswith(prefix) for prefix in code_prefixes) or filepath in code_files

