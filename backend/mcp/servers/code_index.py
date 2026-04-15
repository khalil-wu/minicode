"""
code-index MCP Server（DESIGN.md §六.2）。

传输：stdio
依赖：chromadb（向量存储），可选 tree-sitter（代码结构解析）

功能概述：
  代码库索引与语义搜索。能够索引项目的源代码文件，
  按函数/类边界智能分割后存入向量数据库，
  支持自然语言搜索代码片段和符号定位。

Tools:
  index(root_dir, extensions=None) → IndexStats
    递归扫描目录，分块后存入向量库
    默认只索引常见代码文件（.py .js .ts .tsx .go .rs .java 等）

  search_code(query, top_k=5, file_filter=None) → 代码片段列表
    自然语言搜索代码库

  find_symbol(name, symbol_type=None) → 符号位置列表
    搜索函数名/类名/变量名

Resources:
  code://stats → 索引统计（文件数、分块数）
  code://files → 已索引文件列表

运行方式：python -m backend.mcp.servers.code_index
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import sys
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
        "code-index",
        instructions=(
            "代码索引 MCP Server。索引代码库后，支持自然语言搜索和符号定位。"
        ),
    )
else:
    mcp = None


# ── 配置 ────────────────────────────────────────────────────

DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data" / "chroma"

# 默认索引的文件扩展名
DEFAULT_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx",
    ".go", ".rs", ".java", ".c", ".cpp", ".h",
    ".rb", ".php", ".swift", ".kt",
    ".css", ".scss", ".html", ".vue", ".svelte",
    ".yaml", ".yml", ".toml", ".json", ".md",
}

# 忽略的目录
IGNORE_DIRS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv",
    "dist", "build", ".next", ".cache", "data",
    ".mypy_cache", ".pytest_cache", ".tox",
}

# 分块大小
CHUNK_MAX_LINES = 60
CHUNK_OVERLAP_LINES = 5

_collection = None
_client = None
_indexed_files: dict[str, str] = {}  # {filepath: hash}


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
        name="codebase",
        metadata={"hnsw:space": "cosine"},
    )
    return _collection


# ── 代码分块器 ──────────────────────────────────────────────

def _chunk_code(content: str, file_path: str) -> list[dict[str, Any]]:
    """
    将代码文件分割为语义块。

    策略：
      1. 尝试按函数/类定义边界分割（正则匹配）
      2. 边界不足时 fallback 到固定行数 + 重叠

    Returns:
        [{"content": str, "start_line": int, "end_line": int, "symbol": str|None}, ...]
    """
    lines = content.split("\n")
    ext = Path(file_path).suffix.lower()
    chunks: list[dict[str, Any]] = []

    # 尝试按函数/类边界分割
    boundaries = _find_code_boundaries(lines, ext)

    if boundaries and len(boundaries) > 1:
        # 按边界分割
        for i, start in enumerate(boundaries):
            end = boundaries[i + 1] if i + 1 < len(boundaries) else len(lines)
            chunk_lines = lines[start:end]
            chunk_content = "\n".join(chunk_lines)

            # 提取符号名
            symbol = _extract_symbol_name(chunk_lines[0] if chunk_lines else "", ext)

            chunks.append({
                "content": chunk_content,
                "start_line": start + 1,
                "end_line": end,
                "symbol": symbol,
            })
    else:
        # Fallback：固定行数分割
        for start in range(0, len(lines), CHUNK_MAX_LINES - CHUNK_OVERLAP_LINES):
            end = min(start + CHUNK_MAX_LINES, len(lines))
            chunk_content = "\n".join(lines[start:end])
            chunks.append({
                "content": chunk_content,
                "start_line": start + 1,
                "end_line": end,
                "symbol": None,
            })
            if end >= len(lines):
                break

    return chunks


def _find_code_boundaries(lines: list[str], ext: str) -> list[int]:
    """
    找出代码的函数/类边界行号。

    根据语言使用不同的正则模式。
    """
    patterns: dict[str, list[str]] = {
        ".py": [
            r"^(class|def|async\s+def)\s+\w+",
        ],
        ".js": [
            r"^(function|class|const|let|var|export)\s+\w+",
            r"^(async\s+function|export\s+default|export\s+function)\s*",
        ],
        ".ts": [
            r"^(function|class|const|let|interface|type|export|async)\s+\w+",
        ],
        ".tsx": [
            r"^(function|class|const|let|interface|type|export|async)\s+\w+",
        ],
        ".go": [
            r"^(func|type)\s+",
        ],
        ".rs": [
            r"^(fn|struct|enum|impl|trait|pub\s+fn|pub\s+struct)\s+",
        ],
        ".java": [
            r"^\s*(public|private|protected|static)?\s*(class|interface|enum)\s+",
            r"^\s*(public|private|protected|static)?\s+\w+\s+\w+\s*\(",
        ],
    }

    # 选择合适的模式
    lang_patterns = patterns.get(ext, [r"^(function|class|def)\s+\w+"])

    boundaries: list[int] = [0]  # 文件始终从第 0 行开始
    for i, line in enumerate(lines):
        stripped = line.strip()
        for pattern in lang_patterns:
            if re.match(pattern, stripped):
                if i > 0 and i not in boundaries:
                    boundaries.append(i)
                break

    return boundaries


def _extract_symbol_name(line: str, ext: str) -> str | None:
    """从代码行提取符号名。"""
    line = line.strip()

    # Python
    m = re.match(r"(?:async\s+)?(?:def|class)\s+(\w+)", line)
    if m:
        return m.group(1)

    # JS/TS
    m = re.match(r"(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(\w+)", line)
    if m:
        return m.group(1)

    m = re.match(r"(?:export\s+)?(?:const|let|var|class|interface|type)\s+(\w+)", line)
    if m:
        return m.group(1)

    # Go
    m = re.match(r"func\s+(?:\([^)]+\)\s+)?(\w+)", line)
    if m:
        return m.group(1)

    # Rust
    m = re.match(r"(?:pub\s+)?(?:fn|struct|enum|trait|impl)\s+(\w+)", line)
    if m:
        return m.group(1)

    return None


def _file_hash(content: str) -> str:
    """文件内容 hash。"""
    return hashlib.md5(content.encode()).hexdigest()[:12]


# ── MCP Tools ──────────────────────────────────────────────

if HAS_MCP and mcp:

    @mcp.tool()
    def index(
        root_dir: str,
        extensions: list[str] | None = None,
    ) -> str:
        """
        索引指定目录的代码文件。

        递归扫描目录，将代码按函数/类边界分块后存入向量库。
        已索引的文件如果内容未变，会跳过。

        Args:
            root_dir: 目标目录的绝对路径
            extensions: 要索引的文件扩展名列表（如 [".py", ".js"]），留空则索引所有常见代码文件

        Returns:
            索引统计信息

        示例:
            index("/c/Desktop/MiniCode/backend")
            index("/c/Desktop/project", extensions=[".py", ".ts"])
        """
        collection = _get_collection()
        if collection is None:
            return "错误: 向量数据库未初始化。请确认已安装 chromadb。"

        root = Path(root_dir)
        if not root.exists():
            return f"错误: 目录不存在: {root_dir}"

        exts = set(extensions) if extensions else DEFAULT_EXTENSIONS

        # 扫描文件
        files_to_index: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(root):
            # 过滤忽略目录
            dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS]

            for fname in filenames:
                if Path(fname).suffix.lower() in exts:
                    files_to_index.append(Path(dirpath) / fname)

        if not files_to_index:
            return f"在 {root_dir} 中未找到匹配的代码文件。"

        # 索引
        total_chunks = 0
        new_files = 0
        skipped_files = 0

        for fpath in files_to_index:
            try:
                content = fpath.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

            # 增量：检查文件是否已变更
            fhash = _file_hash(content)
            fpath_str = str(fpath)
            if _indexed_files.get(fpath_str) == fhash:
                skipped_files += 1
                continue

            # 分块
            chunks = _chunk_code(content, fpath_str)

            # 存入向量库
            ids = []
            documents = []
            metadatas = []

            for j, chunk in enumerate(chunks):
                chunk_id = f"code_{hashlib.md5(f'{fpath_str}:{j}'.encode()).hexdigest()[:10]}"
                ids.append(chunk_id)
                documents.append(chunk["content"])
                metadatas.append({
                    "file_path": fpath_str,
                    "relative_path": str(fpath.relative_to(root)),
                    "start_line": chunk["start_line"],
                    "end_line": chunk["end_line"],
                    "symbol": chunk["symbol"] or "",
                    "language": fpath.suffix.lstrip("."),
                })

            if ids:
                try:
                    collection.upsert(
                        ids=ids,
                        documents=documents,
                        metadatas=metadatas,
                    )
                    total_chunks += len(ids)
                    new_files += 1
                    _indexed_files[fpath_str] = fhash
                except Exception as exc:
                    logger.error("索引文件 %s 失败: %s", fpath, exc)

        return (
            f"## 索引完成\n"
            f"- **扫描目录**: {root_dir}\n"
            f"- **新增索引**: {new_files} 个文件, {total_chunks} 个代码块\n"
            f"- **跳过（未变更）**: {skipped_files} 个文件\n"
            f"- **总已索引文件**: {len(_indexed_files)}"
        )


    @mcp.tool()
    def search_code(
        query: str,
        top_k: int = 5,
        file_filter: str | None = None,
    ) -> str:
        """
        自然语言搜索代码库。

        使用语义向量匹配，找到与查询最相关的代码片段。

        Args:
            query: 搜索查询（自然语言，如 "处理 WebSocket 消息的函数"）
            top_k: 返回结果数（1-20，默认 5）
            file_filter: 文件路径过滤（如 "agent/" 只搜索 agent 目录）

        Returns:
            匹配的代码片段列表

        示例:
            search_code("WebSocket 消息处理")
            search_code("权限检查", file_filter="permissions/")
        """
        collection = _get_collection()
        if collection is None:
            return "错误: 向量数据库未初始化。"

        if collection.count() == 0:
            return "代码库未索引。请先使用 index() 索引项目。"

        top_k = max(1, min(20, top_k))

        # 组装查询参数
        where = None
        if file_filter:
            where = {"relative_path": {"$contains": file_filter}}

        try:
            results = collection.query(
                query_texts=[query],
                n_results=top_k,
                where=where,
            )
        except Exception as exc:
            return f"错误: 搜索失败: {exc}"

        if not results["ids"] or not results["ids"][0]:
            return f"未找到与 \"{query}\" 相关的代码。"

        # 格式化
        lines = [f"搜索 \"{query}\" 的结果：\n"]

        ids = results["ids"][0]
        docs = results["documents"][0] if results["documents"] else []
        metas = results["metadatas"][0] if results["metadatas"] else []
        distances = results["distances"][0] if results.get("distances") else []

        for i, (cid, doc, meta) in enumerate(zip(ids, docs, metas)):
            score = 1 - (distances[i] if distances else 0.5)
            rel_path = meta.get("relative_path", "?") if meta else "?"
            start = meta.get("start_line", "?") if meta else "?"
            end = meta.get("end_line", "?") if meta else "?"
            symbol = meta.get("symbol", "") if meta else ""
            lang = meta.get("language", "") if meta else ""

            # 代码预览（限制行数）
            preview_lines = doc.split("\n")[:15] if doc else []
            preview = "\n".join(preview_lines)
            if len(doc.split("\n")) > 15:
                preview += "\n..."

            lines.append(f"### {i + 1}. {rel_path}:{start}-{end} (相关性: {score:.2f})")
            if symbol:
                lines.append(f"符号: `{symbol}`")
            lines.append(f"```{lang}")
            lines.append(preview)
            lines.append("```")
            lines.append("")

        return "\n".join(lines)


    @mcp.tool()
    def find_symbol(
        name: str,
        symbol_type: str | None = None,
    ) -> str:
        """
        搜索代码符号（函数名、类名等）。

        基于元数据精确匹配，不使用向量搜索。

        Args:
            name: 符号名称（支持部分匹配）
            symbol_type: 可选，符号类型过滤（目前通过文件内容推断）

        Returns:
            匹配的符号位置列表

        示例:
            find_symbol("run_agent_loop")
            find_symbol("BaseTool")
        """
        collection = _get_collection()
        if collection is None:
            return "错误: 向量数据库未初始化。"

        try:
            results = collection.get(
                where={"symbol": {"$contains": name}} if name else None,
                limit=20,
            )
        except Exception:
            # 如果元数据过滤失败，fallback 到向量搜索
            return search_code(f"function or class named {name}", top_k=5)

        if not results["ids"]:
            return f"未找到符号 '{name}'。尝试使用 search_code() 进行语义搜索。"

        lines = [f"符号 \"{name}\" 的定位：\n"]
        for mid, meta in zip(results["ids"], results["metadatas"] or []):
            if meta:
                rel_path = meta.get("relative_path", "?")
                start = meta.get("start_line", "?")
                symbol = meta.get("symbol", "?")
                lines.append(f"- `{symbol}` → {rel_path}:{start}")

        return "\n".join(lines)


    @mcp.resource("code://stats")
    def code_stats() -> str:
        """代码索引统计。"""
        collection = _get_collection()
        if collection is None:
            return "向量数据库未初始化。"
        return (
            f"代码索引统计：\n"
            f"- 已索引文件: {len(_indexed_files)}\n"
            f"- 代码块总数: {collection.count()}"
        )


    @mcp.resource("code://files")
    def indexed_files_list() -> str:
        """已索引文件列表。"""
        if not _indexed_files:
            return "暂无已索引的文件。"
        lines = ["已索引的文件：\n"]
        for fpath in sorted(_indexed_files.keys()):
            lines.append(f"- {fpath}")
        return "\n".join(lines)


# ── 入口 ────────────────────────────────────────────────────

def main() -> None:
    if not HAS_MCP or not mcp:
        print("错误: 需要安装 MCP SDK: pip install 'mcp[cli]'", file=sys.stderr)
        sys.exit(1)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
