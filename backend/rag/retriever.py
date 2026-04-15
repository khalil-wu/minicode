"""
向量检索器（DESIGN.md §四.1/4.2）。

两种 RAG 路径共用此检索器：
  1. 被动 RAG: Context 构建时静默注入（用户无感知）
  2. 主动 RAG: Agent 调用 mcp__memory_rag__recall 按需检索

检索后的后处理：
  - 相关性过滤: 低于阈值的结果丢弃
  - 去重: 内容高度重叠的结果合并
  - 截断: 按 token 预算裁剪
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class RetrievedChunk:
    """检索到的内容块。"""
    content: str
    score: float  # 相关性分数 [0, 1]
    source: str = ""  # 来源标识
    metadata: dict[str, Any] = field(default_factory=dict)


class Retriever:
    """
    向量检索器。

    使用示例：
        retriever = Retriever()
        results = retriever.retrieve(
            query="WebSocket 消息处理",
            collection=chroma_collection,
            top_k=5,
            min_score=0.7,
        )
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
        collection: Any,  # chromadb.Collection
        top_k: int | None = None,
        min_score: float | None = None,
        where_filter: dict[str, Any] | None = None,
    ) -> list[RetrievedChunk]:
        """
        从 ChromaDB collection 检索相关内容。

        Args:
            query: 查询文本
            collection: ChromaDB collection 对象
            top_k: 返回数量上限
            min_score: 最低相关性阈值
            where_filter: 元数据过滤条件

        Returns:
            RetrievedChunk 列表（按相关性排序）
        """
        top_k = top_k or self._default_top_k
        min_score = min_score if min_score is not None else self._default_min_score

        try:
            query_kwargs: dict[str, Any] = {
                "query_texts": [query],
                "n_results": top_k,
            }
            if where_filter:
                query_kwargs["where"] = where_filter

            results = collection.query(**query_kwargs)
        except Exception as exc:
            logger.error("向量检索失败: %s", exc)
            return []

        if not results or not results["ids"] or not results["ids"][0]:
            return []

        chunks: list[RetrievedChunk] = []
        ids = results["ids"][0]
        docs = results["documents"][0] if results.get("documents") else []
        metas = results["metadatas"][0] if results.get("metadatas") else []
        distances = results["distances"][0] if results.get("distances") else []

        for i, (doc_id, doc, meta) in enumerate(zip(ids, docs, metas)):
            # 距离转相关性分数
            score = 1.0 - (distances[i] if i < len(distances) else 0.5)

            # 过滤低相关性
            if score < min_score:
                continue

            chunks.append(RetrievedChunk(
                content=doc,
                score=score,
                source=meta.get("source", "") if meta else "",
                metadata=meta or {},
            ))

        # 去重：内容高度重叠的合并
        chunks = self._deduplicate(chunks)

        return chunks

    def retrieve_and_format(
        self,
        query: str,
        collection: Any,
        top_k: int = 3,
        min_score: float = 0.82,
        max_tokens: int = 3000,
    ) -> str:
        """
        检索并格式化为注入 context 的文本。

        用于被动 RAG：直接返回可拼入 system prompt 的文本。

        Args:
            query: 查询文本
            collection: ChromaDB collection
            top_k: 返回数量
            min_score: 相关性阈值（被动 RAG 用较高阈值）
            max_tokens: 输出 token 上限

        Returns:
            格式化的背景知识文本，空字符串表示无相关内容
        """
        chunks = self.retrieve(
            query=query,
            collection=collection,
            top_k=top_k,
            min_score=min_score,
        )

        if not chunks:
            return ""

        # 按 token 预算裁剪
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
        去除内容高度重叠的检索结果。

        使用简单的 Jaccard 相似度判断重叠。
        """
        if len(chunks) <= 1:
            return chunks

        result: list[RetrievedChunk] = []

        for chunk in chunks:
            is_duplicate = False
            chunk_words = set(chunk.content.split())

            for existing in result:
                existing_words = set(existing.content.split())
                if not chunk_words or not existing_words:
                    continue

                # Jaccard 相似度
                intersection = len(chunk_words & existing_words)
                union = len(chunk_words | existing_words)
                similarity = intersection / union if union > 0 else 0

                if similarity > similarity_threshold:
                    is_duplicate = True
                    break

            if not is_duplicate:
                result.append(chunk)

        return result
