#!/usr/bin/env python3
"""mem0_50q_aggregate.py — Merge batch QA results into a canonical score.

Usage:
  python3 scripts/mem0_50q_aggregate.py [--root /tmp/openclaw-mem0-fastpath] [--stamp STAMP_PREFIX]

Finds all qa-batch.json files from runs matching the stamp prefix (or all runs
if no stamp is given), merges them in question order, and writes:
  - qa-50.json    (merged answers)
  - score.json    (aggregate pass/fail)
  - failures.json (missed questions)

Agent handoff protocol:
  Each batch run writes its results to /tmp/openclaw-mem0-fastpath/<RUN_TAG>/qa-batch.json.
  The STAMP_PREFIX groups runs from the same overall batch job.
  After all N agents complete their batch, run this script to aggregate.
  The canonical score is written to the latest-50q symlink target.

Example multi-agent workflow:
  STAMP=$(date -u +%Y%m%dT%H%M%SZ)
  export OPENCLAW_50Q_STAMP="$STAMP"
  for i in 1 2 3 4 5; do
    OPENCLAW_50Q_AGENT=memqa0$i mem0_50q_run.sh --batch-size 10 --batch $i/5 \
      --skip-reindex &
  done
  wait
  python3 mem0_50q_aggregate.py --stamp "$STAMP"
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description='Aggregate mem0 50Q batch results.')
    parser.add_argument('--root', default='/tmp/openclaw-mem0-fastpath',
                        help='Root directory for run artifacts')
    parser.add_argument('--stamp', default='',
                        help='Timestamp prefix to filter runs (e.g. 20260314T). '
                             'Omit to aggregate all qa-batch.json files found.')
    parser.add_argument('--out-dir', default='',
                        help='Output directory. Defaults to latest-50q symlink target.')
    args = parser.parse_args()

    root = pathlib.Path(args.root)
    if not root.exists():
        print(f'error: root directory not found: {root}', file=sys.stderr)
        sys.exit(1)

    # Find all batch result files matching the stamp
    pattern = f'{args.stamp}*' if args.stamp else '*'
    batch_files = sorted(root.glob(f'{pattern}/qa-batch.json'))

    if not batch_files:
        print(f'error: no qa-batch.json files found under {root}/{pattern}', file=sys.stderr)
        sys.exit(1)

    print(f'Found {len(batch_files)} batch file(s):')
    for bf in batch_files:
        print(f'  {bf}')

    # Load and merge all rows, sorting by global question number
    all_rows: list[dict] = []
    seen_ns: set[int] = set()
    for bf in batch_files:
        rows = json.loads(bf.read_text())
        for row in rows:
            n = row['n']
            if n in seen_ns:
                raise ValueError(f'duplicate question n={n} from {bf} — remove stale batch files')
            seen_ns.add(n)
            all_rows.append(row)

    all_rows.sort(key=lambda r: r['n'])

    # Filter to canonical range 1-50 before computing score
    expected_ns = set(range(1, 51))
    extra = {r['n'] for r in all_rows} - expected_ns
    if extra:
        print(f'warning: dropping out-of-range question numbers: {sorted(extra)}')
        all_rows = [r for r in all_rows if r['n'] in expected_ns]

    actual_ns = {r['n'] for r in all_rows}
    missing = expected_ns - actual_ns
    if missing:
        print(f'warning: missing questions: {sorted(missing)}')

    passed = sum(1 for r in all_rows if r.get('passed'))
    total = 50  # canonical total is always 50
    pass_rate = passed / total if total else 0.0

    score = {
        'passed': passed,
        'total': total,
        'pass_rate': pass_rate,
        'questions_answered': len(all_rows),
        'questions_missing': sorted(missing),
        'batch_files': [str(bf) for bf in batch_files],
    }

    # Determine output directory
    if args.out_dir:
        out_dir = pathlib.Path(args.out_dir)
    else:
        latest = root / 'latest-50q'
        if latest.is_symlink():
            out_dir = latest.resolve()
        else:
            out_dir = latest
    out_dir.mkdir(parents=True, exist_ok=True)

    (out_dir / 'qa-50.json').write_text(json.dumps(all_rows, indent=2))
    (out_dir / 'score.json').write_text(json.dumps(score, indent=2))
    (out_dir / 'failures.json').write_text(
        json.dumps([r for r in all_rows if not r.get('passed')], indent=2)
    )

    print(f'\nAggregate score: {passed}/{total} ({pass_rate:.0%})')
    print(f'Written to: {out_dir}')
    if missing:
        print(f'WARNING: {len(missing)} questions not answered — score may be deflated')


if __name__ == '__main__':
    main()
