"""
Git Worktree 管理器（参考 Claude Code 的 worktree 支持）。

特性：
- 创建/删除 worktree
- 列出所有 worktree
- 分支隔离
- 并行工作流
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class WorktreeInfo:
    """Worktree 信息"""
    path: Path
    branch: str
    commit: str
    is_bare: bool
    is_detached: bool


@dataclass
class WorktreeStatus:
    """Current workspace worktree summary."""

    is_worktree: bool
    current_path: Path
    main_repo_path: Optional[Path]
    current_branch: Optional[str]
    worktree_count: int
    worktrees: list[WorktreeInfo]


class WorktreeManager:
    """
    Git Worktree 管理器。

    Worktree 允许在同一个仓库中同时检出多个分支，
    适用于并行开发、测试、代码审查等场景。
    """

    def __init__(self, repo_root: Path):
        """
        初始化 Worktree 管理器。

        Args:
            repo_root: Git 仓库根目录
        """
        self.repo_root = repo_root.resolve()

        if not self._is_git_repo():
            raise ValueError(f"Not a git repository: {repo_root}")

        logger.info(f"Initialized worktree manager for {repo_root}")

    def list_worktrees(self) -> list[WorktreeInfo]:
        """
        列出所有 worktree。

        Returns:
            Worktree 信息列表
        """
        try:
            result = subprocess.run(
                ["git", "worktree", "list", "--porcelain"],
                cwd=self.repo_root,
                capture_output=True,
                text=True, encoding="utf-8",
                check=True,
            )

            worktrees: list[WorktreeInfo] = []
            current_worktree: dict[str, str] = {}

            for line in result.stdout.strip().split("\n"):
                if not line:
                    # 空行表示一个 worktree 结束
                    if current_worktree:
                        worktrees.append(self._parse_worktree_info(current_worktree))
                        current_worktree = {}
                    continue

                if line.startswith("worktree "):
                    current_worktree["path"] = line[9:]
                elif line.startswith("HEAD "):
                    current_worktree["commit"] = line[5:]
                elif line.startswith("branch "):
                    current_worktree["branch"] = line[7:]
                elif line == "bare":
                    current_worktree["bare"] = "true"
                elif line == "detached":
                    current_worktree["detached"] = "true"

            # 处理最后一个 worktree
            if current_worktree:
                worktrees.append(self._parse_worktree_info(current_worktree))

            return worktrees

        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to list worktrees: {e.stderr}")
            return []

    def create_worktree(
        self,
        path: Path,
        branch: Optional[str] = None,
        new_branch: bool = False,
        commit: Optional[str] = None,
    ) -> bool:
        """
        创建新的 worktree。

        Args:
            path: Worktree 路径
            branch: 分支名（如果为 None 则使用 commit）
            new_branch: 是否创建新分支
            commit: 基于的提交（默认为 HEAD）

        Returns:
            True 如果成功
        """
        path = path.resolve()

        if path.exists():
            logger.error(f"Path already exists: {path}")
            return False

        cmd = ["git", "worktree", "add"]

        if new_branch and branch:
            cmd.extend(["-b", branch])

        cmd.append(str(path))

        if branch and not new_branch:
            cmd.append(branch)
        elif commit:
            cmd.append(commit)

        try:
            result = subprocess.run(
                cmd,
                cwd=self.repo_root,
                capture_output=True,
                text=True, encoding="utf-8",
                check=True,
            )

            logger.info(f"Created worktree at {path}")
            logger.debug(result.stdout)
            return True

        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to create worktree: {e.stderr}")
            return False

    def remove_worktree(self, path: Path, force: bool = False) -> bool:
        """
        删除 worktree。

        Args:
            path: Worktree 路径
            force: 是否强制删除（即使有未提交的更改）

        Returns:
            True 如果成功
        """
        path = path.resolve()

        cmd = ["git", "worktree", "remove"]

        if force:
            cmd.append("--force")

        cmd.append(str(path))

        try:
            result = subprocess.run(
                cmd,
                cwd=self.repo_root,
                capture_output=True,
                text=True, encoding="utf-8",
                check=True,
            )

            logger.info(f"Removed worktree at {path}")
            logger.debug(result.stdout)
            return True

        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to remove worktree: {e.stderr}")
            return False

    def prune_worktrees(self) -> bool:
        """
        清理已删除的 worktree 记录。

        Returns:
            True 如果成功
        """
        try:
            result = subprocess.run(
                ["git", "worktree", "prune"],
                cwd=self.repo_root,
                capture_output=True,
                text=True, encoding="utf-8",
                check=True,
            )

            logger.info("Pruned worktrees")
            logger.debug(result.stdout)
            return True

        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to prune worktrees: {e.stderr}")
            return False

    def _is_git_repo(self) -> bool:
        """检查是否为 Git 仓库"""
        try:
            subprocess.run(
                ["git", "rev-parse", "--git-dir"],
                cwd=self.repo_root,
                capture_output=True,
                check=True,
            )
            return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False

    def _parse_worktree_info(self, data: dict[str, str]) -> WorktreeInfo:
        """解析 worktree 信息"""
        path = Path(data.get("path", ""))
        branch = data.get("branch", "")
        commit = data.get("commit", "")
        is_bare = data.get("bare") == "true"
        is_detached = data.get("detached") == "true"

        # 移除 refs/heads/ 前缀
        if branch.startswith("refs/heads/"):
            branch = branch[11:]

        return WorktreeInfo(
            path=path,
            branch=branch,
            commit=commit,
            is_bare=is_bare,
            is_detached=is_detached,
        )


# 全局管理器实例
_global_manager: Optional[WorktreeManager] = None


def get_global_worktree_manager(repo_root: Optional[Path] = None) -> Optional[WorktreeManager]:
    """
    获取全局 Worktree 管理器实例。

    Args:
        repo_root: Git 仓库根目录（首次调用时必须提供）

    Returns:
        管理器实例，如果不是 Git 仓库则返回 None
    """
    global _global_manager

    if _global_manager is None:
        if repo_root is None:
            repo_root = Path.cwd()

        try:
            _global_manager = WorktreeManager(repo_root)
        except ValueError:
            logger.debug(f"Not a git repository: {repo_root}")
            return None

    return _global_manager


def summarize_worktree_status(current_path: Path, worktrees: list[WorktreeInfo]) -> WorktreeStatus:
    """Summarize whether the current workspace is a linked worktree."""

    resolved_current_path = current_path.resolve()
    resolved_worktrees = [
        WorktreeInfo(
            path=worktree.path.resolve(),
            branch=worktree.branch,
            commit=worktree.commit,
            is_bare=worktree.is_bare,
            is_detached=worktree.is_detached,
        )
        for worktree in worktrees
    ]
    current_entry = next(
        (worktree for worktree in resolved_worktrees if worktree.path == resolved_current_path),
        None,
    )
    main_repo_entry = next(
        (worktree for worktree in resolved_worktrees if (worktree.path / ".git").is_dir()),
        resolved_worktrees[0] if resolved_worktrees else None,
    )
    main_repo_path = main_repo_entry.path if main_repo_entry else None

    return WorktreeStatus(
        is_worktree=bool(main_repo_path and main_repo_path != resolved_current_path),
        current_path=resolved_current_path,
        main_repo_path=main_repo_path,
        current_branch=current_entry.branch or None if current_entry else None,
        worktree_count=len(resolved_worktrees),
        worktrees=resolved_worktrees,
    )
