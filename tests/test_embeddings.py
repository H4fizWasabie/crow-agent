import pytest
from unittest.mock import patch
import numpy as np
from crow_agent.embeddings import (
    semantic_search, embed, _CACHE, _MAX_CACHE_SIZE,
    _evict_lru, store_memory_embedding, precompute_items,
)

class TestEmbed:
    def test_embed_returns_array(self):
        vec = embed(["hello world"])
        assert vec is not None
        assert len(vec.shape) == 2 and vec.shape[0] == 1

    def test_embed_empty_returns_none(self):
        assert embed([]) is None

    def test_embed_api_failure_returns_none(self):
        with patch("crow_agent.embeddings.requests.post", side_effect=Exception("fail")):
            assert embed(["test"]) is None

class TestSemanticSearch:
    def test_returns_top_results(self):
        items = {"skill:a": "deploy apps", "skill:b": "bake cookies"}
        results = semantic_search("ship code", items, top_k=2)
        assert len(results) == 2

    def test_empty_items_returns_empty(self):
        assert semantic_search("q", {}) == []

    def test_api_failure_returns_empty(self):
        with patch("crow_agent.embeddings.embed", return_value=None):
            assert semantic_search("test", {"k": "v"}) == []

class TestLRUCache:
    def setup_method(self):
        _CACHE.clear()

    def test_evicts_when_full(self):
        for i in range(_MAX_CACHE_SIZE + 10):
            _CACHE[f"key_{i}"] = (float(i), np.zeros(1536))
        _evict_lru()
        assert len(_CACHE) <= _MAX_CACHE_SIZE
        assert "key_0" not in _CACHE

    def test_no_eviction_when_under_limit(self):
        for i in range(10):
            _CACHE[f"key_{i}"] = (float(i), np.zeros(1536))
        _evict_lru()
        assert len(_CACHE) == 10
