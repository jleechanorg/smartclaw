#!/usr/bin/env python3
"""mem0 extraction: distill session facts into qdrant via mem0.

Two modes:
    --since <duration>   Scan all sources for sessions modified in last N minutes
    --session <path>     Extract from a single session file (used by supervisor hook)

Options:
    --project <hash>     Filter Claude sessions to specific project (use with --since)
    --workers <n>        Number of parallel workers for batch mode (default: 4)

ORCH-6uz fix: Uses fcntl.flock for atomic read-modify-write on extraction-state.json
ORCH-2f9 fix: All m.add() calls include batch_id in metadata for rollback capability
ORCH-scan fix: Per-source session detection — Claude UUID files, all Codex session dirs
ORCH-lpcn fix: Added --project filter for targeted Claude project scanning

Usage:
    python3 scripts/mem0_extract_facts.py --since 65
    python3 scripts/mem0_extract_facts.py --since 999999 --workers 4  # full corpus rescan
    python3 scripts/mem0_extract_facts.py --since 65 --project=-Users-jleechan--openclaw
    python3 scripts/mem0_extract_facts.py --session ~/.openclaw/agents/main/sessions/abc123.jsonl
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any


class AgentId(StrEnum):
    """Agent identifiers for multi-tenant memory partitioning."""
    CLAW_MAIN = "claw-main"
    CLAUDE = "claude"
    CODEX = "codex"


# State file location
STATE_DIR = Path.home() / ".openclaw" / "memory"
STATE_FILE = STATE_DIR / "extraction-state.json"
STATE_LOCK_FILE = STATE_DIR / "extraction-state.lock"

# Source directories
SOURCES = {
    AgentId.CLAW_MAIN: Path.home() / ".openclaw" / "agents",
    AgentId.CLAUDE: Path.home() / ".claude" / "projects",
    AgentId.CODEX: Path.home() / ".codex",
}

# Claude stores sessions as UUID-named JSONL directly in project dirs (no sessions/ subdir)
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.jsonl$",
    re.IGNORECASE,
)

# Codex session dirs (relative to ~/.codex)
_CODEX_SESSION_DIRS = {"sessions", "archived_sessions", "sessions_archive"}

# Codex paths to skip even if under a session dir
_CODEX_SKIP_PATTERNS = {".beads", "worktrees", "history.jsonl"}


def _is_session_file(path: Path, agent_id: AgentId) -> bool:
    """Return True if path is a real session JSONL for the given agent."""
    if agent_id == AgentId.CLAUDE:
        # Claude: UUID-named file directly inside a project dir (2 levels deep)
        # ~/.claude/projects/<project-hash>/<uuid>.jsonl
        return _UUID_RE.match(path.name) is not None

    if agent_id == AgentId.CODEX:
        # Codex: file under sessions/, archived_sessions/, or sessions_archive/
        # but not under worktrees/ or .beads/
        rel = str(path)
        if any(skip in rel for skip in _CODEX_SKIP_PATTERNS):
            return False
        parts = path.parts
        # Find if any ancestor dir is a known session dir
        return any(p in _CODEX_SESSION_DIRS for p in parts)

    if agent_id == AgentId.CLAW_MAIN:
        # OpenClaw: ~/.openclaw/agents/<agent>/sessions/<file>.jsonl
        return "sessions" in str(path)

    return False


def ensure_state_dir() -> None:
    """Create state directory if it doesn't exist."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def load_state() -> dict[str, Any]:
    """Load extraction state with flock lock.

    ORCH-6uz fix: Uses fcntl.flock for atomic read-modify-write.
    """
    ensure_state_dir()

    if not STATE_FILE.exists():
        return {"sessions": {}, "last_run": None, "last_success_ts": None, "facts_added": 0}

    # Acquire exclusive lock for reading
    with open(STATE_LOCK_FILE, "w") as lock_fd:
        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX)
        try:
            return json.loads(STATE_FILE.read_text())
        finally:
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)


def save_state(state: dict[str, Any]) -> None:
    """Save extraction state with flock lock.

    ORCH-6uz fix: Uses fcntl.flock + atomic temp rename for safe concurrent writes.
    """
    ensure_state_dir()

    # Write to temp file first
    temp_file = STATE_FILE.with_suffix(".tmp")
    temp_file.write_text(json.dumps(state, indent=2))

    # Acquire exclusive lock, then atomic rename
    with open(STATE_LOCK_FILE, "w") as lock_fd:
        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX)
        try:
            temp_file.replace(STATE_FILE)
        finally:
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)


