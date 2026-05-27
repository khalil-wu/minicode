"""
交互式终端系统（参考 Claude Code BashTool/PowerShellTool）。

提供：
  - TerminalSession: 持久化 shell 会话
  - TerminalSessionManager: 多会话管理
  - BackgroundCommandManager: 后台命令管理
"""

from backend.terminal.session import TerminalSession, TerminalSessionManager
from backend.terminal.manager import BackgroundCommand, BackgroundCommandManager

__all__ = [
    "TerminalSession",
    "TerminalSessionManager",
    "BackgroundCommand",
    "BackgroundCommandManager",
]
