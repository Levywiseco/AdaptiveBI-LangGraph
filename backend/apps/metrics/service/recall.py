"""Semantic recall for governed metric candidates.

Lexical matching only finds metrics whose name or alias appears in the question.
When embeddings are enabled, the question is also compared with each published
version's business description, so "卖了多少钱" can still reach "成交总额".
Any embedding failure degrades to lexical recall; it never blocks a question.
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections.abc import Iterable

from apps.metrics.models.metric import MetricDefinition, MetricVersion
from common.core.config import settings

_CACHE_LIMIT = 4096
# A missing or broken embedding model must not slow every question down.
_FAILURE_BACKOFF_SECONDS = 600
_cache: dict[tuple[int, str], list[float]] = {}
_cache_lock = threading.Lock()
_disabled_until = 0.0


def metric_text(metric: MetricDefinition, version: MetricVersion) -> str:
    parts = [metric.name, *metric.aliases, metric.description or "", version.unit or ""]
    return "；".join(part.strip() for part in parts if part and part.strip())


def _cache_key(version: MetricVersion, text: str) -> tuple[int, str]:
    return int(version.id), hashlib.sha256(text.encode("utf-8")).hexdigest()


def vector_similarities(
    question: str,
    rows: Iterable[tuple[MetricDefinition, MetricVersion]],
) -> dict[int, float]:
    """Cosine similarity per metric version id; empty when embeddings are unavailable."""
    global _disabled_until
    rows = list(rows)
    if (not settings.EMBEDDING_ENABLED or not rows or not question.strip()
            or time.monotonic() < _disabled_until):
        return {}
    try:
        from apps.ai_model.embedding import EmbeddingModelCache
        from apps.datasource.embedding.utils import cosine_similarity

        model = EmbeddingModelCache.get_model()
        keyed = [(_cache_key(version, metric_text(metric, version)), metric_text(metric, version))
                 for metric, version in rows]
        with _cache_lock:
            missing = [(key, text) for key, text in keyed if key not in _cache]
        if missing:
            vectors = model.embed_documents([text for _key, text in missing])
            with _cache_lock:
                if len(_cache) + len(missing) > _CACHE_LIMIT:
                    _cache.clear()
                for (key, _text), vector in zip(missing, vectors, strict=True):
                    _cache[key] = list(vector)
        question_vector = model.embed_query(question)
        with _cache_lock:
            vectors = {key[0]: _cache.get(key) for key, _text in keyed}
        return {
            version_id: cosine_similarity(question_vector, vector)
            for version_id, vector in vectors.items()
            if vector is not None
        }
    except Exception:
        _disabled_until = time.monotonic() + _FAILURE_BACKOFF_SECONDS
        logging.getLogger("adaptive.metrics").warning("metric_vector_recall_unavailable", exc_info=True)
        return {}


def vector_score(similarity: float | None) -> int:
    """Map a similarity onto the lexical scale: below alias containment (500+),
    above partial character overlap (<100)."""
    if similarity is None or similarity < settings.GRAPH_METRIC_VECTOR_MIN_SIMILARITY:
        return 0
    return round(400 * similarity)
