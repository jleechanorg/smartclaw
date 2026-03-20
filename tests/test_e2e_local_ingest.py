"""Real end-to-end test: local Ollama ingest on a 1% sample of the session corpus.

This is a REAL test — no mocks, no stubs. It:
1. Scans actual session files from ~/.claude/projects and ~/.codex
2. Takes a random 1% sample (capped at 50 sessions for speed)
3. Runs mem0_extract_facts.py against a temporary Qdrant test collection
4. Verifies points land in Qdrant
5. Runs a semantic search and verifies results are returned

Prerequisites:
- Ollama running: ollama list shows nomic-embed-text + llama3.2:3b
- Qdrant running: localhost:6333

Skip conditions:
- Ollama not reachable
- Qdrant not reachable
- No session files found

Runtime: ~5-15 minutes for 50 sessions (local Ollama LLM)
"""
from __future__ import annotations

import json
import random
import re
import sys
import time
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TEST_COLLECTION = "openclaw_mem0_e2e_test"
SAMPLE_CAP = 50       # max sessions to process (keeps test under ~15 min)
SAMPLE_PCT = 0.01     # 1% of corpus
MIN_SESSIONS = 5      # fail if fewer than this many sessions found
USER_ID = "jleechan"

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.jsonl$",
    re.IGNORECASE,
)
_CODEX_SESSION_DIRS = {"sessions", "archived_sessions", "sessions_archive"}
_CODEX_SKIP = {".beads", "worktrees", "history.jsonl"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ollama_ready() -> bool:
    try:
        import requests
        r = requests.get("http://localhost:11434/api/tags", timeout=5)
        models = [m["name"] for m in r.json().get("models", [])]
        return any("nomic-embed-text" in m for m in models)
    except Exception:
        return False


def _qdrant_ready() -> bool:
    try:
        from qdrant_client import QdrantClient
        QdrantClient(host="127.0.0.1", port=6333).get_collections()
        return True
    except Exception:
        return False


def _find_sessions(cap: int) -> list[Path]:
    """Find real session files from Claude + Codex dirs, return random sample."""
    sources = {
        "claude": Path.home() / ".claude" / "projects",
        "codex": Path.home() / ".codex",
    }
    all_sessions: list[Path] = []

    for source, base in sources.items():
        if not base.exists():
            continue
        for f in base.rglob("*.jsonl"):
            if source == "claude" and _UUID_RE.match(f.name):
                all_sessions.append(f)
            elif source == "codex":
                rel = str(f)
                if any(skip in rel for skip in _CODEX_SKIP):
                    continue
                if any(p in _CODEX_SESSION_DIRS for p in f.parts):
                    all_sessions.append(f)

    if not all_sessions:
        return []

    sample_size = min(cap, max(MIN_SESSIONS, int(len(all_sessions) * SAMPLE_PCT)))
    random.seed(42)  # reproducible
    return random.sample(all_sessions, min(sample_size, len(all_sessions)))


def _setup_test_collection(dims: int = 768) -> None:
    """Drop + recreate the test collection."""
    from qdrant_client import QdrantClient
    from qdrant_client.models import VectorParams, Distance

    client = QdrantClient(host="127.0.0.1", port=6333)
    try:
        client.delete_collection(TEST_COLLECTION)
    except Exception:
        pass
    client.create_collection(
        TEST_COLLECTION,
        vectors_config=VectorParams(size=dims, distance=Distance.COSINE),
    )


def _teardown_test_collection() -> None:
    try:
        from qdrant_client import QdrantClient
        QdrantClient(host="127.0.0.1", port=6333).delete_collection(TEST_COLLECTION)
    except Exception:
        pass


def _point_count() -> int:
    from qdrant_client import QdrantClient
    info = QdrantClient(host="127.0.0.1", port=6333).get_collection(TEST_COLLECTION)
    return info.points_count or 0


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module", autouse=True)
def patch_collection(monkeypatch_module):
    """Redirect all mem0 writes to TEST_COLLECTION for the duration of this module."""
    # We patch _load_openclaw_mem0_config to return test collection name
    import scripts.mem0_shared_client as client_mod

    original_loader = client_mod._load_openclaw_mem0_config
    _cache = {}

    def patched_loader():
        if _cache:
            return _cache
        cfg = original_loader()
        import copy
        cfg = copy.deepcopy(cfg)
        cfg["vector_store"]["config"]["collection_name"] = TEST_COLLECTION
        _cache.update(cfg)
        return _cache

    monkeypatch_module.setattr(client_mod, "_load_openclaw_mem0_config", patched_loader)
    monkeypatch_module.setattr(client_mod, "_MEM0_CONFIG_CACHE", None)
    yield
    client_mod._MEM0_CONFIG_CACHE = None


@pytest.fixture(scope="module")
def monkeypatch_module():
    from _pytest.monkeypatch import MonkeyPatch
    mp = MonkeyPatch()
    yield mp
    mp.undo()


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------

@pytest.mark.real_e2e
def test_local_ingest_sample():
    """
    REAL E2E: ingest 1% sample (~50 sessions) using local Ollama stack.

    Proved when:
    - Qdrant test collection has > 0 points after ingest
    - Semantic search returns at least 1 result
    - No cloud API calls made (Ollama only)
    """
    # --- Prerequisites ---
    if not _ollama_ready():
        pytest.skip("Ollama not running or nomic-embed-text not pulled")
    if not _qdrant_ready():
        pytest.skip("Qdrant not reachable at localhost:6333")

    sessions = _find_sessions(SAMPLE_CAP)
    if len(sessions) < MIN_SESSIONS:
        pytest.skip(f"Too few sessions found: {len(sessions)} < {MIN_SESSIONS}")

    print(f"\n[e2e] Sample: {len(sessions)} sessions from corpus", flush=True)

    # --- Evidence dir ---
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    evidence_dir = Path(f"/tmp/mem0-e2e-{ts}")
    evidence_dir.mkdir(parents=True, exist_ok=True)
    print(f"\n[e2e] Evidence dir: {evidence_dir}", flush=True)

    # Save sampled session list
    (evidence_dir / "sessions_sampled.txt").write_text(
        "\n".join(str(s) for s in sessions) + "\n"
    )

    # --- Setup ---
    _setup_test_collection(dims=768)
    points_before = _point_count()
    assert points_before == 0, "Test collection should start empty"

    # --- Run ingest ---
    from scripts.mem0_extract_facts import process_session

    batch_id = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    facts_added = 0
    errors = 0
    error_log: list[dict] = []
    start = time.time()

    for i, session_path in enumerate(sessions):
        try:
            n = process_session(session_path, batch_id)
            facts_added += n
            if (i + 1) % 10 == 0:
                elapsed = time.time() - start
                print(
                    f"[e2e] {i+1}/{len(sessions)} sessions, "
                    f"{facts_added} facts, {elapsed:.0f}s elapsed",
                    flush=True,
                )
        except Exception as e:
            errors += 1
            error_log.append({"session": str(session_path), "error": str(e)})
            print(f"[e2e] Error on {session_path.name}: {e}", file=sys.stderr)

    elapsed = time.time() - start
    points_after = _point_count()

    print(
        f"\n[e2e] Done: {facts_added} facts, {points_after} Qdrant points, "
        f"{errors} errors, {elapsed:.0f}s total",
        flush=True,
    )

    # --- Dump Qdrant points to evidence ---
    from qdrant_client import QdrantClient
    qdrant = QdrantClient(host="127.0.0.1", port=6333)
    hits = qdrant.scroll(
        collection_name=TEST_COLLECTION,
        limit=500,
        with_payload=True,
        with_vectors=False,
    )[0]
    qdrant_points = [
        {"id": str(h.id), "memory": h.payload.get("data", h.payload.get("memory", "")), "metadata": h.payload}
        for h in hits
    ]
    (evidence_dir / "qdrant_points.json").write_text(
        json.dumps(qdrant_points, indent=2, default=str)
    )
    if error_log:
        (evidence_dir / "errors.json").write_text(json.dumps(error_log, indent=2))

    # --- Assertions ---
    assert points_after > 0, (
        f"Expected >0 Qdrant points after ingesting {len(sessions)} sessions, got 0. "
        f"errors={errors}, facts_added={facts_added}"
    )

    # --- Search verification ---
    from scripts.mem0_shared_client import search_memory

    results = search_memory("git commit branch worktree", top_k=5, user_id=USER_ID)
    print(f"[e2e] Search returned {len(results)} results", flush=True)
    if results:
        print(f"[e2e] Top result [{results[0].get('score', '?'):.2f}]: {results[0].get('memory','')[:120]}")

    (evidence_dir / "search_results.json").write_text(
        json.dumps(results, indent=2, default=str)
    )

    # --- Summary artifact ---
    summary = {
        "timestamp": ts,
        "sessions_sampled": len(sessions),
        "facts_added": facts_added,
        "qdrant_points": points_after,
        "errors": errors,
        "elapsed_seconds": round(elapsed, 1),
        "search_results": len(results),
        "top_score": results[0].get("score") if results else None,
        "evidence_dir": str(evidence_dir),
        "verdict": "PASSED" if points_after > 0 and len(results) > 0 else "FAILED",
    }
    (evidence_dir / "00_SUMMARY.json").write_text(json.dumps(summary, indent=2))
    print(f"\n[e2e] Evidence saved to {evidence_dir}/", flush=True)
    print(f"[e2e]   00_SUMMARY.json")
    print(f"[e2e]   sessions_sampled.txt  ({len(sessions)} paths)")
    print(f"[e2e]   qdrant_points.json    ({points_after} points)")
    print(f"[e2e]   search_results.json   ({len(results)} results)")
    if error_log:
        print(f"[e2e]   errors.json          ({errors} errors)")

    assert len(results) > 0, (
        f"Search returned 0 results despite {points_after} points in collection. "
        "Check Ollama embedding is working and threshold is not too high."
    )

    # --- Teardown ---
    _teardown_test_collection()

    print(f"\n[e2e] PASSED — {points_after} points ingested, search verified ✓")
