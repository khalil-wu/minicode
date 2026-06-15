"""
Grep 工具 - 代码内容搜索（GrepFilesTool 的别名）。

该模块保留仅为向后兼容。实际实现已合并至 search_tools.GrepFilesTool。
GrepTool 是 GrepFilesTool 的别名，注册工具名为 "grep_files"。

支持功能（已合并自原独立实现）：
  - ripgrep 后端（如已安装 rg，自动启用）
  - 正则表达式搜索
  - 文件类型 / glob 过滤
  - 上下文行显示（context 参数）
  - 结果缓存
  - 并行文件处理
"""

from backend.tools.search_tools import GrepFilesTool

# GrepTool 是 GrepFilesTool 的别名。
# 注册工具名为 "grep_files"，不再单独注册 "grep"。
GrepTool = GrepFilesTool

__all__ = ["GrepTool"]
