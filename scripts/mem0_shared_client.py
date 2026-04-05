#!/usr/bin/env python3
"""Shared mem0 client — openclaw, Codex, and Claude all use the same qdrant store.

Reads the openclaw-mem0 plugin config from ~/.openclaw/openclaw.json so
there is exactly one source of truth for the connection parameters.

Usage from any agent or script:
    from scripts.mem0_shared_client import get_memory, search_memory, add_memory

    # Search for relevant facts before answering
    results = search_memory("ORCH-e2e-029c50 branch")
    for r in results:
        print(r["memory"], r["score"])

    # Add a new fact after learning it
    add_memory("ORCH-e2e-abc123 was committed to ai-orch-99999 on branch feat/abc")

CLI:
    python scripts/mem0_shared_client.py search "ORCH-e2e-029c50 branch"
    python scripts/mem0_shared_client.py add "ORCH-xyz committed to ai-orch-12345"
    python scripts/mem0_shared_client.py stats
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any


class EmbedderProvider(StrEnum):
    """Supported embedder providers."""

    OLLAMA = "ollama"
    OPENAI = "openai"


@dataclass
class EmbedderConfig:
    """Type-safe embedder configuration."""

    provider: EmbedderProvider
    config: dict


_MEM0_CONFIG_CACHE: dict | None = None


def _load_openclaw_mem0_config() -> dict:
    global _MEM0_CONFIG_CACHE
    if _MEM0_CONFIG_CACHE is not None:
        return _MEM0_CONFIG_CACHE

    cfg_path = Path.home() / ".openclaw" / "openclaw.json"
    cfg = json.loads(cfg_path.read_text())
    oss = cfg["plugins"]["entries"]["openclaw-mem0"]["config"]["oss"]

    # Normalize camelCase keys to snake_case and expand env vars
    _CAMEL_TO_SNAKE = {"apiKey": "api_key", "modelName": "model_name"}

    def expand(obj: Any) -> Any:
        if isinstance(obj, str):
            return os.path.expandvars(obj)
        if isinstance(obj, dict):
            return {_CAMEL_TO_SNAKE.get(k, k): expand(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [expand(v) for v in obj]
        return obj

    oss = expand(oss)
    top = cfg["plugins"]["entries"]["openclaw-mem0"]["config"]

    _MEM0_CONFIG_CACHE = {
        "embedder": oss["embedder"],
        "llm": oss["llm"],
        "vector_store": {
            "provider": oss["vectorStore"]["provider"],
            "config": {
                "host": oss["vectorStore"]["config"]["host"],
                "port": oss["vectorStore"]["config"]["port"],
                "collection_name": oss["vectorStore"]["config"]["collectionName"],
                "embedding_model_dims": oss["vectorStore"]["config"]["embeddingModelDims"],
            },
        },
        "history_db_path": oss["historyDbPath"],
        # openclaw plugin settings
        "_top_k": top.get("topK", 8),
        "_threshold": top.get("searchThreshold", 0.3),
        "_user_id": top.get("userId", None),  # None = no default, must be provided
    }
    return _MEM0_CONFIG_CACHE


def get_memory() -> Any:
    """Return a configured mem0 Memory instance pointing at the shared qdrant store."""
    from mem0 import Memory  # type: ignore
    cfg = _load_openclaw_mem0_config()
    mem0_cfg = {k: v for k, v in cfg.items() if not k.startswith("_")}
    return Memory.from_config(mem0_cfg)


def search_memory(
    query: str,
    top_k: int | None = None,
    user_id: str | None = None,
    agent_id: str | None = None,
    filter: dict | None = None,
    include_legacy: bool = False,
) -> list[dict]:
    """Search the shared memory store.

    ORCH-sre fix: By default excludes legacy data using must_not filter.
    The filter param allows custom qdrant filters to be passed through.

    Args:
        query: Search query string
        top_k: Number of results to return (default from config)
        user_id: User ID for the search (required, no default)
        agent_id: Optional agent ID to filter by (stored in metadata)
        filter: Optional qdrant filter dict (e.g., {"must": [{"key": "agent_id", "match": {"value": "claw-main"}}]})
        include_legacy: If True, include legacy (is_legacy=True) data in results

    Returns:
        List of {memory, score, id} dicts (filtered by threshold if set)
    """
    cfg = _load_openclaw_mem0_config()
    m = get_memory()
    if user_id is None:
        user_id = cfg["_user_id"]
    if user_id is None:
        raise ValueError("user_id is required - no default configured")
    k = cfg["_top_k"] if top_k is None else top_k
    threshold = cfg.get("_threshold", 0.0)

    # ORCH-sre fix: Build filter to exclude legacy data by default
    # Use must_not+match+True (excludes tagged, includes untagged)
    # NOT must+match+False (would exclude untagged new points too)
    search_filter = filter
    if not include_legacy:
        legacy_filter = {"must_not": [{"key": "is_legacy", "match": {"value": True}}]}
        if search_filter:
            # Merge: preserve both must and must_not separately
            search_filter = {
                "must": list(search_filter.get("must", [])),
                "must_not": [legacy_filter["must_not"][0], *search_filter.get("must_not", [])],
            }
        else:
            search_filter = legacy_filter

    # Add agent_id filter if specified
    if agent_id:
        agent_filter = {"must": [{"key": "agent_id", "match": {"value": agent_id}}]}
        if search_filter:
            search_filter = {
                "must": [
                    *search_filter.get("must", []),
                    *agent_filter.get("must", []),
                ]
            }
        else:
            search_filter = agent_filter

    # mem0.search() can't handle native qdrant filter dicts (pydantic mismatch).
    # Always use raw qdrant client when we have a filter; plain search otherwise.
    if search_filter:
        results = _search_via_raw_client(query, user_id, k, search_filter)
    else:
        try:
            results = m.search(query, user_id=user_id, limit=k)
        except Exception:
            results = _search_via_raw_client(query, user_id, k, None)

    # mem0 returns {"results": [...]} or list depending on version
    if isinstance(results, dict):
        results = results.get("results", [])

    # Apply threshold filter
    if threshold > 0:
        results = [r for r in results if (r.get("score") or 0) >= threshold]

    return results


def _get_embedding(query: str, embedder_cfg: EmbedderConfig) -> list[float]:
    """Return an embedding vector for query using the configured provider.

    Supports:
    - EmbedderProvider.OLLAMA  → calls local Ollama API (no network cost)
    - EmbedderProvider.OPENAI  → calls OpenAI embeddings API

    Raises:
        ValueError: If provider is unknown or response is malformed.
    """
    config = embedder_cfg.config

    if embedder_cfg.provider == EmbedderProvider.OLLAMA:
        import requests
        # Support multiple config key variations for base URL
        base_url = (
            config.get("ollama_base_url")
            or config.get("base_url")
            or config.get("baseURL")
            or "http://localhost:11434"
        )
        model = config.get("model", "nomic-embed-text")
        # Use current /api/embed endpoint (not deprecated /api/embeddings)
        resp = requests.post(
            f"{base_url}/api/embed",
            json={"model": model, "input": query},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if "embeddings" not in data or not data["embeddings"]:
            raise ValueError(f"Ollama response missing 'embeddings' key. Response: {data!r}")
        return data["embeddings"][0]

    if embedder_cfg.provider == EmbedderProvider.OPENAI:
        import os
        from openai import OpenAI
        api_key = config.get("api_key", "") or os.environ.get("OPENAI_API_KEY", "")
        model = config.get("model", "text-embedding-3-small")
        client = OpenAI(api_key=api_key)
        response = client.embeddings.create(model=model, input=query)
        return response.data[0].embedding

    raise ValueError(f"Unknown embedder provider: {embedder_cfg.provider}. Supported: {list(EmbedderProvider)}")


def _search_via_raw_client(query: str, user_id: str, limit: int, filter: dict | None) -> list[dict]:
    """Fallback search via raw qdrant_client when mem0 doesn't support filter."""
    from qdrant_client import QdrantClient
    from qdrant_client.models import Filter, FieldCondition, MatchValue

    cfg = _load_openclaw_mem0_config()
    vs = cfg["vector_store"]["config"]

    # Convert raw dict embedder config to type-safe EmbedderConfig
    raw_embedder = cfg.get("embedder", {})
    embedder_cfg = EmbedderConfig(
        provider=EmbedderProvider(raw_embedder.get("provider", "openai")),
        config=raw_embedder.get("config", {}),
    )
    vector = _get_embedding(query, embedder_cfg)

    # Search qdrant directly
    qdrant = QdrantClient(host=vs["host"], port=vs["port"])

    # Build qdrant filter: exclude is_legacy + scope to user_id
    must = [FieldCondition(key="user_id", match=MatchValue(value=user_id))]
    must_not = []
    if filter:
        for cond in filter.get("must_not", []):
            if cond.get("key") == "is_legacy":
                must_not.append(FieldCondition(key="is_legacy", match=MatchValue(value=True)))
        for cond in filter.get("must", []):
            k, v = cond.get("key"), (cond.get("match") or {}).get("value")
            if k and v is not None:
                must.append(FieldCondition(key=k, match=MatchValue(value=v)))

    qdrant_filter = Filter(must=must, must_not=must_not if must_not else None)

    hits = qdrant.query_points(
        collection_name=vs["collection_name"],
        query=vector,
        limit=limit,
        query_filter=qdrant_filter,
        with_payload=True,
    ).points

    return [
        {"memory": r.payload.get("data", r.payload.get("memory", "")), "score": r.score, "id": str(r.id)}
        for r in hits
    ]


