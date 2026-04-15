"""
被动 RAG 流水线（DESIGN.md §四.1）。

在 Context 构建时静默调用，用户无感知：
  1. 用 user_message 做向量检索
  2. Top-K=3, 相关性阈值 0.82（高阈值避免噪声）
  3. 注入为 <background> 块，限制 3K tokens
  4. 只在有相关内容时注入，无匹配时不注入任何内容

主动 RAG vs 被动 RAG（DESIGN.md §四 双模式）：
  - 被动 RAG: 本模块 — Context 构建时静默注入，用户无感知
  - 主动 RAG: MCP memory-rag Server — Agent 主动调用 recall() 工具
  两者共用同一 ChromaDB 存储，数据不重复。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ChromaDB 数据目录
DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "chroma"


class RAGPipeline:
    """
    被动 RAG 流水线。

    在 ContextBuilder.build() 中调用 retrieve_context()，
    将检索结果注入 state.retrieved_chunks。

    使用示例：
        pipeline = RAGPipeline()
        background = pipeline.retrieve_context(user_message)
        if background:
            state.retrieved_chunks = [background]
    """

    def __init__(
        self,
        top_k: int = 3,
        min_score: float = 0.82,
        max_tokens: int = 3000,
    ) -> None:
        self._top_k = top_k
        self._min_score = min_score
        self._max_tokens = max_tokens
        self._collection = None
        self._initialized = False

    def _ensure_initialized(self) -> bool:
        """懒初始化 ChromaDB。"""
        if self._initialized:
            return self._collection is not None

        self._initialized = True

        try:
            import chromadb
        except ImportError:
            logger.debug("chromadb 未安装，被动 RAG 禁用")
            return False

        if not DATA_DIR.exists():
            logger.debug("ChromaDB 数据目录不存在，被动 RAG 禁用")
            return False

        try:
            client = chromadb.PersistentClient(path=str(DATA_DIR))
            # 尝试获取 memory collection
            self._collection = client.get_or_create_collection(
                name="memory",
                metadata={"hnsw:space": "cosine"},
            )
            count = self._collection.count()
            if count == 0:
                logger.debug("记忆库为空，被动 RAG 暂不启用")
                return False

            logger.info("被动 RAG 就绪，记忆库 %d 条", count)
            return True

        except Exception as exc:
            logger.warning("被动 RAG 初始化失败: %s", exc)
            return False

    def retrieve_context(self, user_message: str) -> str:
        """
        根据用户消息检索背景知识。

        Args:
            user_message: 用户消息

        Returns:
            格式化的背景知识文本。空字符串表示无相关内容。
        """
        if not self._ensure_initialized():
            return ""

        # 消息太短不触发检索（避免噪声）
        if len(user_message.strip()) < 10:
            return ""

        try:
            from backend.rag.retriever import Retriever

            retriever = Retriever(
                default_top_k=self._top_k,
                default_min_score=self._min_score,
            )

            result = retriever.retrieve_and_format(
                query=user_message,
                collection=self._collection,
                top_k=self._top_k,
                min_score=self._min_score,
                max_tokens=self._max_tokens,
            )

            if result:
                logger.debug(
                    "被动 RAG 注入 %d 字符背景知识",
                    len(result),
                )

            return result

        except Exception as exc:
            logger.warning("被动 RAG 检索失败: %s", exc)
            return ""

    def is_available(self) -> bool:
        """检查被动 RAG 是否可用。"""
        return self._ensure_initialized()

    @property
    def stats(self) -> dict[str, Any]:
        """获取统计信息。"""
        if not self._collection:
            return {"available": False, "count": 0}
        return {
            "available": True,
            "count": self._collection.count(),
            "top_k": self._top_k,
            "min_score": self._min_score,
        }
