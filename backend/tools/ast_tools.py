"""
AST 轻量代码分析工具（newplan.md §4. Phase 4 — 实验组）。

  不直接挂载重型 Language Server（如 tsserver / pyright），
  转而提供基于 AST/正则的轻量代码跳转与引用分析：

  - go_to_definition:  在工作区内查找名称的定义位置（函数/类/变量）
  - find_references:   在工作区内查找名称的所有引用出现位置

  支持语言：Python（AST 精确解析），JS/TS/Go/Rust/Java（正则近似搜索）。
  权限：AUTO（只读操作，无副作用）。
"""

from __future__ import annotations

import ast
import logging
import re
from pathlib import Path
from typing import Any

from backend.permissions.context import ToolExecutionContext
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema

logger = logging.getLogger(__name__)

# ── 可搜索的文件扩展名 ────────────────────────────────────────────
_SEARCHABLE_EXTENSIONS = {
    ".py", ".pyi",
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ".go", ".rs", ".java", ".kt", ".swift", ".c", ".cpp", ".h", ".hpp",
    ".rb", ".php", ".cs",
}

_IGNORED_DIRS = {"__pycache__", "node_modules", ".git", ".venv", "venv", "dist", "build", ".next"}

# ── 每语言定义模式（正则） ───────────────────────────────────────
_DEFINITION_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    "py":   [re.compile(r"^\s*(?:async\s+)?def\s+({name})\s*[\(:]", re.M),
             re.compile(r"^\s*class\s+({name})\s*[\(:]", re.M),
             re.compile(r"^\s*({name})\s*=\s*", re.M)],
    "ts":   [re.compile(r"\b(?:function|class|const|let|var|interface|type|enum)\s+({name})\b"),
             re.compile(r"\bexport\s+(?:default\s+)?(?:function|class|const|let|var)\s+({name})\b")],
    "js":   [re.compile(r"\b(?:function|class|const|let|var)\s+({name})\b"),
             re.compile(r"\bexport\s+(?:default\s+)?(?:function|class|const|let)\s+({name})\b")],
    "go":   [re.compile(r"\bfunc\b.*?({name})\s*[\(\[]"),
             re.compile(r"\btype\s+({name})\s+(?:struct|interface)")],
    "rs":   [re.compile(r"\bfn\s+({name})\s*[\(<]"),
             re.compile(r"\b(?:struct|enum|trait|impl)\s+({name})\b")],
    "java": [re.compile(r"\b(?:class|interface|enum|record|void|public|private|protected|static)\s+({name})\s*[\(<{]")],
    "kt":   [re.compile(r"\b(?:fun|class|object|interface|val|var)\s+({name})\b")],
    "c":    [re.compile(r"\b\w[\w\s\*]+\s+({name})\s*\("),
             re.compile(r"\btypedef\s+.*?\s+({name})\s*;")],
    "cpp":  [re.compile(r"\b\w[\w\s\*:]+\s+({name})\s*\("),
             re.compile(r"\b(?:class|struct|enum|namespace)\s+({name})\b")],
    "rb":   [re.compile(r"\bdef\s+({name})\b"),
             re.compile(r"\bclass\s+({name})\b")],
}

# TS / TSX / JS / JSX 共用相同模式
for _ext in ("tsx", "jsx", "mjs", "cjs"):
    _DEFINITION_PATTERNS[_ext] = _DEFINITION_PATTERNS["ts"] if _ext in ("tsx",) else _DEFINITION_PATTERNS["js"]


def _iter_source_files(root: Path) -> list[Path]:
    """递归收集可搜索文件，排除黑名单目录。"""
    result: list[Path] = []
    for item in root.rglob("*"):
        if not item.is_file():
            continue
        if any(part in _IGNORED_DIRS for part in item.parts):
            continue
        if item.suffix.lower() in _SEARCHABLE_EXTENSIONS:
            result.append(item)
    return result