class MemoryWriteError(Exception):
    """Raised when mem0 infer=True returns no results (LLM refused or deduplicated)."""


def add_memory(
    text: str,
    user_id: str | None = None,
    agent_id: str | None = None,
    metadata: dict | None = None,
    infer: bool = True,
) -> dict:
    """Add a fact to the shared memory store.

    Args:
        text: The memory/fact to store
        user_id: User ID (required, no default)
        agent_id: Optional agent ID to associate with this memory
        metadata: Optional additional metadata dict
        infer: If True (default), mem0 runs the text through gpt-4o-mini to extract
            facts before storing — the LLM may silently drop inputs it considers
            unworthy or duplicate. If False, stores the text verbatim without LLM
            filtering; use this for tests or when guaranteed write is required.

    Raises:
        MemoryWriteError: When infer=True and the LLM returns no results (silent drop).
            Catch this to accept deduplication, or retry with infer=False.
        ValueError: When user_id is not provided and no default is configured.
    """
    cfg = _load_openclaw_mem0_config()
    m = get_memory()
    if user_id is None:
        user_id = cfg["_user_id"]
    if user_id is None:
        raise ValueError("user_id is required - no default configured")

    # Build metadata with agent_id if provided
    meta = dict(metadata) if metadata else {}
    if "is_legacy" not in meta:
        meta["is_legacy"] = False
    if agent_id:
        meta["agent_id"] = agent_id

    result = m.add(text, user_id=user_id, metadata=meta, infer=infer)

    # Raise if infer=True returned nothing — makes the silent LLM drop visible.
    # infer=False always returns results, so this check only fires for the infer path.
    if infer:
        results_list = result.get("results", []) if isinstance(result, dict) else result
        if not results_list:
            raise MemoryWriteError(
                f"mem0 infer=True returned no results for input (LLM refused or deduplicated). "
                f"Use infer=False for guaranteed storage, or catch MemoryWriteError to accept dedup. "
                f"Input (first 120 chars): {text[:120]!r}"
            )

    # COMPAT FIX: Node.js openclaw-mem0 extension filters Qdrant by camelCase "userId",
    # but Python mem0 library stores snake_case "user_id". Add userId to make points
    # visible to autoRecall. Uses cached client for performance.
    try:
        results_list = result.get("results", []) if isinstance(result, dict) else (result or [])
        if results_list:
            point_ids = [r["id"] for r in results_list if r.get("id")]
            if point_ids:
                # Use cached client to avoid connection overhead on every call
                if not hasattr(add_memory, "_qdrant_client"):
                    from qdrant_client import QdrantClient
                    cfg = _load_openclaw_mem0_config()
                    vs = cfg["vector_store"]["config"]
                    add_memory._qdrant_client = QdrantClient(host=vs["host"], port=vs["port"])
                    add_memory._collection_name = vs["collection_name"]
                add_memory._qdrant_client.set_payload(
                    collection_name=add_memory._collection_name,
                    payload={"userId": user_id},
                    points=point_ids,
                )
    except Exception as e:
        import sys
        print(f"WARNING: userId compat write failed: {e}", file=sys.stderr)

    return result


