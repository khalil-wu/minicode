"""
Vector retrieval shared by passive and active RAG flows.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class RetrievedChunk:
    """A retrieved chunk plus score and source metadata."""

    content: str
    score: float
    source: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class Retriever:
    """
    Shared vector retriever.

    Used by:
    1. Passive RAG: context injection during prompt building.
    2. Active RAG: explicit recall tools.
    """

    def __init__(
        self,
        default_top_k: int = 5,
        default_min_score: float = 0.7,
    ) -> None:
        self._default_top_k = default_top_k
        self._default_min_score = default_min_score

    def retrieve(
        self,
        query: str,
        collection: Any,
        top_k: int | None = None,
        min_score: float | None = None,
        where_filter: dict[str, Any] | None = None,
        query_embeddings: list[list[float]] | None = None,
    ) -> list[RetrievedChunk]:
        """
        Retrieve relevant chunks from a Chroma collection.
        """

        top_k = top_k or self._default_top_k
        min_score = min_score if min_score is not None else self._default_min_score

        query_kwargs: dict[str, Any] = {"n_results": top_k}
        if query_embeddings:
            query_kwargs["query_embeddings"] = query_embeddings
        else:
            query_kwargs["query_texts"] = [query]

        if where_filter:
            query_kwargs["where"] = where_filter

        try:
            results = collection.query(**query_kwargs)
        except Exception as exc:
            if not query_embeddings:
                logger.error("向量检索失败: %s", exc)
                return []

            logger.warning("向量检索失败，回退到 query_texts: %s", exc)
            fallback_kwargs: dict[str, Any] = {
                "n_results": top_k,
                "query_texts": [query],
            }
            if where_filter:
                fallback_kwargs["where"] = where_filter
            try:
                results = collection.query(**fallback_kwargs)
            except Exception as fallback_exc:
                logger.error("向量检索失败: %s", fallback_exc)
                return []

        if not results or not results["ids"] or not results["ids"][0]:
            return []

        chunks: list[RetrievedChunk] = []
        ids = results["ids"][0]
        docs = results["documents"][0] if results.get("documents") else []
        metas = results["metadatas"][0] if results.get("metadatas") else []
        distances = results["distances"][0] if results.get("distances") else []

        for i, (_doc_id, doc, meta) in enumerate(zip(ids, docs, metas)):
            score = 1.0 - (distances[i] if i < len(distances) else 0.5)
            if score < min_score:
                continue

            chunks.append(
                RetrievedChunk(
                    content=doc,
                    score=score,
                    source=meta.get("source", "") if meta else "",
                    metadata=meta or {},
                )
            )

        chunks = self._deduplicate(chunks)
        return chunks

    def retrieve_and_format(
        self,
        query: str,
        collection: Any,
        top_k: int = 3,
        min_score: float = 0.82,
        max_tokens: int = 3000,
        query_embeddings: list[list[float]] | None = None,
    ) -> str:
        """
        Retrieve and format results for prompt injection.
        """

        chunks = self.retrieve(
            query=query,
            collection=collection,
            top_k=top_k,
            min_score=min_score,
            query_embeddings=query_embeddings,
        )

        if not chunks:
            return ""

        parts: list[str] = []
        used = 0
        max_chars = max_tokens * 4

        for chunk in chunks:
            chunk_chars = len(chunk.content)
            if used + chunk_chars > max_chars:
                remaining = max_chars - used
                if remaining > 100:
                    parts.append(chunk.content[:remaining] + "...")
                break
            parts.append(chunk.content)
            used += chunk_chars

        return "\n\n---\n\n".join(parts)

    @staticmethod
    def _deduplicate(
        chunks: list[RetrievedChunk],
        similarity_threshold: float = 0.8,
    ) -> list[RetrievedChunk]:
        """
        Remove highly overlapping chunks using character 3-gram Jaccard similarity.
        """

        if len(chunks) <= 1:
            return chunks

        def _ngrams(text: str, n: int = 3) -> set[str]:
            text = text.lower().strip()
            if len(text) < n:
                return {text}
            return {text[i : i + n] for i in range(len(text) - n + 1)}

        result: list[RetrievedChunk] = []

        for chunk in chunks:
            is_duplicate = False
            chunk_grams = _ngrams(chunk.content)

            if not chunk_grams:
                continue

            for existing in result:
                existing_grams = _ngrams(existing.content)
                if not existing_grams:
                    continue

                intersection = len(chunk_grams & existing_grams)
                union = len(chunk_grams | existing_grams)
                similarity = intersection / union if union > 0 else 0

                if similarity > similarity_threshold:
                    is_duplicate = True
                    break

            if not is_duplicate:
                result.append(chunk)

        return result
