"""
Glob 工具 - 文件模式匹配（GlobFilesTool 的别名）。

该模块保留仅为向后兼容。实际实现已合并至 search_tools.GlobFilesTool。
GlobTool 是 GlobFilesTool 的别名，注册工具名为 "glob_files"。

支持功能（已合并自原独立实现）：
  - glob 模式匹配（**、*、?）
  - 结果缓存
  - 路径穿越防护
  - 隐藏目录 / node_modules / __pycache__ 自动跳过
"""

from backend.tools.search_tools import GlobFilesTool

# GlobTool 是 GlobFilesTool 的别名。
# 注册工具名为 "glob_files"，不再单独注册 "glob"。
GlobTool = GlobFilesTool

__all__ = ["GlobTool"]