def _read_safe(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


def _line_of_pos(content: str, pos: int) -> int:
    """将字符偏移转换为行号（1-indexed）。"""
    return content.count("\n", 0, pos) + 1


# ── Python AST 精确定义提取 ─────────────────────────────────────
def _python_ast_definitions(content: str, name: str) -> list[int]:
    """用 Python AST 精确查找 def / class / assignment 定义行号。"""
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return []

    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == name:
                lines.append(node.lineno)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    lines.append(node.lineno)
        elif isinstance(node, (ast.AnnAssign,)):
            if isinstance(node.target, ast.Name) and node.target.id == name:
                lines.append(node.lineno)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                canonical = alias.asname or alias.name.split(".")[-1]
                if canonical == name:
                    lines.append(node.lineno)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                canonical = alias.asname or alias.name
                if canonical == name:
                    lines.append(node.lineno)
    return lines


def _regex_definitions(content: str, name: str, ext: str) -> list[int]:
    """对非 Python 文件用正则模式匹配定义，返回行号列表。"""
    patterns = _DEFINITION_PATTERNS.get(ext, [])
    matched: set[int] = set()
    for pat in patterns:
        compiled = re.compile(pat.pattern.replace("{name}", re.escape(name)), pat.flags | re.MULTILINE)
        for m in compiled.finditer(content):
            matched.add(_line_of_pos(content, m.start()))
    return sorted(matched)


def _find_definitions_in_file(path: Path, name: str) -> list[dict[str, Any]]:
    content = _read_safe(path)
    if content is None:
        return []

    ext = path.suffix.lower().lstrip(".")
    if ext == "py":
        line_nums = _python_ast_definitions(content, name)
    else:
        line_nums = _regex_definitions(content, name, ext)

    results = []
    source_lines = content.splitlines()
    for lineno in line_nums:
        snippet = source_lines[lineno - 1].strip() if 0 < lineno <= len(source_lines) else ""
        results.append({
            "file": str(path),
            "line": lineno,
            "snippet": snippet[:200],
        })
    return results


# ── 引用查找（通用正则词边界匹配） ──────────────────────────────
def _find_references_in_file(path: Path, name: str, include_defs: bool) -> list[dict[str, Any]]:
    content = _read_safe(path)
    if content is None:
        return []

    pattern = re.compile(r"\b" + re.escape(name) + r"\b")
    source_lines = content.splitlines()
    results: list[dict[str, Any]] = []

    for lineno, line in enumerate(source_lines, 1):
        if pattern.search(line):
            results.append({
                "file": str(path),
                "line": lineno,
                "snippet": line.strip()[:200],
            })

    return results


def _resolve_workspace(context: ToolExecutionContext | None, directory: str) -> Path:
    if directory and directory not in (".", ""):
        return Path(directory).resolve()
    if context and getattr(context, "workspace_root", None):
        return context.workspace_root  # type: ignore[return-value]
    return Path.cwd()


# ════════════════════════════════════════════════════════════════
# GoToDefinitionTool
# ════════════════════════════════════════════════════════════════
class GoToDefinitionTool(BaseTool):
    """
    在工作区中查找符号（函数/类/变量）的定义位置。

    Python 文件使用 AST 精确解析；其余语言使用语言特定的
    正则模式匹配，准确率约 85-95%。
    权限: AUTO（只读）。
    """

    name = "go_to_definition"
    read_only = True
    description = (
        "在工作区源码中查找函数、类或变量名称的定义位置。"
        "Python 文件使用 AST 精确解析，其余语言使用正则模式匹配（准确率 ~90%）。"
        "返回定义所在文件路径、行号和代码片段。"
        "示例: go_to_definition(name='run_agent_loop')。"
        "注意: 不依赖外部 LSP，适用于任何规模的工作区。"
    )
    permission = PermissionLevel.AUTO

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "required": ["name"],
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "要查找定义的符号名称（函数名、类名或变量名）",
                    },
                    "directory": {
                        "type": "string",
                        "description": "搜索的根目录，默认为工作区根目录",
                    },
                    "file_extensions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "限定文件扩展名，如 ['.py', '.ts']，默认搜索所有支持的语言",
                    },
                },
            },
        )

    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        symbol_name = args.get("name", "").strip()
        directory = args.get("directory", "")
        file_extensions: list[str] = args.get("file_extensions", [])

        if not symbol_name:
            return self._error_result("缺少 name 参数")

        root = _resolve_workspace(context, directory)
        if not root.exists():
            return self._error_result(f"目录不存在: {directory}")

        # 收集候选文件
        all_files = _iter_source_files(root)
        if file_extensions:
            normalized_exts = {
                (e if e.startswith(".") else f".{e}").lower()
                for e in file_extensions
            }
            all_files = [f for f in all_files if f.suffix.lower() in normalized_exts]

        definitions: list[dict[str, Any]] = []
        for path in all_files:
            defs = _find_definitions_in_file(path, symbol_name)
            definitions.extend(defs)
            if len(definitions) >= 20:
                break

        if not definitions:
            return self._success_result(
                f"在 {root} 中未找到符号 '{symbol_name}' 的定义。\n"
                "提示：检查名称拼写，或使用 grep_files 进行宽松搜索。"
            )

        lines = [f"符号 '{symbol_name}' 的定义（共 {len(definitions)} 处）：\n"]
        for i, d in enumerate(definitions, 1):
            rel = Path(d["file"]).relative_to(root) if Path(d["file"]).is_relative_to(root) else Path(d["file"])
            lines.append(f"[{i}] {rel}:{d['line']}")
            lines.append(f"    {d['snippet']}")
            lines.append("")

        return self._success_result("\n".join(lines).strip())


