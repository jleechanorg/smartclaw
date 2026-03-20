"""Tests for Ollama local embedder integration in mem0_shared_client.

Test coverage:
1. _get_embedding() dispatches to Ollama when provider=ollama
2. _get_embedding() dispatches to OpenAI when provider=openai (regression)
3. Ollama returns 768-dim vectors (nomic-embed-text)
4. _search_via_raw_client uses _get_embedding() (no hardcoded OpenAI)
5. Config round-trip: ollama config loads correctly from openclaw.json fixture
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

OLLAMA_EMBEDDER_CONFIG = {
    "embedder": {
        "provider": "ollama",
        "config": {
            "model": "nomic-embed-text",
            "ollama_base_url": "http://localhost:11434",
        },
    },
    "llm": {
        "provider": "groq",
        "config": {"api_key": "test-key", "model": "llama-3.3-70b-versatile"},
    },
    "vector_store": {
        "provider": "qdrant",
        "config": {
            "host": "127.0.0.1",
            "port": 6333,
            "collection_name": "openclaw_mem0",
            "embedding_model_dims": 768,
        },
    },
    "history_db_path": "/tmp/test-mem0-history.db",
    "_top_k": 8,
    "_threshold": 0.3,
    "_user_id": "jleechan",
}

OPENAI_EMBEDDER_CONFIG = {
    "embedder": {
        "provider": "openai",
        "config": {"api_key": "sk-test", "model": "text-embedding-3-small"},
    },
    "llm": {
        "provider": "groq",
        "config": {"api_key": "test-key", "model": "llama-3.3-70b-versatile"},
    },
    "vector_store": {
        "provider": "qdrant",
        "config": {
            "host": "127.0.0.1",
            "port": 6333,
            "collection_name": "openclaw_mem0",
            "embedding_model_dims": 1536,
        },
    },
    "history_db_path": "/tmp/test-mem0-history.db",
    "_top_k": 8,
    "_threshold": 0.0,
    "_user_id": "jleechan",
}


# ---------------------------------------------------------------------------
# Test 1: _get_embedding dispatches to Ollama when provider=ollama
# ---------------------------------------------------------------------------

def test_get_embedding_uses_ollama_when_provider_is_ollama():
    """_get_embedding() must call requests.post to Ollama API, not OpenAI."""
    from scripts.mem0_shared_client import EmbedderConfig, EmbedderProvider, _get_embedding

    fake_vector = [0.1] * 768
    mock_response = MagicMock()
    mock_response.json.return_value = {"embeddings": [fake_vector]}
    mock_response.raise_for_status = MagicMock()

    embedder_cfg = EmbedderConfig(
        provider=EmbedderProvider.OLLAMA,
        config=OLLAMA_EMBEDDER_CONFIG["embedder"]["config"],
    )
    with patch("requests.post", return_value=mock_response) as mock_post:
        result = _get_embedding("hello world", embedder_cfg)

    assert result == fake_vector
    mock_post.assert_called_once()
    call_url = mock_post.call_args[0][0]
    assert "11434" in call_url or "ollama" in call_url.lower()


# ---------------------------------------------------------------------------
# Test 2: _get_embedding dispatches to OpenAI when provider=openai (regression)
# ---------------------------------------------------------------------------

def test_get_embedding_uses_openai_when_provider_is_openai():
    """_get_embedding() must still work with OpenAI provider (no regression)."""
    from scripts.mem0_shared_client import EmbedderConfig, EmbedderProvider, _get_embedding

    fake_vector = [0.2] * 1536
    mock_embedding = MagicMock()
    mock_embedding.data = [MagicMock(embedding=fake_vector)]

    embedder_cfg = EmbedderConfig(
        provider=EmbedderProvider.OPENAI,
        config=OPENAI_EMBEDDER_CONFIG["embedder"]["config"],
    )
    with patch("openai.OpenAI") as mock_openai_cls:
        mock_client = MagicMock()
        mock_client.embeddings.create.return_value = mock_embedding
        mock_openai_cls.return_value = mock_client

        result = _get_embedding("hello world", embedder_cfg)

    assert result == fake_vector
    mock_client.embeddings.create.assert_called_once()


# ---------------------------------------------------------------------------
# Test 3: Ollama returns 768-dim vectors (live integration, skipped if no Ollama)
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_ollama_embedding_is_768_dims():
    """Ollama nomic-embed-text must return 768-dim vectors (live call)."""
    import requests
    try:
        r = requests.post(
            "http://localhost:11434/api/embed",
            json={"model": "nomic-embed-text", "input": "test sentence"},
            timeout=10,
        )
        r.raise_for_status()
    except Exception as e:
        pytest.skip(f"Ollama not reachable: {e}")

    vec = r.json()["embeddings"][0]
    assert len(vec) == 768, f"Expected 768 dims, got {len(vec)}"


# ---------------------------------------------------------------------------
# Test 4: _search_via_raw_client uses _get_embedding (no hardcoded OpenAI)
# ---------------------------------------------------------------------------

def test_search_via_raw_client_uses_get_embedding():
    """_search_via_raw_client must call _get_embedding(), not openai directly."""
    from scripts import mem0_shared_client

    fake_vector = [0.0] * 768
    fake_hit = MagicMock()
    fake_hit.payload = {"data": "test memory"}
    fake_hit.score = 0.9
    fake_hit.id = "abc123"

    mock_qdrant = MagicMock()
    mock_qdrant.query_points.return_value.points = [fake_hit]

    with patch.object(mem0_shared_client, "_get_embedding", return_value=fake_vector) as mock_embed, \
         patch("qdrant_client.QdrantClient", return_value=mock_qdrant), \
         patch.object(mem0_shared_client, "_load_openclaw_mem0_config", return_value=OLLAMA_EMBEDDER_CONFIG):

        results = mem0_shared_client._search_via_raw_client("test query", "jleechan", 5, None)

    from scripts.mem0_shared_client import EmbedderConfig, EmbedderProvider
    mock_embed.assert_called_once()
    called_query, called_cfg = mock_embed.call_args.args
    assert called_query == "test query"
    assert isinstance(called_cfg, EmbedderConfig)
    assert called_cfg.provider == EmbedderProvider.OLLAMA
    assert len(results) == 1
    assert results[0]["memory"] == "test memory"


# ---------------------------------------------------------------------------
# Test 5: Config round-trip — ollama config maps correctly
# ---------------------------------------------------------------------------

def test_ollama_config_loads_from_fixture(tmp_path, monkeypatch):
    """_load_openclaw_mem0_config() must parse ollama provider correctly."""
    from scripts import mem0_shared_client

    # Build a minimal openclaw.json with ollama embedder
    cfg = {
        "plugins": {
            "entries": {
                "openclaw-mem0": {
                    "config": {
                        "topK": 8,
                        "searchThreshold": 0.3,
                        "userId": "jleechan",
                        "oss": {
                            "embedder": {
                                "provider": "ollama",
                                "config": {
                                    "model": "nomic-embed-text",
                                    "ollama_base_url": "http://localhost:11434",
                                },
                            },
                            "llm": {
                                "provider": "groq",
                                "config": {"apiKey": "test", "model": "llama-3.3-70b-versatile"},
                            },
                            "vectorStore": {
                                "provider": "qdrant",
                                "config": {
                                    "host": "127.0.0.1",
                                    "port": 6333,
                                    "collectionName": "openclaw_mem0",
                                    "embeddingModelDims": 768,
                                },
                            },
                            "historyDbPath": str(tmp_path / "mem0-history.db"),
                        },
                    }
                }
            }
        }
    }

    openclaw_dir = tmp_path / ".openclaw"
    openclaw_dir.mkdir()
    cfg_file = openclaw_dir / "openclaw.json"
    cfg_file.write_text(json.dumps(cfg))

    # Patch home to point at tmp_path and clear cache
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    mem0_shared_client._MEM0_CONFIG_CACHE = None

    loaded = mem0_shared_client._load_openclaw_mem0_config()

    assert loaded["embedder"]["provider"] == "ollama"
    assert loaded["embedder"]["config"]["model"] == "nomic-embed-text"
    assert loaded["vector_store"]["config"]["embedding_model_dims"] == 768

    # Reset cache after test
    mem0_shared_client._MEM0_CONFIG_CACHE = None