def get_agent_id_from_path(session_path: Path) -> AgentId | None:
    """Determine agent_id from session file path."""
    path_str = str(session_path)

    if ".openclaw/agents/" in path_str:
        return AgentId.CLAW_MAIN
    elif ".claude/projects/" in path_str:
        return AgentId.CLAUDE
    elif ".codex/" in path_str:
        return AgentId.CODEX

    return None


def parse_session_file(session_path: Path) -> list[dict[str, Any]]:
    """Extract human/assistant turns from a session JSONL file.

    Returns list of {"role": "human"|"assistant", "content": str}
    """
    turns = []

    try:
        for line in session_path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            # OpenClaw format: type field
            if entry.get("type") in ("human", "assistant"):
                content = entry.get("content", "")
                if isinstance(content, list):
                    # Extract text from content blocks
                    content = " ".join(
                        c.get("text", "") for c in content if c.get("type") == "text"
                    )
                turns.append({"role": entry["type"], "content": content})

            # Claude/Codex format: message.role field
            msg = entry.get("message", {})
            if msg.get("role") in ("user", "assistant"):
                content = msg.get("content", "")
                if isinstance(content, list):
                    content = " ".join(
                        c.get("text", "") for c in content if c.get("type") == "text"
                    )
                turns.append({"role": msg["role"], "content": content})

    except Exception as e:
        print(f"Error parsing {session_path}: {e}", file=sys.stderr)

    return turns


