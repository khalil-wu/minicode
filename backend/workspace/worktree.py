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
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from backend.runtime_env import sanitized_git_env

if TYPE_CHECKING:
    from backend.workspace.worktree_snapshots import (
        WorktreeSnapshotRecord,
        WorktreeSnapshotStore,
    )

logger = logging.getLogger(__name__)

WORKTREE_GIT_TIMEOUT_SECONDS = 120
WORKTREE_SNAPSHOT_MAX_PER_REPO = 50
WORKTREE_SNAPSHOT_MAX_AGE_DAYS = 30


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


@dataclass(frozen=True)
class WorktreeRemoval:
    """Outcome of a snapshot-guarded worktree removal."""

    removed: bool
    snapshot: "WorktreeSnapshotRecord | None" = None
    needs_force: bool = False
    error: Optional[str] = None


@dataclass(frozen=True)
class WorktreeRestore:
    """Outcome of restoring a worktree snapshot."""

    restored: bool
    path: Optional[Path] = None
    snapshot: "WorktreeSnapshotRecord | None" = None
    error: Optional[str] = None


class WorktreeManager:
    """
    Git Worktree 管理器。

    Worktree 允许在同一个仓库中同时检出多个分支，
    适用于并行开发、测试、代码审查等场景。
    """

    def __init__(self, repo_root: Path, *, snapshot_store: "WorktreeSnapshotStore | None" = None):
        """
        初始化 Worktree 管理器。

        Args:
            repo_root: Git 仓库根目录
            snapshot_store: 可选的快照存储(默认懒加载到 data/worktree-snapshots)
        """
        self.repo_root = repo_root.resolve()
        self._snapshot_store = snapshot_store

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
                env=sanitized_git_env(),
                capture_output=True,
                text=True, encoding="utf-8",
                check=True,
                timeout=WORKTREE_GIT_TIMEOUT_SECONDS,
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

        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
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
                env=sanitized_git_env(),
                capture_output=True,
                text=True, encoding="utf-8",
                check=True,
                timeout=WORKTREE_GIT_TIMEOUT_SECONDS,
            )

            logger.info(f"Created worktree at {path}")
            logger.debug(result.stdout)
            return True

        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
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
                env=sanitized_git_env(),
                capture_output=True,
                text=True, encoding="utf-8",
                check=True,
                timeout=WORKTREE_GIT_TIMEOUT_SECONDS,
            )

            logger.info(f"Removed worktree at {path}")
            logger.debug(result.stdout)
            return True

        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
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
                env=sanitized_git_env(),
                capture_output=True,
                text=True, encoding="utf-8",
                check=True,
                timeout=WORKTREE_GIT_TIMEOUT_SECONDS,
            )

            logger.info("Pruned worktrees")
            logger.debug(result.stdout)
            return True

        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            logger.error(f"Failed to prune worktrees: {e.stderr}")
            return False

    # ── 快照 / 恢复 ──────────────────────────────────────────────

    def has_local_changes(self, path: Path) -> bool:
        """worktree 是否有未提交改动(含 untracked)。出错时保守返回 True。

        注意:必须区分「成功且无输出」(干净)与「命令失败」(未知→保守),
        所以不复用 _capture_output(后者把空输出也当成 None)。
        """
        try:
            result = subprocess.run(
                ["git", "status", "--porcelain=v1"],
                cwd=Path(path),
                env=sanitized_git_env(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=True,
                timeout=WORKTREE_GIT_TIMEOUT_SECONDS,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            return True
        return bool(result.stdout.strip())

    def snapshot_worktree(
        self,
        path: Path,
        *,
        conversation_id: str = "",
        branch: str = "",
        label: str = "",
    ) -> "WorktreeSnapshotRecord | None":
        """删除前抓一份持久、完整(tracked + untracked)的 worktree 快照。

        用 ``git add -A`` + ``write-tree`` + ``commit-tree`` 把整个工作状态
        固化成一个 commit,再用 ``update-ref`` 锚到 ``refs/minicode/wt-snapshots/<id>``。
        该 ref 存于 common git dir,worktree 删除后仍在、且不会被 GC。失败返回 None。
        """
        wt = Path(path).resolve()
        if not wt.exists():
            logger.warning("Cannot snapshot missing worktree: %s", wt)
            return None

        snapshot_id = f"wtsnap_{uuid.uuid4().hex[:12]}"
        head = self._rev_parse(wt, "HEAD")

        # Build the snapshot tree with a temporary index. The real worktree
        # index (including the user's staging choices) must remain untouched.
        with tempfile.TemporaryDirectory(prefix="minicode-wt-snapshot-") as temp_dir:
            index_path = Path(temp_dir) / "index"
            env = sanitized_git_env()
            env["GIT_INDEX_FILE"] = str(index_path)

            def _snapshot_git(*args: str) -> subprocess.CompletedProcess[str] | None:
                try:
                    return subprocess.run(
                        ["git", *args],
                        cwd=wt,
                        env=env,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        check=True,
                        timeout=WORKTREE_GIT_TIMEOUT_SECONDS,
                    )
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
                    return None

            seeded = _snapshot_git("read-tree", head) if head else _snapshot_git("read-tree", "--empty")
            if seeded is None or _snapshot_git("add", "-A") is None:
                logger.error("Snapshot failed while building temporary index for %s", wt)
                return None
            tree_result = _snapshot_git("write-tree")
            tree = (tree_result.stdout or "").strip() if tree_result is not None else ""
            if not tree:
                logger.error("Snapshot failed at 'git write-tree' for %s", wt)
                return None

        commit_args = ["commit-tree", tree]
        if head:
            commit_args += ["-p", head]
        commit_args += ["-m", f"minicode worktree snapshot: {snapshot_id}"]
        snapshot_sha = self._capture_output(wt, *commit_args)
        if not snapshot_sha:
            logger.error("Snapshot failed at 'git commit-tree' for %s", wt)
            return None

        ref = f"refs/minicode/wt-snapshots/{snapshot_id}"
        if not self._git_ok(wt, "update-ref", ref, snapshot_sha):
            logger.error("Snapshot failed at 'git update-ref %s' for %s", ref, wt)
            return None

        from backend.workspace.worktree_snapshots import WorktreeSnapshotRecord

        record = WorktreeSnapshotRecord(
            id=snapshot_id,
            conversation_id=conversation_id,
            branch=branch or self._current_branch(wt),
            original_path=str(wt),
            main_repo_path=str(self.repo_root),
            head=head,
            snapshot_sha=snapshot_sha,
            snapshot_ref=ref,
            label=label,
        )
        logger.info("Captured worktree snapshot %s (%s) for %s", snapshot_id, snapshot_sha[:8], wt)
        saved = self._snapshots().save(record)
        self.prune_snapshots(exclude_ids={saved.id})
        return saved

    def restore_snapshot(
        self,
        snapshot_id: str,
        *,
        dest: Path | None = None,
    ) -> WorktreeRestore:
        """把快照恢复成一个 worktree(detached 在快照 commit 上)。

        默认恢复到原路径;若原路径已存在且非空,则改用 ``<name>-restored``。
        恢复出的 worktree 处于 detached HEAD,所有文件(tracked + 原 untracked)
        以快照 commit 的形式回来。
        """
        record = self._snapshots().get(snapshot_id)
        if record is None or not record.snapshot_sha:
            logger.warning("Snapshot not found or has no commit: %s", snapshot_id)
            return WorktreeRestore(restored=False, error=f"Snapshot '{snapshot_id}' was not found")
        try:
            if Path(record.main_repo_path).resolve() != self.repo_root:
                return WorktreeRestore(
                    restored=False,
                    snapshot=record,
                    error="Snapshot belongs to a different repository",
                )
        except OSError:
            return WorktreeRestore(restored=False, snapshot=record, error="Invalid snapshot repository")

        dest_path = Path(dest).resolve() if dest else Path(record.original_path).resolve()
        original_path = Path(record.original_path).resolve()
        safe_parent = original_path.parent
        if dest is not None and dest_path != original_path and safe_parent not in dest_path.parents:
            return WorktreeRestore(
                restored=False,
                snapshot=record,
                error="Restore destination is outside the original worktree parent",
            )
        if dest_path.exists() and any(dest_path.iterdir()):
            candidate = dest_path.parent / f"{dest_path.name}-restored"
            if candidate.exists():
                candidate = dest_path.parent / f"{dest_path.name}-restored-{record.id[-6:]}"
            dest_path = candidate

        if not self._git_ok(
            self.repo_root, "worktree", "add", "--detach", str(dest_path), record.snapshot_sha
        ):
            logger.error("Failed to restore snapshot %s to %s", snapshot_id, dest_path)
            return WorktreeRestore(restored=False, snapshot=record, error="git worktree add failed")

        logger.info("Restored worktree snapshot %s to %s", snapshot_id, dest_path)
        return WorktreeRestore(restored=True, path=dest_path, snapshot=record)

    def list_snapshots(
        self, conversation_id: str | None = None, *, limit: int = 100
    ) -> list["WorktreeSnapshotRecord"]:
        return self._snapshots().list(
            conversation_id,
            repo_root=self.repo_root,
            limit=limit,
        )

    def prune_snapshots(
        self,
        *,
        max_records: int = WORKTREE_SNAPSHOT_MAX_PER_REPO,
        max_age_days: int = WORKTREE_SNAPSHOT_MAX_AGE_DAYS,
        exclude_ids: set[str] | None = None,
    ) -> int:
        """Bound recoverable snapshots and remove both metadata and git refs."""
        excluded = exclude_ids or set()
        records = self._snapshots().list(repo_root=self.repo_root, limit=10_000)
        cutoff = datetime.now(UTC) - timedelta(days=max(1, max_age_days))
        removed = 0
        for index, record in enumerate(records):
            if record.id in excluded:
                continue
            try:
                created_at = datetime.fromisoformat(record.created_at)
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=UTC)
            except (TypeError, ValueError):
                created_at = datetime.min.replace(tzinfo=UTC)
            if index < max(1, max_records) and created_at >= cutoff:
                continue
            expected_ref = f"refs/minicode/wt-snapshots/{record.id}"
            if not self._git_ok(self.repo_root, "update-ref", "-d", expected_ref):
                logger.warning("Could not prune worktree snapshot ref %s", expected_ref)
                continue
            if self._snapshots().delete(record.id):
                removed += 1
        return removed

    def safe_remove_worktree(
        self,
        path: Path,
        *,
        force: bool = False,
        snapshot: bool = True,
        conversation_id: str = "",
        branch: str = "",
    ) -> WorktreeRemoval:
        """删除 worktree,脏工作区在销毁前先抓快照。

        - 干净 worktree:直接删除(已提交内容随分支保留,无需快照)。
        - 脏 worktree 且未 force:拒绝,``needs_force=True``。
        - 脏 worktree 且 force:先快照(``snapshot=True`` 时)再删除。
        """
        wt = Path(path).resolve()
        dirty = self.has_local_changes(wt)

        if dirty and not force:
            return WorktreeRemoval(
                removed=False,
                needs_force=True,
                error="Worktree has local changes; confirm force cleanup to remove it",
            )

        snap = None
        if dirty and snapshot:
            snap = self.snapshot_worktree(wt, conversation_id=conversation_id, branch=branch)
            if snap is None:
                return WorktreeRemoval(
                    removed=False,
                    error="Worktree snapshot failed; refusing destructive removal",
                )

        removed = self.remove_worktree(wt, force=force)
        if not removed:
            return WorktreeRemoval(removed=False, snapshot=snap, error="git worktree remove failed")
        return WorktreeRemoval(removed=True, snapshot=snap)

    def _snapshots(self) -> "WorktreeSnapshotStore":
        if self._snapshot_store is None:
            from backend.workspace.worktree_snapshots import WorktreeSnapshotStore

            self._snapshot_store = WorktreeSnapshotStore()
        return self._snapshot_store

    def _capture_output(self, cwd: Path, *args: str) -> str | None:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=cwd,
                env=sanitized_git_env(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=True,
                timeout=WORKTREE_GIT_TIMEOUT_SECONDS,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            return None
        return (result.stdout or "").strip() or None

    def _git_ok(self, cwd: Path, *args: str) -> bool:
        try:
            subprocess.run(
                ["git", *args],
                cwd=cwd,
                env=sanitized_git_env(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=True,
                timeout=WORKTREE_GIT_TIMEOUT_SECONDS,
            )
            return True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            return False

    def _rev_parse(self, cwd: Path, ref: str) -> str | None:
        return self._capture_output(cwd, "rev-parse", "--verify", ref)

    def _current_branch(self, cwd: Path) -> str:
        return self._capture_output(cwd, "branch", "--show-current") or ""

    def _is_git_repo(self) -> bool:
        """检查是否为 Git 仓库"""
        try:
            subprocess.run(
                ["git", "rev-parse", "--git-dir"],
                cwd=self.repo_root,
                env=sanitized_git_env(),
                capture_output=True,
                check=True,
                timeout=WORKTREE_GIT_TIMEOUT_SECONDS,
            )
            return True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
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
