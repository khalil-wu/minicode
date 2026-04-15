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

import hashlib
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

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

# ChromaDB 数据目录
DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data" / "chroma"

_collection = None
_client = None


def _get_collection():
    """懒初始化 ChromaDB collection。"""
    global _collection, _client

    if _collection is not None:
        return _collection

    try:
        import chromadb
    except ImportError:
        logger.error("需要安装 chromadb: pip install chromadb")
        return None

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _client = chromadb.PersistentClient(path=str(DATA_DIR))
    _collection = _client.get_or_create_collection(
        name="memory",
        metadata={"hnsw:space": "cosine"},
    )
    logger.info("ChromaDB memory collection 就绪，数据目录: %s", DATA_DIR)
    return _collection


def _gen_memory_id(content: str) -> str:
    """生成记忆 ID。"""
    return "mem_" + hashlib.md5(
        f"{content[:100]}:{time.time()}".encode()
    ).hexdigest()[:10]


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
        collection = _get_collection()
        if collection is None:
            return "错误: 向量数据库未初始化。请确认已安装 chromadb。"

        tags = tags or []
        importance = max(1, min(5, importance))
        memory_id = _gen_memory_id(content)

        metadata: dict[str, Any] = {
            "tags": json.dumps(tags, ensure_ascii=False),
            "importance": importance,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "char_count": len(content),
        }

        try:
            collection.add(
                ids=[memory_id],
                documents=[content],
                metadatas=[metadata],
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
        collection = _get_collection()
        if collection is None:
            return "错误: 向量数据库未初始化。"

        top_k = max(1, min(20, top_k))

        try:
            results = collection.query(
                query_texts=[query],
                n_results=top_k,
            )
        except Exception as exc:
            return f"错误: 检索失败: {exc}"

        if not results["ids"] or not results["ids"][0]:
            return f"未找到与 \"{query}\" 相关的记忆。"

        # 格式化输出
        lines = [f"搜索 \"{query}\" 的记忆：\n"]
        count = 0

        ids = results["ids"][0]
        docs = results["documents"][0] if results["documents"] else []
        metas = results["metadatas"][0] if results["metadatas"] else []
        distances = results["distances"][0] if results.get("distances") else []

        for i, (mid, doc, meta) in enumerate(zip(ids, docs, metas)):
            # 计算相关性分数（距离转相似度）
            score = 1 - (distances[i] if distances else 0.5)
            if score < min_score:
                continue

            count += 1
            importance = meta.get("importance", 3) if meta else 3
            tags_str = meta.get("tags", "[]") if meta else "[]"
            try:
                tags = json.loads(tags_str)
            except (json.JSONDecodeError, TypeError):
                tags = []
            created = meta.get("created_at", "") if meta else ""

            # 预览（限制长度）
            preview = doc[:200] if doc else ""
            if len(doc) > 200:
                preview += "..."

            lines.append(f"{count}. **[{mid}]** (相关性: {score:.2f}, {'★' * importance})")
            if tags:
                lines.append(f"   标签: {', '.join(tags)}")
            if created:
                lines.append(f"   时间: {created}")
            lines.append(f"   {preview}")
            lines.append("")

        if count == 0:
            return f"未找到相关性 ≥ {min_score} 的记忆。"

        lines.append(f"共 {count} 条匹配。使用 get_memory(id) 获取完整内容。")
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
        collection = _get_collection()
        if collection is None:
            return "错误: 向量数据库未初始化。"

        try:
            result = collection.get(ids=[memory_id])
        except Exception as exc:
            return f"错误: 读取失败: {exc}"

        if not result["ids"]:
            return f"记忆 '{memory_id}' 不存在。"

        doc = result["documents"][0] if result["documents"] else ""
        meta = result["metadatas"][0] if result["metadatas"] else {}

        return (
            f"**记忆 {memory_id}**\n"
            f"- 重要性: {'★' * meta.get('importance', 3)}\n"
            f"- 创建时间: {meta.get('created_at', '未知')}\n\n"
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
        collection = _get_collection()
        if collection is None:
            return "错误: 向量数据库未初始化。"

        try:
            # 先检查是否存在
            existing = collection.get(ids=[memory_id])
            if not existing["ids"]:
                return f"记忆 '{memory_id}' 不存在。"

            collection.delete(ids=[memory_id])
            return f"已删除记忆 `{memory_id}`。"
        except Exception as exc:
            return f"错误: 删除失败: {exc}"


    @mcp.resource("memory://stats")
    def memory_stats() -> str:
        """记忆库统计信息。"""
        collection = _get_collection()
        if collection is None:
            return "向量数据库未初始化。"

        count = collection.count()
        return f"记忆库统计：共 {count} 条记忆。"


    @mcp.resource("memory://tags")
    def memory_tags() -> str:
        """所有标签列表。"""
        collection = _get_collection()
        if collection is None:
            return "向量数据库未初始化。"

        try:
            all_data = collection.get(limit=1000)
            tags_set: set[str] = set()
            for meta in (all_data.get("metadatas") or []):
                if meta:
                    tags_str = meta.get("tags", "[]")
                    try:
                        tags = json.loads(tags_str)
                        tags_set.update(tags)
                    except (json.JSONDecodeError, TypeError):
                        pass
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
