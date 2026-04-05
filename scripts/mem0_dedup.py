#!/usr/bin/env python3
"""
mem0_dedup.py — Remove near-duplicate memories from the shared mem0 store.

Clusters memories by key-token overlap (threshold 0.88), keeps the longest
(most informative) in each cluster, and deletes the rest.

Usage:
    python3 ~/.openclaw/scripts/mem0_dedup.py [--dry-run] [--user-id jleechan]
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from mem0_shared_client import get_memory  # type: ignore

STOPWORDS = {
    "the","a","an","is","was","are","were","be","been","being","have","has","had",
    "do","does","did","will","would","could","should","may","might","shall","can",
    "need","dare","ought","used","to","of","in","on","at","by","for","with","about",
    "against","between","into","through","during","from","up","down","out","off",
    "over","under","again","then","once","and","but","or","not","no","so","if","as",
    "it","its","this","that","these","those","he","she","they","we",
    "user","jeffrey","lee","chan","jleechan",
}

THRESHOLD = 0.88
MIN_LEN = 15
BATCH = 2000


def key_tokens(text: str) -> set[str]:
    return {
        t for t in re.sub(r"[^a-z0-9/._-]", " ", text.lower()).split()
        if t not in STOPWORDS and len(t) > 2
    }


def similarity(a: str, b: str) -> float:
    ta, tb = key_tokens(a), key_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def find_duplicates(items: list[dict]) -> list[str]:
    """Return IDs to delete (keeps longest in each cluster)."""
    assigned: set[int] = set()
    clusters: list[list[int]] = []

    for i, item in enumerate(items):
        if i in assigned:
            continue
        cluster = [i]
        ti = item.get("memory", "")
        if len(ti) >= MIN_LEN:
            for j in range(i + 1, len(items)):
                if j in assigned:
                    continue
                tj = items[j].get("memory", "")
                if len(tj) >= MIN_LEN and similarity(ti, tj) >= THRESHOLD:
                    cluster.append(j)
                    assigned.add(j)
        assigned.add(i)
        clusters.append(cluster)

    to_delete = []
    for cluster in clusters:
        if len(cluster) == 1:
            continue
        best = max(cluster, key=lambda idx: len(items[idx].get("memory", "")))
        for idx in cluster:
            if idx != best:
                to_delete.append(items[idx]["id"])
    return to_delete


def run(user_id: str, dry_run: bool) -> None:
    m = get_memory()
    pass_num = 1
    total_deleted = 0

    while True:
        results = m.get_all(user_id=user_id, limit=BATCH)
        items = results.get("results", results) if isinstance(results, dict) else results
        print(f"Pass {pass_num}: fetched {len(items)} memories")

        to_delete = find_duplicates(items)
        if not to_delete:
            print("No duplicates found — done.")
            break

        print(f"  Found {len(to_delete)} duplicates", "(dry-run)" if dry_run else "")
        if not dry_run:
            deleted = errors = 0
            for mid in to_delete:
                try:
                    m.delete(memory_id=mid)
                    deleted += 1
                except Exception as e:
                    errors += 1
                    if errors <= 3:
                        print(f"  error {mid[:8]}: {e}")
            total_deleted += deleted
            print(f"  Deleted {deleted}, errors {errors}")

        pass_num += 1
        if dry_run or len(items) < BATCH:
            break

    if not dry_run:
        print(f"\nTotal deleted: {total_deleted}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Deduplicate mem0 memories")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be deleted without deleting")
    parser.add_argument("--user-id", default="jleechan", help="mem0 user ID (default: jleechan)")
    args = parser.parse_args()
    run(user_id=args.user_id, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