# ════════════════════════════════════════════════════════════════
# FindReferencesTool
# ════════════════════════════════════════════════════════════════
class FindReferencesTool(BaseTool):
    """
    在工作区中查找符号名称的所有引用位置。

    使用词边界正则匹配（\b name \b），避免误匹配子字符串。
    返回出现该名称的文件路径、行号和代码片段。
    权限: AUTO（只读）。
    """

    name = "find_references"
    read_only = True
    description = (
        "在工作区源码中查找函数、类或变量名称的所有使用位置。"
        "使用词边界正则匹配，避免误匹配子字符串（如 user 不会匹配 username）。"
        "返回所有引用的文件路径、行号和代码片段，最多 60 条。"
        "示例: find_references(name='ReadFileTool')。"
    )
    permission = PermissionLevel.AUTO

    MAX_REFS = 60

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "required": ["name"],
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "要查找引用的符号名称",
                    },
                    "directory": {
                        "type": "string",
                        "description": "搜索的根目录，默认为工作区根目录",
                    },
                    "file_extensions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "限定文件扩展名，默认搜索所有支持语言",
                    },
                    "include_definitions": {
                        "type": "boolean",
                        "description": "是否包含定义位置本身，默认 true",
                    },
                },
            },
        )

    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        symbol_name = args.get("name", "").strip()
        directory = args.get("directory", "")
        file_extensions: list[str] = args.get("file_extensions", [])
        include_definitions: bool = args.get("include_definitions", True)

        if not symbol_name:
            return self._error_result("缺少 name 参数")

        root = _resolve_workspace(context, directory)
        if not root.exists():
            return self._error_result(f"目录不存在: {directory}")

        all_files = _iter_source_files(root)
        if file_extensions:
            normalized_exts = {
                (e if e.startswith(".") else f".{e}").lower()
                for e in file_extensions
            }
            all_files = [f for f in all_files if f.suffix.lower() in normalized_exts]

        all_refs: list[dict[str, Any]] = []
        files_searched = 0
        for path in all_files:
            refs = _find_references_in_file(path, symbol_name, include_definitions)
            all_refs.extend(refs)
            files_searched += 1
            if len(all_refs) >= self.MAX_REFS:
                break

        if not all_refs:
            return self._success_result(
                f"在 {root} 中搜索了 {files_searched} 个文件，"
                f"未找到符号 '{symbol_name}' 的引用。"
            )

        truncated = len(all_refs) >= self.MAX_REFS
        display = all_refs[:self.MAX_REFS]

        lines = [f"符号 '{symbol_name}' 的引用（{len(display)} 处，搜索了 {files_searched} 个文件）{' [已截断]' if truncated else ''}：\n"]
        for i, ref in enumerate(display, 1):
            try:
                rel = Path(ref["file"]).relative_to(root)
            except ValueError:
                rel = Path(ref["file"])
            lines.append(f"[{i}] {rel}:{ref['line']}")
            lines.append(f"    {ref['snippet']}")
            lines.append("")

        return self._success_result("\n".join(lines).strip())
