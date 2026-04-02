#!/usr/bin/env python3.13
"""Seed mem0/Qdrant with product knowledge from docs and session conversations.

This script implements the 2-tier knowledge seeding strategy:
  Tier 1 (PRODUCT/* prefix, infer=False): static docs, CLAUDE.md, SOUL.md, ADRs, roadmap
  Tier 2 (PRODUCT/CONVERSATION, infer=True): targeted session JSONL files filtered by repo

Usage:
    # Seed static docs only (fast, ~2 min)
    python3.13 scripts/seed_product_knowledge.py --docs-only

    # Seed sessions for one repo (slower)
    python3.13 scripts/seed_product_knowledge.py --sessions worldarchitect.ai
    python3.13 scripts/seed_product_knowledge.py --sessions worldai_claw
    python3.13 scripts/seed_product_knowledge.py --sessions smartclaw

    # Full seed (docs + all sessions)
    python3.13 scripts/seed_product_knowledge.py --all

    # Dry run to see what would be ingested
    python3.13 scripts/seed_product_knowledge.py --all --dry-run

Beads: orch-btdv (comprehensive seeding), orch-4jik (session re-classification)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.mem0_shared_client import add_memory, MemoryWriteError  # type: ignore

HOME = Path.home()

# ── Static doc sources ──────────────────────────────────────────────────────

STATIC_SOURCES: list[tuple[str, Path]] = [
    # CLAUDE.md files — dense with encoded rules, exceptions, intent
    ("PRODUCT/ARCHITECTURE", HOME / "projects/worldarchitect.ai/CLAUDE.md"),
    ("PRODUCT/ARCHITECTURE", HOME / "projects/worldai_claw/CLAUDE.md"),
    # OpenClaw harness — identity + routing rules
    ("PRODUCT/INTENT",       HOME / ".smartclaw/SOUL.md"),
    ("PRODUCT/INTENT",       HOME / ".smartclaw/HEARTBEAT.md"),
    # Note: Add project-specific CLAUDE.md via CLI arg or env var for portability
    # ("PRODUCT/INTENT", Path("${HOME}/project_smartclaw/worktree_memory_followups3/CLAUDE.md")),
    # Curated product memory files
    ("PRODUCT/ARCHITECTURE", HOME / ".claude/projects/-Users-jleechan-project-smartclaw-smartclaw/memory/project_worldarchitect_ai.md"),
    ("PRODUCT/ARCHITECTURE", HOME / ".claude/projects/-Users-jleechan-project-smartclaw-smartclaw/memory/project_worldai_claw.md"),
    ("PRODUCT/INTENT",       HOME / ".claude/projects/-Users-jleechan-project-smartclaw-smartclaw/memory/project_smartclaw_goals.md"),
]

# ADRs
ADR_DIRS: list[Path] = [
    HOME / "projects/worldarchitect.ai/docs/adr",
]

# Key roadmap docs (by name, searched in smartclaw and worldarchitect.ai roadmap dirs)
PRIORITY_ROADMAP_DOCS = [
    "ORCHESTRATION_DESIGN.md",
    "NATURAL_LANGUAGE_DISPATCH.md",
    "OUTCOME_LEDGER_DESIGN.md",
    "DURABLE_BEHAVIOR_HARDENING_PLAN.md",
    "AGENTO_PROACTIVE_RECOVERY_DESIGN.md",
    "MVP_OPENCLAW_AIORCH_MULTI_AGENT.md",
    "SHARED_MEM0_ARCHITECTURE.md",
]

ROADMAP_SEARCH_DIRS: list[Path] = [
    # Add project-specific roadmap dirs via CLI arg or env var for portability
    # HOME / "projects/worldarchitect.ai/roadmap",  # Uncomment or configure via args
]

# ── Session sources (dir-name pattern → repo) ────────────────────────────────

REPO_DIR_PATTERNS: dict[str, list[str]] = {
    "worldarchitect.ai": ["worldarchitect", "worldarchitect-ai"],
    "worldai_claw":      ["worldai-claw", "worldai_claw", "worldaiclaw"],
    "smartclaw":      ["smartclaw", "project-smartclaw", "openclaw-workspace-smartclaw"],
}

CHUNK_SIZE = 800
CHUNK_STEP = 700  # Step size: how far to advance the window each iteration (size - step = overlap)
MAX_ROADMAP_CHARS = 8000


def chunks(text: str, size: int = CHUNK_SIZE, step: int = CHUNK_STEP) -> list[str]:
    out = []
    i = 0
    while i < len(text):
        c = text[i: i + size]
        if c.strip():
            out.append(c)
        if i + size >= len(text):
            break
        i += step
    return out


def ingest_file(prefix: str, path: Path, dry_run: bool = False, max_chars: int = 0) -> int:
    if not path.exists():
        print(f"  SKIP (not found): {path}")
        return 0
    text = path.read_text(encoding="utf-8", errors="replace")
    if max_chars:
        text = text[:max_chars]
    cs = chunks(text)
    if dry_run:
        print(f"  DRY {prefix} | {path.name}: {len(cs)} chunks")
        return len(cs)
    for i, c in enumerate(cs):
        add_memory(f"{prefix} | SOURCE:{path.name} chunk {i + 1}/{len(cs)}\n\n{c}", infer=False)
    print(f"  stored {prefix} | {path.name}: {len(cs)} chunks")
    return len(cs)


def seed_static_docs(dry_run: bool = False) -> int:
    total = 0
    print("\n=== Static docs ===")

    for prefix, path in STATIC_SOURCES:
        total += ingest_file(prefix, path, dry_run=dry_run)

    print("\n=== ADRs ===")
    for adr_dir in ADR_DIRS:
        if not adr_dir.is_dir():
            continue
        for f in sorted(adr_dir.glob("*.md")):
            total += ingest_file("PRODUCT/REASONING", f, dry_run=dry_run)

    print("\n=== Priority roadmap docs ===")
    for name in PRIORITY_ROADMAP_DOCS:
        for d in ROADMAP_SEARCH_DIRS:
            f = d / name
            if f.exists():
                total += ingest_file("PRODUCT/ROADMAP", f, dry_run=dry_run, max_chars=MAX_ROADMAP_CHARS)
                break

    return total


def find_repo_sessions(repo: str) -> list[Path]:
    proj_dir = HOME / ".claude/projects"
    patterns = REPO_DIR_PATTERNS.get(repo, [])
    sessions: list[Path] = []
    if not proj_dir.is_dir():
        return sessions  # Guard: Claude projects dir may not exist
    for h in os.listdir(proj_dir):
        hn = h.lower()
        if any(pat in hn for pat in patterns):
            sessions.extend((proj_dir / h).glob("*.jsonl"))
    return sorted(sessions, key=lambda p: p.stat().st_mtime, reverse=True)


def extract_conversation_text(jsonl_path: Path, max_chars: int = 4000) -> str:
    """Extract human+assistant turns from a Claude session JSONL.

    Claude Code sessions use: {"type": "user"|"assistant", "message": {"role": ..., "content": ...}}
    """
    lines: list[str] = []
    try:
        with open(jsonl_path, encoding="utf-8", errors="replace") as f:
            for raw in f:
                try:
                    d = json.loads(raw)
                    # Claude Code format: type=user/assistant, message.role, message.content
                    if d.get("type") in ("user", "assistant"):
                        msg = d.get("message", {})
                        role = msg.get("role", d["type"])
                        content = msg.get("content", "")
                        if isinstance(content, list):
                            content = " ".join(
                                b.get("text", "") for b in content
                                if isinstance(b, dict) and b.get("type") == "text"
                            )
                        if content and str(content).strip():
                            lines.append(f"[{role}] {str(content)[:600]}")
                except Exception:
                    continue  # Skip malformed entries
    except Exception as e:
        print(f"Warning: skipping unreadable {jsonl_path}: {e}", file=sys.stderr)
    return "\n".join(lines)[:max_chars]


def seed_sessions(repo: str, limit: int = 200, dry_run: bool = False) -> int:
    sessions = find_repo_sessions(repo)
    print(f"\n=== Sessions: {repo} ({len(sessions)} found, processing up to {limit}) ===")
    sessions = sessions[:limit]

    total = 0
    failed = 0
    for i, sess in enumerate(sessions):
        text = extract_conversation_text(sess)
        if not text.strip():
            continue
        cs = chunks(text, size=1200, step=1000)
        if dry_run:
            print(f"  DRY session {i + 1}/{len(sessions)} {sess.name}: {len(cs)} chunks")
            total += len(cs)
            continue
        for j, c in enumerate(cs):
            try:
                add_memory(
                    f"PRODUCT/CONVERSATION | REPO:{repo} SESSION:{sess.name} chunk {j + 1}/{len(cs)}\n\n{c}",
                    infer=True,  # let LLM extract decisions/patterns from conversation
                )
                total += 1
            except MemoryWriteError:
                failed += 1  # Accept deduplication; track skipped chunks
        if (i + 1) % 20 == 0:
            print(f"  [{i + 1}/{len(sessions)}] {total} chunks so far")

    if failed:
        print(f"  Note: {failed} chunks skipped (deduplication or write errors)", file=sys.stderr)
    print(f"  Done: {len(sessions)} sessions, {total} total chunks")
    return total


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docs-only", action="store_true", help="Seed static docs only")
    parser.add_argument("--sessions", metavar="REPO",
                        choices=list(REPO_DIR_PATTERNS.keys()),
                        help="Seed sessions for one specific repo")
    parser.add_argument("--all", action="store_true", help="Seed docs + all sessions")
    parser.add_argument("--session-limit", type=int, default=200,
                        help="Max sessions per repo (default: 200)")
    parser.add_argument("--roadmap-dirs", metavar="DIR", nargs="*",
                        help="Additional directories to search for roadmap docs")
    parser.add_argument("--projects-dir", default=str(HOME / "projects"),
                        help="Base directory for projects (default: ~/projects)")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be ingested")
    args = parser.parse_args()

    # Extend roadmap search dirs from CLI args
    # Use `is not None` (not truthiness) so --roadmap-dirs with no args suppresses defaults
    if args.roadmap_dirs is not None:
        for d in args.roadmap_dirs:
            path = Path(d).expanduser()
            if path.is_dir():
                ROADMAP_SEARCH_DIRS.append(path)
            else:
                print(f"Warning: --roadmap-dirs entry is not a directory: {path}", file=sys.stderr)
    else:
        # Default: search in projects dir if not specified
        projects_base = Path(args.projects_dir)
        if projects_base.is_dir():
            ROADMAP_SEARCH_DIRS.append(projects_base / "worldarchitect.ai" / "roadmap")
            ROADMAP_SEARCH_DIRS.append(projects_base / "smartclaw" / "roadmap")

    total = 0
    if args.docs_only or args.all:
        total += seed_static_docs(dry_run=args.dry_run)

    # Track repos already processed via --sessions to avoid duplicate ingestion with --all
    processed_repos: set[str] = set()

    if args.sessions:
        total += seed_sessions(args.sessions, limit=args.session_limit, dry_run=args.dry_run)
        processed_repos.add(args.sessions)

    if args.all:
        for repo in REPO_DIR_PATTERNS:
            if repo in processed_repos:
                continue  # Skip repos already processed via --sessions
            total += seed_sessions(repo, limit=args.session_limit, dry_run=args.dry_run)

    if not any([args.docs_only, args.sessions, args.all]):
        parser.print_help()
        return

    print(f"\nTotal chunks ingested: {total}")


if __name__ == "__main__":
    main()
