"""Embedding-based semantic search via OpenRouter API.

ponytail: single function + inline mtime re-embed. No watcher threads,
no vector DB, no hybrid scoring. LLM merges lexical + semantic results.

Interface:
    semantic_search(query, items, top_k) -> list[tuple[str, float]]
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import numpy as np
import requests

logger = logging.getLogger("crow_agent.embeddings")

_MODEL = "sentence-transformers/all-mpnet-base-v2"
_API_URL = "https://openrouter.ai/api/v1/embeddings"
_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
_TIMEOUT = 15

_CACHE: dict[str, tuple[float, np.ndarray]] = {}
_MAX_CACHE_SIZE = 200


def _evict_lru() -> None:
    if len(_CACHE) <= _MAX_CACHE_SIZE:
        return
    sorted_keys = sorted(_CACHE.items(), key=lambda x: x[1][0])  # oldest first
    overage = len(_CACHE) - _MAX_CACHE_SIZE
    for key, _ in sorted_keys[:overage]:
        del _CACHE[key]


def embed(texts: list[str]) -> np.ndarray | None:
    if not texts:
        return None
    if not _API_KEY:
        logger.warning("OPENROUTER_API_KEY not set — semantic search disabled")
        return None
    try:
        resp = requests.post(
            _API_URL,
            headers={
                "Authorization": f"Bearer {_API_KEY}",
                "Content-Type": "application/json",
            },
            json={"model": _MODEL, "input": texts},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        return np.array([d["embedding"] for d in data["data"]], dtype=np.float32)
    except Exception:
        logger.debug("Embedding API call failed", exc_info=True)
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
