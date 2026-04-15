"""
Artifact Store — 会话级产物存储（DESIGN.md §9）。

消息层 vs 产物层分离的核心实现：
  - 消息层（Message Layer）：在 context window 中流转，每条工具结果 ≤ 500 tokens
  - 产物层（Artifact Layer）：在 Artifact Store 中存储，Agent 调用 read_artifact 按需读取

会话结束后清理（重要内容已进长期记忆）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ArtifactMeta:
    """Artifact 元数据。"""

    artifact_id: str
    source: str  # 由哪个工具/操作产生
    type: str  # 内容类型：code / text / search_result / command_output
    size: int  # 字符数
    preview: str  # 前几行预览


class ArtifactStore:
    """
    会话级产物存储。

    设计要点：
    - 工具大输出不直接塞进 context，写入此处，只在 context 中保留引用
    - 内存字典实现，会话级生命周期
    - Agent 通过 read_artifact(artifact_id) 工具按需读取
    """

    def __init__(self) -> None:
        self._store: dict[str, str] = {}
        self._meta: dict[str, ArtifactMeta] = {}

    def save(
        self,
        content: str,
        source: str,
        type: str = "text",
        preview_lines: int = 5,
    ) -> str:
        """
        存储大内容，返回 artifact_id。

        Args:
            content: 完整内容
            source: 来源（工具名或操作描述）
            type: 内容类型
            preview_lines: 预览行数

        Returns:
            artifact_id
        """
        artifact_id = f"art_{uuid.uuid4().hex[:8]}"
        self._store[artifact_id] = content

        lines = content.split("\n")
        preview = "\n".join(lines[:preview_lines])
        if len(lines) > preview_lines:
            preview += f"\n... (共 {len(lines)} 行)"

        self._meta[artifact_id] = ArtifactMeta(
            artifact_id=artifact_id,
            source=source,
            type=type,
            size=len(content),
            preview=preview,
        )

        return artifact_id

    def get(self, artifact_id: str) -> str | None:
        """读取完整内容。"""
        return self._store.get(artifact_id)

    def get_preview(self, artifact_id: str, lines: int = 5) -> str | None:
        """读取预览。"""
        content = self._store.get(artifact_id)
        if content is None:
            return None
        content_lines = content.split("\n")
        preview = "\n".join(content_lines[:lines])
        if len(content_lines) > lines:
            preview += f"\n... (共 {len(content_lines)} 行)"
        return preview

    def get_meta(self, artifact_id: str) -> ArtifactMeta | None:
        """读取元数据。"""
        return self._meta.get(artifact_id)

    def list_artifacts(self) -> list[ArtifactMeta]:
        """列出所有 artifact。"""
        return list(self._meta.values())

    def clear(self) -> None:
        """清理所有 artifact（会话结束时调用）。"""
        self._store.clear()
        self._meta.clear()

    @property
    def count(self) -> int:
        return len(self._store)
