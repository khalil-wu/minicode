"""
产品级工作区管理器

根据 newplan.md 第 9.1 节实现
工作区成为一级上下文对象，统一驱动文件树、Git 状态、RAG 检索等
"""

from pathlib import Path
from typing import Optional, List
import subprocess
from datetime import datetime, timezone

from .models import (
    WorkspaceMetadata,
    WorkspaceSnapshot,
    FileTreeNode,
    GitStatus,
)
from .service import WorkspaceService


class ProductWorkspaceManager:
    """产品级工作区管理器"""

    def __init__(self, workspace_service: WorkspaceService):
        self._service = workspace_service
        self._current_workspace: Optional[Path] = None
        self._recent_workspaces: List[Path] = []

    def get_workspace_metadata(self, path: Path) -> WorkspaceMetadata:
        """获取工作区元信息"""
        name = path.name
        project_type = self._detect_project_type(path)
        is_git_repo = (path / ".git").exists()
        is_worktree = False
        main_repo_path = None
        current_branch = None

        if is_git_repo:
            # 检查是否为 worktree
            git_dir = path / ".git"
            if git_dir.is_file():
                # worktree 的 .git 是文件而非目录
                is_worktree = True
                main_repo_path = self._get_main_repo_path(path)

            # 获取当前分支
            current_branch = self._get_current_branch(path)

        return WorkspaceMetadata(
            path=str(path),
            name=name,
            project_type=project_type,
            is_git_repo=is_git_repo,
            is_worktree=is_worktree,
            main_repo_path=main_repo_path,
            current_branch=current_branch,
            last_accessed=datetime.now(timezone.utc).isoformat(),
        )

    def get_workspace_snapshot(self, path: Path) -> WorkspaceSnapshot:
        """获取工作区快照（用于 WebSocket 推送）"""
        metadata = self.get_workspace_metadata(path)
        git_status = None
        file_count = 0
        total_size = 0

        if metadata.is_git_repo:
            git_status = self.get_git_status(path)

        # 统计文件数量和总大小
        try:
            for item in path.rglob("*"):
                if item.is_file() and not self._should_skip_path(item):
                    file_count += 1
                    try:
                        total_size += item.stat().st_size
                    except (OSError, PermissionError):
                        pass
        except (OSError, PermissionError):
            pass

        return WorkspaceSnapshot(
            metadata=metadata,
            git_status=git_status,
            file_count=file_count,
            total_size=total_size,
        )

    def get_git_status(self, path: Path) -> Optional[GitStatus]:
        """获取 Git 状态"""
        if not (path / ".git").exists():
            return None

        try:
            # 获取当前分支
            current_branch = self._get_current_branch(path)
            if not current_branch:
                return None

            # 获取 ahead/behind 信息
            ahead, behind = self._get_ahead_behind(path)

            # 获取文件状态
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=path,
                capture_output=True,
                text=True, encoding="utf-8",
                timeout=5,
            )

            staged = []
            modified = []
            untracked = []
            deleted = []

            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    if len(line) < 4:
                        continue
                    status = line[:2]
                    filepath = line[3:]

                    if status[0] in ("A", "M", "D", "R", "C"):
                        staged.append(filepath)
                    if status[1] == "M":
                        modified.append(filepath)
                    elif status[1] == "D":
                        deleted.append(filepath)
                    elif status == "??":
                        untracked.append(filepath)

            is_clean = not (staged or modified or untracked or deleted)

            return GitStatus(
                current_branch=current_branch,
                is_clean=is_clean,
                ahead=ahead,
                behind=behind,
                staged=staged,
                modified=modified,
                untracked=untracked,
                deleted=deleted,
            )

        except (subprocess.TimeoutExpired, subprocess.SubprocessError, FileNotFoundError):
            return None

    def build_file_tree(self, path: Path, max_depth: int = 3) -> FileTreeNode:
        """构建文件树（带 Git 状态）"""
        git_status_map = self._build_git_status_map(path)

        def build_node(current_path: Path, depth: int) -> FileTreeNode:
            stat = current_path.stat()
            relative_path = str(current_path.relative_to(path))
            git_status = git_status_map.get(relative_path)

            node = FileTreeNode(
                name=current_path.name,
                path=str(current_path),
                type="directory" if current_path.is_dir() else "file",
                size=None if current_path.is_dir() else stat.st_size,
                modified=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                git_status=git_status,
                children=[],
                is_expanded=depth < 2,
            )

            if current_path.is_dir() and depth < max_depth:
                try:
                    for child in sorted(current_path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
                        if not self._should_skip_path(child):
                            node.children.append(build_node(child, depth + 1))
                except (OSError, PermissionError):
                    pass

            return node

        return build_node(path, 0)

    def _detect_project_type(self, path: Path) -> Optional[str]:
        """检测项目类型"""
        if (path / "package.json").exists():
            return "node"
        elif (path / "pyproject.toml").exists() or (path / "setup.py").exists():
            return "python"
        elif (path / "Cargo.toml").exists():
            return "rust"
        elif (path / "go.mod").exists():
            return "go"
        elif (path / "pom.xml").exists() or (path / "build.gradle").exists():
            return "java"
        return None

    def _get_current_branch(self, path: Path) -> Optional[str]:
        """获取当前分支名"""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=path,
                capture_output=True,
                text=True, encoding="utf-8",
                timeout=3,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except (subprocess.TimeoutExpired, subprocess.SubprocessError, FileNotFoundError):
            pass
        return None

    def _get_ahead_behind(self, path: Path) -> tuple[int, int]:
        """获取 ahead/behind 数量"""
        try:
            result = subprocess.run(
                ["git", "rev-list", "--left-right", "--count", "HEAD...@{upstream}"],
                cwd=path,
                capture_output=True,
                text=True, encoding="utf-8",
                timeout=3,
            )
            if result.returncode == 0:
                parts = result.stdout.strip().split()
                if len(parts) == 2:
                    return int(parts[0]), int(parts[1])
        except (subprocess.TimeoutExpired, subprocess.SubprocessError, FileNotFoundError, ValueError):
            pass
        return 0, 0

    def _get_main_repo_path(self, path: Path) -> Optional[str]:
        """获取主仓库路径（如果是 worktree）"""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--git-common-dir"],
                cwd=path,
                capture_output=True,
                text=True, encoding="utf-8",
                timeout=3,
            )
            if result.returncode == 0:
                common_dir = Path(result.stdout.strip())
                if common_dir.is_absolute():
                    return str(common_dir.parent)
                else:
                    return str((path / common_dir).parent.resolve())
        except (subprocess.TimeoutExpired, subprocess.SubprocessError, FileNotFoundError):
            pass
        return None

    def _build_git_status_map(self, path: Path) -> dict[str, str]:
        """构建 Git 状态映射表"""
        status_map = {}
        try:
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=path,
                capture_output=True,
                text=True, encoding="utf-8",
                timeout=5,
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    if len(line) < 4:
                        continue
                    status = line[:2]
                    filepath = line[3:]

                    if status == "??":
                        status_map[filepath] = "untracked"
                    elif status[0] == "A":
                        status_map[filepath] = "added"
                    elif status[0] == "M" or status[1] == "M":
                        status_map[filepath] = "modified"
                    elif status[0] == "D" or status[1] == "D":
                        status_map[filepath] = "deleted"
                    elif status[0] == "R":
                        status_map[filepath] = "renamed"

        except (subprocess.TimeoutExpired, subprocess.SubprocessError, FileNotFoundError):
            pass

        return status_map

    def _should_skip_path(self, path: Path) -> bool:
        """判断是否应跳过路径"""
        skip_names = {".git", ".idea", ".vscode", ".venv", "venv", "node_modules", "__pycache__", "dist", "build"}
        skip_suffixes = {".pyc", ".pyo", ".pyd"}

        if path.name in skip_names:
            return True
        if path.suffix in skip_suffixes:
            return True
        return False
