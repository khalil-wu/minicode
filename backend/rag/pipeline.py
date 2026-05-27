"""
Passive RAG pipeline used by the context builder.
"""

from __future__ import annotations

import hashlib
import inspect
import logging
from collections import OrderedDict
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "chroma"
CHROMA_HNSW_METADATA = {
    "hnsw:space": "cosine",
    "hnsw:ef_construction": 200,
    "hnsw:M": 32,
    "hnsw:search_ef": 100,
}
_CACHE_SIZE = 256
_SEMANTIC_KEY_PRECISION = 2  # 保留 2 位小数做语义键量化，覆盖改写型相同查询


class RAGPipeline:
    """Retrieve and format background context across all available collections."""

    def __init__(
        self,
        top_k: int = 3,
        min_score: float = 0.35,
        max_tokens: int = 3000,
    ) -> None:
        self._top_k = top_k
        self._min_score = min_score
        self._max_tokens = max_tokens
        self._collections: dict[str, Any] = {}
        self._retriever = None
        self._embedder = None
        self._initialized = False
        self._sync_cache: OrderedDict[str, str] = OrderedDict()
        self._async_cache: OrderedDict[str, str] = OrderedDict()

    def _ensure_initialized(self) -> bool:
        if self._initialized:
            return len(self._collections) > 0

        self._initialized = True

        try:
            import chromadb
        except ImportError:
            logger.debug("chromadb is unavailable; passive RAG disabled")
            return False

        if not DATA_DIR.exists():
            logger.debug("ChromaDB data directory is missing; passive RAG disabled")
            return False

        try:
            client = chromadb.PersistentClient(path=str(DATA_DIR))
            for name in ("memory", "documents", "codebase"):
                try:
                    collection = self._get_or_create_collection(client, name)
                    if collection.count() > 0:
                        self._collections[name] = collection
                except Exception:
                    continue

            if not self._collections:
                logger.debug("No non-empty RAG collections were found")
                return False

            from backend.rag.retriever import Retriever

            self._retriever = Retriever(
                default_top_k=self._top_k,
                default_min_score=self._min_score,
            )

            try:
                from backend.rag.embedder import Embedder

                self._embedder = Embedder()
            except Exception:
                self._embedder = None

            total = sum(collection.count() for collection in self._collections.values())
            logger.info(
                "Passive RAG ready with %d collections and %d records",
                len(self._collections),
                total,
            )
            return True
        except Exception as exc:
            logger.warning("Passive RAG initialization failed: %s", exc)
            return False

    def retrieve_context(self, user_message: str) -> str:
        key = self._cache_key(user_message)
        cached = self._cache_get(self._sync_cache, key)
        if cached is not None:
            return cached

        result = self._retrieve_context_impl(user_message)
        self._cache_set(self._sync_cache, key, result)
        return result

    async def retrieve_context_async(self, user_message: str) -> str:
        text_key = self._cache_key(user_message)
        cached = self._cache_get(self._async_cache, text_key)
        if cached is not None:
            return cached

        if not self._ensure_initialized():
            return ""

        if len(user_message.strip()) < 10:
            return ""

        query_embeddings: list[list[float]] | None = None
        semantic_key: str | None = None
        if self._embedder is not None:
            try:
                embedding = self._embedder.embed(user_message)
                if inspect.isawaitable(embedding):
                    embedding = await embedding
                query_embeddings = [embedding]
                semantic_key = self._semantic_cache_key(embedding)
            except Exception as exc:
                logger.debug("Async embedding failed, falling back to text query: %s", exc)

        if semantic_key is not None:
            cached_semantic = self._cache_get(self._async_cache, semantic_key)
            if cached_semantic is not None:
                self._cache_set(self._async_cache, text_key, cached_semantic)
                return cached_semantic

        result = self._retrieve_context_impl(
            user_message,
            query_embeddings=query_embeddings,
        )
        self._cache_set(self._async_cache, text_key, result)
        if semantic_key is not None:
            self._cache_set(self._async_cache, semantic_key, result)
        return result

    async def warmup_async(self) -> None:
        """启动期预热：初始化集合 + 预热 embedder。"""
        if not self._ensure_initialized():
            return
        if self._embedder is None:
            return
        try:
            warmup = self._embedder.warmup()
            if inspect.isawaitable(warmup):
                await warmup
        except Exception as exc:  # noqa: BLE001
            logger.debug("RAG embedder warmup failed: %s", exc)

    def _retrieve_context_impl(
        self,
        user_message: str,
        *,
        query_embeddings: list[list[float]] | None = None,
    ) -> str:
        if not self._ensure_initialized():
            return ""

        if len(user_message.strip()) < 10:
            return ""

        try:
            all_results: list[str] = []
            per_collection_top_k = max(1, self._top_k // max(1, len(self._collections)))
            per_collection_max_tokens = max(256, self._max_tokens // max(1, len(self._collections)))
            for collection in self._collections.values():
                result = self._retriever.retrieve_and_format(
                    query=user_message,
                    collection=collection,
                    top_k=per_collection_top_k,
                    min_score=self._min_score,
                    max_tokens=per_collection_max_tokens,
                    query_embeddings=query_embeddings,
                )
                if result:
                    all_results.append(result)

            combined = "\n\n---\n\n".join(all_results)
            max_chars = self._max_tokens * 4
            if len(combined) > max_chars:
                combined = combined[: max_chars - 4] + "..."
            if combined:
                logger.debug("Injected %d characters of passive RAG context", len(combined))
            return combined
        except Exception as exc:
            logger.warning("Passive RAG retrieval failed: %s", exc)
            return ""

    def is_available(self) -> bool:
        return self._ensure_initialized()

    @property
    def stats(self) -> dict[str, Any]:
        if not self._collections:
            return {"available": False, "count": 0}
        return {
            "available": True,
            "collections": {name: coll.count() for name, coll in self._collections.items()},
            "top_k": self._top_k,
            "min_score": self._min_score,
        }

    @staticmethod
    def _cache_key(user_message: str) -> str:
        return "text::" + user_message[:200]

    @staticmethod
    def _get_or_create_collection(client: Any, name: str) -> Any:
        try:
            return client.get_or_create_collection(
                name=name,
                metadata=CHROMA_HNSW_METADATA,
            )
        except Exception as exc:
            if not _should_retry_collection_without_metadata(exc):
                raise
            return client.get_or_create_collection(name=name)

    @staticmethod
    def _semantic_cache_key(embedding: list[float]) -> str:
        if not embedding:
            return ""
        quantized = ",".join(f"{value:.{_SEMANTIC_KEY_PRECISION}f}" for value in embedding)
        digest = hashlib.sha1(quantized.encode("utf-8")).hexdigest()  # noqa: S324
        return "sem::" + digest

    @staticmethod
    def _cache_get(cache: OrderedDict[str, str], key: str) -> str | None:
        if key not in cache:
            return None
        cache.move_to_end(key)
        return cache[key]

    @staticmethod
    def _cache_set(cache: OrderedDict[str, str], key: str, value: str) -> None:
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > _CACHE_SIZE:
            cache.popitem(last=False)


def _should_retry_collection_without_metadata(exc: Exception) -> bool:
    if isinstance(exc, TypeError):
        return True
    text = str(exc).lower()
    return "hnsw" in text and "metadata" in text
