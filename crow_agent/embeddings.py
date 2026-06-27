"""Embedding-based semantic search — local model primary, OpenRouter fallback.

Uses sentence-transformers/all-MiniLM-L6-v2 locally (80MB, free, no API).
Falls back to OpenRouter API if model not installed.

Interface:
    semantic_search(query, items, top_k) -> list[tuple[str, float]]
    embed(texts) -> np.ndarray | None
    store_memory_embedding, precompute_items (cache helpers)
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import numpy as np
import requests

logger = logging.getLogger("crow_agent.embeddings")

_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
_API_URL = "https://openrouter.ai/api/v1/embeddings"
_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
_TIMEOUT = 15

_CACHE: dict[str, tuple[float, np.ndarray]] = {}
_MAX_CACHE_SIZE = 200

# Lazy-loaded local model
_local_model = None
_local_model_failed = False


def _get_local_model():
    """Lazy-load sentence-transformers model. Returns None if unavailable."""
    global _local_model, _local_model_failed
    if _local_model is not None:
        return _local_model
    if _local_model_failed:
        return None
    try:
        from sentence_transformers import SentenceTransformer
        _local_model = SentenceTransformer(_MODEL_NAME)
        logger.info("Loaded local embedding model: %s", _MODEL_NAME)
        return _local_model
    except ImportError:
        logger.info("sentence-transformers not installed — using OpenRouter fallback")
        _local_model_failed = True
        return None
    except Exception:
        logger.warning("Local embedding model failed to load, using OpenRouter")
        _local_model_failed = True
        return None


def _evict_lru() -> None:
    if len(_CACHE) <= _MAX_CACHE_SIZE:
        return
    sorted_keys = sorted(_CACHE.items(), key=lambda x: x[1][0])
    overage = len(_CACHE) - _MAX_CACHE_SIZE
    for key, _ in sorted_keys[:overage]:
        del _CACHE[key]


def embed(texts: list[str]) -> np.ndarray | None:
    """Embed texts — local model first, OpenRouter API fallback."""
    if not texts:
        return None

    # Primary: local sentence-transformers
    model = _get_local_model()
    if model is not None:
        try:
            vectors = model.encode(texts, convert_to_numpy=True, show_progress_bar=False)
            if vectors.ndim == 1:
                vectors = vectors.reshape(1, -1)
            return np.array(vectors, dtype=np.float32)
        except Exception:
            logger.debug("Local embedding failed, trying OpenRouter", exc_info=True)

    # Fallback: OpenRouter API
    if not _API_KEY:
        logger.warning("No embedding available — install sentence-transformers or set OPENROUTER_API_KEY")
        return None
    try:
        resp = requests.post(
            _API_URL,
            headers={
                "Authorization": f"Bearer {_API_KEY}",
                "Content-Type": "application/json",
            },
            json={"model": _MODEL_NAME, "input": texts},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        return np.array([d["embedding"] for d in data["data"]], dtype=np.float32)
    except Exception:
        logger.debug("OpenRouter embedding failed", exc_info=True)
        return None


def semantic_search(
    query: str,
    items: dict[str, str],
    top_k: int = 5,
    *,
    recheck_mtimes: dict[str, float] | None = None,
) -> list[tuple[str, float]]:
    if not items:
        return []
    if recheck_mtimes:
        _evict_stale(items, recheck_mtimes)
    uncached = {k: v for k, v in items.items() if k not in _CACHE}
    if uncached:
        keys, texts = zip(*uncached.items())
        vectors = embed(list(texts))
        if vectors is None:
            return []
        for i, key in enumerate(keys):
            _CACHE[key] = (time.time(), vectors[i])
        _evict_lru()
    query_vec = embed([query])
    if query_vec is None:
        return []
    query_vec = query_vec[0]
    q_norm = float(np.linalg.norm(query_vec))
    if q_norm == 0:
        return []
    scores: list[tuple[str, float]] = []
    for key in items:
        if key not in _CACHE:
            continue
        _, vec = _CACHE[key]
        v_norm = float(np.linalg.norm(vec))
        if v_norm == 0:
            continue
        sim = float(np.dot(query_vec, vec) / (q_norm * v_norm))
        scores.append((key, sim))
    scores.sort(key=lambda x: x[1], reverse=True)
    return scores[:top_k]


def precompute_items(prefix: str, items_dict: dict[str, str]) -> None:
    items = {f"{prefix}:{key}": text for key, text in items_dict.items()}
    _batch_embed(items)


def store_memory_embedding(obs_id: str, text: str) -> None:
    vec = embed([text])
    if vec is not None:
        _CACHE[f"memory:{obs_id}"] = (time.time(), vec[0])
        _evict_lru()


def _batch_embed(items: dict[str, str]) -> None:
    new = {k: v for k, v in items.items() if k not in _CACHE}
    if not new:
        return
    keys, texts = zip(*new.items())
    vectors = embed(list(texts))
    if vectors is not None:
        for i, key in enumerate(keys):
            _CACHE[key] = (time.time(), vectors[i])
        _evict_lru()


def _evict_stale(items: dict[str, str], mtimes: dict[str, float]) -> None:
    stale: list[str] = []
    for key in items:
        if key not in _CACHE:
            continue
        cached_ts = _CACHE[key][0]
        for fpath, mtime in mtimes.items():
            fname = Path(fpath).stem
            if key.startswith("skill:") and key.split(":", 1)[1] == fname:
                if cached_ts < mtime:
                    stale.append(key)
                break
            if key.startswith("vault:") and fname in key:
                if cached_ts < mtime:
                    stale.append(key)
                break
    for key in stale:
        _CACHE.pop(key, None)