def get_stats() -> dict:
    """Return basic stats about the shared memory store."""
    try:
        from qdrant_client import QdrantClient  # type: ignore
        cfg = _load_openclaw_mem0_config()
        vs = cfg["vector_store"]["config"]
        client = QdrantClient(host=vs["host"], port=vs["port"])
        info = client.get_collection(vs["collection_name"])
        return {
            "points_count": info.points_count,
            "collection": vs["collection_name"],
            "host": f"{vs['host']}:{vs['port']}",
        }
    except Exception as e:
        return {"error": str(e)}


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Shared mem0 client CLI")
    sub = parser.add_subparsers(dest="cmd")

    s = sub.add_parser("search", help="Search memory")
    s.add_argument("query", help="Search query")
    s.add_argument("--top-k", type=int, default=None)
    s.add_argument("--user-id", default=None)

    a = sub.add_parser("add", help="Add a memory")
    a.add_argument("text", help="Fact text to store")
    a.add_argument("--user-id", default=None)
    a.add_argument("--no-infer", action="store_true", help="Bypass LLM extraction; store text verbatim (guaranteed write)")

    sub.add_parser("stats", help="Show store stats")

    args = parser.parse_args()

    if args.cmd == "search":
        results = search_memory(args.query, top_k=args.top_k, user_id=args.user_id)
        for r in results:
            score = r.get("score")
            mem = r.get("memory", r.get("text", r))
            score_str = f"[{score:.3f}]" if isinstance(score, (int, float)) else f"[{score}]"
            print(f"{score_str} {mem}")
    elif args.cmd == "add":
        result = add_memory(args.text, user_id=args.user_id, infer=not args.no_infer)
        print(json.dumps(result, indent=2))
    elif args.cmd == "stats":
        print(json.dumps(get_stats(), indent=2))
    else:
        parser.print_help()
