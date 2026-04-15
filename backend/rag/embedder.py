"""
Embedding 模型封装（DESIGN.md §四.3）。

支持：
  - OpenAI text-embedding-3-small（默认，低成本，1536 维）
  - 离线模式：简单的 TF-IDF 近似（开发模式，无需 API）

设计要点：
  - 统一接口：embed(text) → list[float]
  - 批量接口：embed_batch(texts) → list[list[float]]
  - 自动 fallback：API 不可用时降级到离线模式
"""

from __future__ import annotations

import hashlib
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


class Embedder:
    """
    文本向量化封装。

    使用示例：
        embedder = Embedder()
        vec = await embedder.embed("Hello, world!")
        vecs = await embedder.embed_batch(["Hello", "World"])
    """

    def __init__(
        self,
        model: str = "text-embedding-3-small",
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self._model = model
        self._api_key = api_key or os.getenv("OPENAI_API_KEY", "")
        self._base_url = base_url or os.getenv("OPENAI_BASE_URL", "")
        self._client = None
        self._dimension = 1536

    async def embed(self, text: str) -> list[float]:
        """
        将单段文本转为向量。

        Args:
            text: 输入文本

        Returns:
            浮点向量
        """
        results = await self.embed_batch([text])
        return results[0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """
        批量文本向量化。

        优先使用 OpenAI API，失败时 fallback 到离线模式。

        Args:
            texts: 文本列表

        Returns:
            向量列表
        """
        if not texts:
            return []

        # 尝试 OpenAI API
        if self._api_key:
            try:
                return await self._embed_openai(texts)
            except Exception as exc:
                logger.warning("OpenAI embedding 失败: %s, fallback 到离线模式", exc)

        # Fallback：离线哈希向量（开发模式，不适合生产）
        return [self._offline_embed(text) for text in texts]

    async def _embed_openai(self, texts: list[str]) -> list[list[float]]:
        """调用 OpenAI Embeddings API。"""
        if not self._client:
            from openai import AsyncOpenAI
            kwargs: dict[str, Any] = {"api_key": self._api_key}
            if self._base_url:
                kwargs["base_url"] = self._base_url
            self._client = AsyncOpenAI(**kwargs)

        # 限制单次批量大小
        batch_size = 100
        all_embeddings: list[list[float]] = []

        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            response = await self._client.embeddings.create(
                model=self._model,
                input=batch,
            )
            for item in response.data:
                all_embeddings.append(item.embedding)

        return all_embeddings

    def _offline_embed(self, text: str) -> list[float]:
        """
        离线哈希向量（开发模式）。

        使用文本的 hash 生成伪向量。
        不适合语义搜索，仅用于开发测试。
        """
        # 用 hash 生成 deterministic 伪向量
        hash_bytes = hashlib.sha256(text.encode()).digest()
        # 扩展到所需维度
        dim = min(self._dimension, 256)
        vec = []
        for i in range(dim):
            byte_val = hash_bytes[i % len(hash_bytes)]
            vec.append((byte_val / 255.0) * 2 - 1)  # 归一化到 [-1, 1]

        # 填充到目标维度
        while len(vec) < self._dimension:
            vec.append(0.0)

        # L2 归一化
        norm = sum(v ** 2 for v in vec) ** 0.5
        if norm > 0:
            vec = [v / norm for v in vec]

        return vec

    @property
    def dimension(self) -> int:
        return self._dimension
