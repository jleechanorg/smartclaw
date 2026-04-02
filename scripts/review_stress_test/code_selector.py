#!/usr/bin/env python3
"""Code Selector - picks ~5000 lines of coherent code from the repo."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from datetime import datetime


@dataclass
class CodeSlice:
    path: str
    line_count: int
    files: list[str]
    last_reviewed: Optional[datetime] = None


class CodeSelector:
    """Selects coherent code slices for AI review testing."""

    def __init__(self, repo_path: str, target_lines: int = 5000) -> None:
        self.repo_path = Path(repo_path)
        self.target_lines = target_lines
        self.coverage_file = self.repo_path / ".ai_review_coverage.json"

    def scan(self) -> list[CodeSlice]:
        """Scan repo, return slices by directory/module."""
        slices: list[CodeSlice] = []

        # Skip these directories
        skip_dirs = {'.git', 'node_modules', '__pycache__', '.pytest_cache',
                     'dist', 'build', '.venv', 'venv', 'media', 'memory'}

        # Only these extensions
        valid_exts = {'.py', '.ts', '.js', '.go', '.rs', '.sh', '.yaml', '.yml'}

        for dir_path in sorted(self.repo_path.iterdir()):
            if not dir_path.is_dir():
                continue
            if dir_path.name.startswith('.'):
                continue
            if dir_path.name in skip_dirs:
                continue

            files: list[str] = []
            total_lines = 0

            for root, _, filenames in os.walk(dir_path):
                # Skip nested skip dirs
                if any(s in root for s in skip_dirs):
                    continue

                for fname in filenames:
                    fpath = Path(root) / fname
                    if fpath.suffix not in valid_exts:
                        continue
                    # Skip tests
                    if 'test' in fname.lower() or '_test.' in fname:
                        continue
                    # Skip generated
                    if 'gen-' in fname or 'generated' in str(fpath):
                        continue

                    try:
                        with open(fpath) as f:
                            lines = len([l for l in f if l.strip() and not l.strip().startswith('#')])
                            if lines > 10:  # Skip very small files
                                files.append(str(fpath.relative_to(self.repo_path)))
                                total_lines += lines
                    except (OSError, UnicodeDecodeError):
                        continue

            if files:
                slices.append(CodeSlice(
                    path=dir_path.name,
                    line_count=total_lines,
                    files=files
                ))

        return sorted(slices, key=lambda s: s.line_count, reverse=True)

    def get_coverage(self) -> dict:
        """Load coverage tracking."""
        if self.coverage_file.exists():
            with open(self.coverage_file) as f:
                return json.load(f)
        return {"slices": {}, "total_runs": 0}

    def save_coverage(self, coverage: dict) -> None:
        """Save coverage tracking."""
        with open(self.coverage_file, 'w') as f:
            json.dump(coverage, f, indent=2)

    def select_next_slice(self) -> CodeSlice:
        """Return oldest unreviewed slice ~ target_lines."""
        slices = self.scan()
        coverage = self.get_coverage()

        # Find slice not reviewed recently (oldest first)
        for slice_ in slices:
            if slice_.path not in coverage.get("slices", {}):
                coverage["slices"] = coverage.get("slices", {})
                coverage["slices"][slice_.path] = {"last_reviewed": None, "runs": 0}

            last = coverage["slices"][slice_.path].get("last_reviewed")
            if not last:
                # Never reviewed - prioritize
                coverage["slices"][slice_.path]["last_reviewed"] = datetime.now().isoformat()
                coverage["slices"][slice_.path]["runs"] = coverage["slices"][slice_.path].get("runs", 0) + 1
                coverage["total_runs"] = coverage.get("total_runs", 0) + 1
                self.save_coverage(coverage)
                return slice_

        # All reviewed - pick oldest
        oldest = min(
            [(s, coverage["slices"].get(s.path, {}).get("last_reviewed", "2000-01-01")) for s in slices],
            key=lambda x: x[1]
        )[0]

        coverage["slices"][oldest.path]["last_reviewed"] = datetime.now().isoformat()
        coverage["slices"][oldest.path]["runs"] = coverage["slices"][oldest.path].get("runs", 0) + 1
        coverage["total_runs"] = coverage.get("total_runs", 0) + 1
        self.save_coverage(coverage)

        return oldest

    def get_coverage_report(self) -> dict:
        """Return % of codebase reviewed."""
        slices = self.scan()
        coverage = self.get_coverage()

        reviewed = sum(1 for s in slices if coverage["slices"].get(s.path, {}).get("last_reviewed"))
        total = len(slices)

        return {
            "reviewed_slices": reviewed,
            "total_slices": total,
            "coverage_pct": round(reviewed / total * 100, 1) if total else 0,
            "total_lines": sum(s.line_count for s in slices),
            "total_runs": coverage.get("total_runs", 0)
        }


if __name__ == "__main__":
    import sys
    repo = sys.argv[1] if len(sys.argv) > 1 else "."

    selector = CodeSelector(repo)
    slices = selector.scan()

    print(f"Found {len(slices)} code slices:")
    for s in slices[:10]:
        print(f"  {s.path}: {s.line_count} lines")

    print("\nSelecting next slice...")
    next_slice = selector.select_next_slice()
    print(f"  Selected: {next_slice.path} ({next_slice.line_count} lines)")

    print("\nCoverage:", selector.get_coverage_report())
