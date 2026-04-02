#!/usr/bin/env python3.13
"""
5% stratified sample ingest of February 2026 sessions into mem0.

Takes every Nth file from the Feb session list (sorted by mtime) so the
sample is spread evenly across the month rather than front-loaded.

Usage:
    python3.13 scripts/ingest_feb_sample.py [--pct 5] [--workers 4] [--dry-run]
"""
from __future__ import annotations

import argparse
import glob
import os
import sys
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# Allow running from repo root
sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.mem0_extract_facts import process_session  # type: ignore

FEB_START = datetime(2026, 2, 1).timestamp()
FEB_END   = datetime(2026, 3, 1).timestamp()

CLAUDE_GLOB  = str(Path.home() / ".claude/projects/*/*.jsonl")
CODEX_GLOBS  = [
    str(Path.home() / ".codex/sessions/*.jsonl"),
    str(Path.home() / ".codex/archived_sessions/*.jsonl"),
    str(Path.home() / ".codex/sessions_archive/*.jsonl"),
]


def collect_feb_sessions() -> list[Path]:
    """Return all session files modified in February 2026, sorted by mtime."""
    paths: list[Path] = []
    for pattern in [CLAUDE_GLOB] + CODEX_GLOBS:
        for f in glob.glob(pattern):
            mtime = os.path.getmtime(f)
            if FEB_START <= mtime < FEB_END:
                paths.append(Path(f))
    paths.sort(key=lambda p: os.path.getmtime(p))
    return paths


def stratified_sample(paths: list[Path], pct: float) -> list[Path]:
    """Pick every Nth file to get ~pct% spread evenly across the list."""
    if pct >= 100:
        return paths
    step = max(1, int(100 / pct))
    return paths[::step]


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest a % sample of Feb sessions into mem0")
    parser.add_argument("--pct",     type=float, default=5.0,  help="Percentage to ingest (default 5)")
    parser.add_argument("--workers", type=int,   default=4,    help="Parallel workers (default 4)")
    parser.add_argument("--dry-run", action="store_true",       help="List sessions without ingesting")
    args = parser.parse_args()

    print(f"Collecting February 2026 sessions …")
    all_feb = collect_feb_sessions()
    sample  = stratified_sample(all_feb, args.pct)

    print(f"  Total Feb sessions : {len(all_feb):,}")
    print(f"  Sample ({args.pct:.0f}%)     : {len(sample):,}  (every {max(1,int(100/args.pct))}th file)")

    if args.dry_run:
        for p in sample[:10]:
            print(f"  {datetime.fromtimestamp(os.path.getmtime(p)).strftime('%Y-%m-%d')}  {p.name}")
        if len(sample) > 10:
            print(f"  … and {len(sample)-10} more")
        return

    batch_id = datetime.utcnow().strftime("feb-sample-%Y%m%dT%H%M%SZ")
    print(f"  Batch ID           : {batch_id}")
    print(f"  Workers            : {args.workers}")
    print()

    done = failed = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(process_session, p, batch_id): p for p in sample}
        for i, fut in enumerate(as_completed(futures), 1):
            p = futures[fut]
            try:
                result = fut.result()
                if result:
                    done += 1
                    if i % 50 == 0 or i == 1:
                        print(f"  [{i}/{len(sample)}] +{result} facts  {p.name[:36]}")
                else:
                    done += 1
            except Exception as exc:
                failed += 1
                print(f"  [{i}/{len(sample)}] ERROR {p.name[:36]}: {exc}", file=sys.stderr)

    print()
    print(f"Done. {done} sessions processed, {failed} failed.")
    print(f"Next: run the 15Q recall test:")
    print(f"  python3.13 scripts/mem0_shared_client.py search '<query>'")


if __name__ == "__main__":
    main()
