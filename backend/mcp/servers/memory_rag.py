"""
memory-rag MCP Server（DESIGN.md §六.2）。

传输：stdio
依赖：chromadb（向量存储）、openai（embedding）

功能概述：
  向量记忆存储与检索。Agent 可以主动「记住」和「回忆」信息，
  实现 Agentic RAG（Agent 主动调用 remember/recall）。
  同时支持被动 RAG（Context 构建时静默检索）。

Tools:
  remember(content, tags=[], importance=3) → MemoryId
    存储一段内容到向量数据库
    importance: 1-5，影响召回权重

  recall(query, top_k=5, min_score=0.6) → 匹配结果列表
    语义搜索记忆库

  get_memory(memory_id) → 完整内容
    按 ID 读取完整记忆

  forget(memory_id) → 是否成功
    删除一条记忆

Resources:
  memory://stats → 记忆库统计
  memory://tags → 所有标签列表

运行方式：python -m backend.mcp.servers.memory_rag
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

from backend.memory.vector_memory import VectorMemory

logger = logging.getLogger(__name__)

# ── MCP Server ──────────────────────────────────────────────

try:
    from mcp.server.fastmcp import FastMCP
    HAS_MCP = True
except ImportError:
    HAS_MCP = False

if HAS_MCP:
    mcp = FastMCP(
        "memory-rag",
        instructions=(
            "向量记忆 MCP Server。提供语义记忆的存储和检索能力。"
            "Agent 可以主动记住重要信息，并在需要时语义召回。"
        ),
    )
else:
    mcp = None


# ── 向量存储封装 ────────────────────────────────────────────

DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data" / "chroma"

_vector_memory: VectorMemory | None = None


def _get_vector_memory() -> VectorMemory:
    """Return the shared memory backend used by both local and MCP tools."""
    global _vector_memory

    if _vector_memory is None:
        _vector_memory = VectorMemory(storage_dir=DATA_DIR, collection_name="memory")
    return _vector_memory

def _format_memory_id(memory_id: str) -> str:
    return memory_id.strip()


# ── MCP Tools ──────────────────────────────────────────────

if HAS_MCP and mcp:

    @mcp.tool()
    def remember(
        content: str,
        tags: list[str] | None = None,
        importance: int = 3,
    ) -> str:
        """
        将内容存入记忆库。

        存储一段文本到向量数据库，支持标签和重要性标注。
        后续可以通过 recall() 语义检索。

        Args:
            content: 要记住的内容
            tags: 标签列表（如 ["用户偏好", "项目配置"]）
            importance: 重要性 1-5（5 最重要，影响召回权重）

        Returns:
            记忆 ID 和确认信息

        示例:
            remember("用户偏好使用 TypeScript + React 技术栈", tags=["偏好"], importance=4)
            remember("项目根目录是 /c/Desktop/MiniCode", tags=["项目"])
        """
        memory = _get_vector_memory()

        tags = tags or []
        importance = max(1, min(5, importance))

        try:
            memory_id = memory.remember(
                content=content,
                tags=tags,
                importance=importance,
                metadata={
                    "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "char_count": len(content),
                    "source": "mcp:memory-rag",
                },
            )
        except Exception as exc:
            return f"错误: 存储失败: {exc}"

        return (
            f"已存入记忆库。\n"
            f"- **ID**: `{memory_id}`\n"
            f"- **标签**: {', '.join(tags) or '无'}\n"
            f"- **重要性**: {'★' * importance}{'☆' * (5 - importance)}\n"
            f"- **字数**: {len(content)}"
        )


    @mcp.tool()
    def recall(
        query: str,
        top_k: int = 5,
        min_score: float = 0.6,
    ) -> str:
        """
        语义搜索记忆库。

        使用向量相似度匹配，返回最相关的记忆片段。

        Args:
            query: 搜索查询（自然语言）
            top_k: 最多返回条数（1-20，默认 5）
            min_score: 最低相关性阈值（0-1，默认 0.6）

        Returns:
            匹配的记忆列表

        示例:
            recall("用户的技术栈偏好")
            recall("之前关于路由设计的讨论", top_k=3)
        """
        memory = _get_vector_memory()

        top_k = max(1, min(20, top_k))

        try:
            results = memory.recall(query=query, top_k=top_k, min_score=min_score)
        except Exception as exc:
            return f"错误: 检索失败: {exc}"

        if not results:
            return f"未找到与 \"{query}\" 相关的记忆。"

        lines = [f"搜索 \"{query}\" 的记忆：\n"]
        for count, item in enumerate(results, start=1):
            mid = _format_memory_id(str(item.get("memory_id") or ""))
            score = float(item.get("score") or 0.0)
            importance = int(item.get("importance") or 3)
            tags = [str(tag) for tag in item.get("tags", []) if str(tag).strip()]
            preview = str(item.get("summary") or "")

            lines.append(f"{count}. **[{mid}]** (相关性: {score:.2f}, {'★' * importance})")
            if tags:
                lines.append(f"   标签: {', '.join(tags)}")
            lines.append(f"   {preview}")
            lines.append("")

        lines.append(f"共 {len(results)} 条匹配。使用 get_memory(id) 获取完整内容。")
        return "\n".join(lines)


    @mcp.tool()
    def get_memory(memory_id: str) -> str:
        """
        按 ID 获取完整记忆内容。

        Args:
            memory_id: 记忆 ID（由 remember() 返回）

        Returns:
            完整的记忆内容
        """
        memory = _get_vector_memory()

        try:
            doc = memory.get_memory(memory_id)
        except Exception as exc:
            return f"错误: 读取失败: {exc}"

        if not doc:
            return f"记忆 '{memory_id}' 不存在。"

        return (
            f"**记忆 {memory_id}**\n"
            f"{doc}"
        )


    @mcp.tool()
    def forget(memory_id: str) -> str:
        """
        删除一条记忆。

        Args:
            memory_id: 要删除的记忆 ID

        Returns:
            操作结果
        """
        memory = _get_vector_memory()

        try:
            existing = memory.get_memory(memory_id)
            if not existing:
                return f"记忆 '{memory_id}' 不存在。"
            memory.forget(memory_id)
            return f"已删除记忆 `{memory_id}`。"
        except Exception as exc:
            return f"错误: 删除失败: {exc}"


    @mcp.resource("memory://stats")
    def memory_stats() -> str:
        """记忆库统计信息。"""
        memory = _get_vector_memory()
        count = len(memory.list_memories(limit=1000))
        return f"记忆库统计：共 {count} 条记忆。"


    @mcp.resource("memory://tags")
    def memory_tags() -> str:
        """所有标签列表。"""
        try:
            all_data = _get_vector_memory().list_memories(limit=1000)
            tags_set: set[str] = set()
            for item in all_data:
                tags_set.update(str(tag) for tag in item.get("tags", []) if str(tag).strip())
            if not tags_set:
                return "暂无标签。"
            return "所有标签：\n" + "\n".join(f"- {t}" for t in sorted(tags_set))
        except Exception:
            return "获取标签失败。"


# ── 入口 ────────────────────────────────────────────────────

def main() -> None:
    if not HAS_MCP or not mcp:
        print("错误: 需要安装 MCP SDK: pip install 'mcp[cli]'", file=sys.stderr)
        sys.exit(1)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
