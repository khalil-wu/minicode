"""
Git 集成服务

根据 newplan.md 第 9.3 节实现
提供 Git 基础能力：分支显示、切换、状态、Diff、Log、提交
"""

from pathlib import Path
from typing import Optional, List
import subprocess
from dataclasses import dataclass


@dataclass
class GitBranch:
    """Git 分支信息"""
    name: str
    is_current: bool
    commit_hash: str
    commit_message: str


@dataclass
class GitCommit:
    """Git 提交信息"""
    hash: str
    short_hash: str
    author: str
    date: str
    message: str


@dataclass
class GitDiff:
    """Git Diff 信息"""
    file_path: str
    status: str  # "modified", "added", "deleted", "renamed"
    additions: int
    deletions: int
    patch: str


class GitService:
    """Git 服务"""

    def __init__(self, workspace_root: Path):
        self.workspace_root = workspace_root

    def is_git_repo(self) -> bool:
        """检查是否为 Git 仓库"""
        return (self.workspace_root / ".git").exists()

    def get_current_branch(self) -> Optional[str]:
        """获取当前分支名"""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=self.workspace_root,
                capture_output=True,
                text=True, encoding="utf-8",
                timeout=3,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except (subprocess.TimeoutExpired, subprocess.SubprocessError, FileNotFoundError):
            pass
        return None

    def list_branches(self) -> List[GitBranch]:
        """列出所有分支"""
        branches = []
        try:
            result = subprocess.run(
                ["git", "branch", "-v", "--no-abbrev"],
                cwd=self.workspace_root,
                capture_output=True,
                text=True, encoding="utf-8",
                timeout=5,
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    if not line.strip():
                        continue
                    is_current = line.startswith("*")
                    parts = line[2:].split(maxsplit=2)
                    if len(parts) >= 3:
                        branches.append(
                            GitBranch(
                                name=parts[0],
                                is_current=is_current,
                                commit_hash=parts[1],
                                commit_message=parts[2],
                            )
                        )
        except (subprocess.TimeoutExpired, subprocess.SubprocessError, FileNotFoundError):
            pass
        return branches

    def switch_branch(self, branch_name: str) -> bool:
        """切换分支"""
        try:
            result = subprocess.run(
                ["git", "checkout", branch_name],
                cwd=self.workspace_root,
                capture_output=True,
                text=True, encoding="utf-8",
                timeout=10,
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, subprocess.SubprocessError, FileNotFoundError):
            return False

    def get_status(self) -> dict:
        """获取 Git 状态"""
        status = {
            "branch": self.get_current_branch(),
            "staged": [],
            "modified": [],
            "untracked": [],
            "deleted": [],
        }

        try:
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=self.workspace_root,
                capture_output=True,
                text=True, encoding="utf-8",
                timeout=5,
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    if len(line) < 4:
                        continue
                    file_status = line[:2]
                    filepath = line[3:]

                    if file_status[0] in ("A", "M", "D", "R", "C"):
                        status["staged"].append(filepath)
                    if file_status[1] == "M":
                        status["modified"].append(filepath)
                    elif file_status[1] == "D":
                        status["deleted"].append(filepath)
                    elif file_status == "??":
                        status["untracked"].append(filepath)

        except (subprocess.TimeoutExpired, subprocess.SubprocessError, FileNotFoundError):
            pass

        return status

    def get_diff(self, filepath: Optional[str] = None, staged: bool = False) -> List[GitDiff]:
        """获取 Diff"""
        diffs = []
        try:
            cmd = ["git", "diff", "--numstat"]
            if staged:
                cmd.append("--staged")
            if filepath:
                cmd.append("--")
                cmd.append(filepath)

            result = subprocess.run(
                cmd,
                cwd=self.workspace_root,
                capture_output=True,
                text=True, encoding="utf-8",
                timeout=10,
            )

            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    parts = line.split(maxsplit=2)
                    if len(parts) == 3:
                        additions = int(parts[0]) if parts[0] != "-" else 0
                        deletions = int(parts[1]) if parts[1] != "-" else 0
                        file_path = parts[2]

                        # 获取详细 patch
                        patch_cmd = ["git", "diff"]
                        if staged:
                            patch_cmd.append("--staged")
                        patch_cmd.extend(["--", file_path])

                        patch_result = subprocess.run(
                            patch_cmd,
                            cwd=self.workspace_root,
                            capture_output=True,
                            text=True, encoding="utf-8",
                            timeout=5,
                        )

                        diffs.append(
                            GitDiff(
                                file_path=file_path,
                                status="modified",
                                additions=additions,
                                deletions=deletions,
                                patch=patch_result.stdout if patch_result.returncode == 0 else "",
                            )
                        )

        except (subprocess.TimeoutExpired, subprocess.SubprocessError, FileNotFoundError, ValueError):
            pass

        return diffs

    def get_log(self, max_count: int = 20) -> List[GitCommit]:
        """获取提交历史"""
        commits = []
        try:
            result = subprocess.run(
                [
                    "git",
                    "log",
                    f"--max-count={max_count}",
                    "--pretty=format:%H|%h|%an|%ai|%s",
                ],
                cwd=self.workspace_root,
                capture_output=True,
                text=True, encoding="utf-8",
                timeout=10,
            )

            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    parts = line.split("|", maxsplit=4)
                    if len(parts) == 5:
                        commits.append(
                            GitCommit(
                                hash=parts[0],
                                short_hash=parts[1],
                                author=parts[2],
                                date=parts[3],
                                message=parts[4],
                            )
                        )

        except (subprocess.TimeoutExpired, subprocess.SubprocessError, FileNotFoundError):
            pass

        return commits

    def commit(self, message: str, files: Optional[List[str]] = None) -> bool:
        """创建提交"""
        try:
            # Stage files
            if files:
                for file in files:
                    subprocess.run(
                        ["git", "add", file],
                        cwd=self.workspace_root,
                        capture_output=True,
                        timeout=5,
                    )

            # Commit
            result = subprocess.run(
                ["git", "commit", "-m", message],
                cwd=self.workspace_root,
                capture_output=True,
                text=True, encoding="utf-8",
                timeout=10,
            )
            return result.returncode == 0

        except (subprocess.TimeoutExpired, subprocess.SubprocessError, FileNotFoundError):
            return False