def filter_turns(turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter turns: skip <3 or >200 human/assistant turns.

    For oversized sessions, truncate to last 200 turns.
    """
    if len(turns) < 3:
        return []

    if len(turns) > 200:
        return turns[-200:]

    return turns


def add_facts_to_memory(
    turns: list[dict[str, Any]],
    agent_id: AgentId,
    batch_id: str,
    session_path: Path,
) -> int:
    """Add extracted facts to mem0 with batch_id for rollback capability.

    ORCH-2f9 fix: Every m.add() includes batch_id in metadata.

    Returns number of facts added.
    """
    from scripts.mem0_shared_client import add_memory, _load_openclaw_mem0_config

    if not turns:
        return 0

    # Combine turns into a single prompt for mem0 extraction
    content = "\n".join(f"{t['role']}: {t['content'][:500]}" for t in turns[:50])

    # ORCH-2f9 fix: Include batch_id in metadata for rollback capability
    metadata = {
        "is_legacy": False,  # New data is not legacy
        "batch_id": batch_id,  # For bulk delete on rollback
        "agent_id": agent_id,
        "session_path": str(session_path),
    }

    try:
        cfg = _load_openclaw_mem0_config()
        user_id = cfg.get("_user_id") or "example-user"
        result = add_memory(
            content,
            user_id=user_id,
            metadata=metadata,
        )
        # mem0 with infer=True extracts atomic facts
        return 1  # One composite entry, mem0 will split into facts
    except Exception as e:
        print(f"Error adding facts: {e}", file=sys.stderr)
        return 0


def process_session(session_path: Path, batch_id: str) -> int:
    """Process a single session file.

    Returns number of facts added.
    """
    agent_id = get_agent_id_from_path(session_path)
    if agent_id is None:
        print(f"Unknown agent for {session_path}", file=sys.stderr)
        return 0

    turns = parse_session_file(session_path)
    filtered = filter_turns(turns)

    if not filtered:
        return 0

    return add_facts_to_memory(filtered, agent_id, batch_id, session_path)


def scan_sessions_since(minutes: int, project_filter: str | None = None) -> list[Path]:
    """Find all session files modified in the last N minutes.

    ORCH-scan fix: Uses per-source _is_session_file() to correctly identify:
    - Claude: UUID-named JSONL files directly in project dirs (no sessions/ subdir)
    - Codex: files in sessions/, archived_sessions/, sessions_archive/ (not worktrees/)
    - OpenClaw: files under agents/*/sessions/

    ORCH-lpcn fix: If project_filter is set, only scan matching Claude project.
    """
    cutoff = time.time() - (minutes * 60)
    sessions = []

    for agent_id, base_dir in SOURCES.items():
        if not base_dir.exists():
            continue

        # ORCH-lpcn: If filtering Claude sessions by project, iterate only that project
        if agent_id == AgentId.CLAUDE and project_filter:
            # project_filter is the hash name (e.g., "-Users-jleechan--openclaw")
            project_dir = base_dir / project_filter
            if project_dir.exists():
                search_dirs = [project_dir]
            else:
                print(f"Warning: project filter '{project_filter}' not found in {base_dir}", file=sys.stderr)
                continue
        else:
            # Scan all subdirs
            search_dirs = [base_dir]

        for search_dir in search_dirs:
            for jsonl_file in search_dir.rglob("*.jsonl"):
                if not _is_session_file(jsonl_file, agent_id):
                    continue

                try:
                    if jsonl_file.stat().st_mtime >= cutoff:
                        sessions.append(jsonl_file)
                except OSError:
                    continue

    return sessions


def validate_project_filter(project_filter: str | None) -> None:
    """Validate project_filter to prevent path traversal attacks.

    Raises ValueError for any value that could escape the base directory,
    including dot-segment bypass patterns (., ./, .\\).
    Empty strings are also rejected (use None to disable filtering).
    """
    if project_filter is None:
        return
    if (
        ".." in project_filter
        or "/" in project_filter
        or "\\" in project_filter
        or project_filter in (".", "")
        or project_filter.startswith("./")
        or project_filter.startswith(".\\")
    ):
        raise ValueError("project_filter must be a simple name, not a path")


def run_extraction(
    since_minutes: int | None = None,
    session_path: Path | None = None,
    max_workers: int = 4,
    project_filter: str | None = None,
) -> int:
    """Run the extraction pipeline.

    ORCH-lpcn fix: project_filter restricts Claude sessions to specific project.

    Returns total facts added.
    """
    validate_project_filter(project_filter)
    
    # ORCH-2f9 fix: Generate batch_id as ISO timestamp for this run
    batch_id = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    state = load_state()
    total_facts = 0

    if session_path:
        # Single session mode (called from supervisor hook) — no parallelism needed
        facts = process_session(session_path, batch_id)
        total_facts += facts

        mtime = int(session_path.stat().st_mtime)
        state["sessions"][str(session_path)] = {
            "mtime": mtime,
            "processed_at": int(time.time()),
        }
    elif since_minutes is not None:
        # Batch mode — filter to unprocessed sessions, then run in parallel
        if since_minutes < 0:
            raise ValueError("since_minutes must be non-negative")
        sessions = scan_sessions_since(since_minutes, project_filter)

        pending: list[tuple[Path, int]] = []
        for sess in sessions:
            sess_str = str(sess)
            try:
                mtime = int(sess.stat().st_mtime)
            except OSError:
                continue
            # ORCH-c6d fix: Track (path, mtime) pair, not just path
            if state["sessions"].get(sess_str, {}).get("mtime") == mtime:
                continue
            pending.append((sess, mtime))

        print(f"Processing {len(pending)} new/changed sessions with {max_workers} workers", flush=True)

        processed_at = int(time.time())
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_sess = {
                executor.submit(process_session, sess, batch_id): (sess, mtime)
                for sess, mtime in pending
            }
            done = 0
            for future in as_completed(future_to_sess):
                sess, mtime = future_to_sess[future]
                done += 1
                try:
                    facts = future.result()
                except Exception as e:
                    print(f"Error processing {sess}: {e}", file=sys.stderr)
                    facts = 0
                total_facts += facts
                state["sessions"][str(sess)] = {"mtime": mtime, "processed_at": processed_at}
                if done % 100 == 0 or done == len(pending):
                    print(f"  {done}/{len(pending)} sessions, {total_facts} facts so far", flush=True)
    else:
        print("Error: must specify either --since or --session", file=sys.stderr)
        return 0

    # Update state atomically
    state["last_run"] = batch_id
    state["last_success_ts"] = datetime.now(timezone.utc).isoformat()
    state["facts_added"] = state.get("facts_added", 0) + total_facts
    save_state(state)

    return total_facts


def main() -> None:
    parser = argparse.ArgumentParser(description="mem0 extraction: distill session facts")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--since",
        type=int,
        help="Scan all sources for sessions modified in last N minutes",
    )
    group.add_argument(
        "--session",
        type=Path,
        help="Extract from a single session file",
    )

    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Number of parallel workers for batch mode (default: 4)",
    )
    parser.add_argument(
        "--project",
        type=str,
        default=None,
        help="ORCH-lpcn: Filter Claude sessions to specific project hash. If hash starts with '-', use --project=VALUE syntax (e.g., --project=-Users-jleechan--openclaw)",
    )

    args = parser.parse_args()

    # Validate --project is not used with --session
    if args.session and args.project:
        parser.error("--project is not supported in single-session mode (use --since for project filtering)")

    if args.since is not None:
        facts = run_extraction(since_minutes=args.since, max_workers=args.workers, project_filter=args.project)
    else:
        facts = run_extraction(session_path=args.session)

    print(f"Extraction complete: {facts} facts added")


if __name__ == "__main__":
    main()
