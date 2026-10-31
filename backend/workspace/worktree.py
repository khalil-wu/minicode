"""
Git Worktree 管理器（MiniCode 实现）。

特性：
- 创建/删除 worktree
- 列出所有 worktree
- 分支隔离
- 并行工作流
"""

from __future__ import annotations

import logging
import os
import stat
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

from backend.runtime_env import sanitized_git_env
from backend.workspace.worktree_snapshot_git import IN_PROGRESS_GIT_STATE, RAW_ATTRIBUTES, index_entries, object_alternate, recovery_mode

if TYPE_CHECKING:
    from backend.workspace.worktree_snapshots import (
        WorktreeSnapshotRecord,
        WorktreeSnapshotStore,
    )

logger = logging.getLogger(__name__)

WORKTREE_GIT_TIMEOUT_SECONDS = 120
WORKTREE_SNAPSHOT_MAX_PER_REPO = 50
WORKTREE_SNAPSHOT_MAX_AGE_DAYS = 30
MINICODE_WORKTREE_RELATIVE_ROOT = Path(".minicode") / "worktrees"


class NotGitRepositoryError(ValueError):
    """The selected directory has no owning Git repository."""


def isolated_worktree_root(repo_root: Path) -> Path:
    """Return the host-local root selected in Desktop Worktrees settings."""
    from backend.services.worktree_settings_service import managed_worktree_root

    return managed_worktree_root()


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
    head: str = ""
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

    def __init__(self, repo_root: Path, *, snapshot_store: "WorktreeSnapshotStore | None" = None, git_runner: Callable[..., Any] | None = None, environment_runner=None, git_path_mapper=None, git_path_unmapper=None, worktree_access=None):
        """
        初始化 Worktree 管理器。

        Args:
            repo_root: Git 仓库根目录
            snapshot_store: 可选的快照存储(默认懒加载到 data/worktree-snapshots)
        """
        self.repo_root = repo_root.resolve()
        self._snapshot_store = snapshot_store
        self._git_runner = git_runner
        self._environment_runner = environment_runner
        self._git_path_mapper = git_path_mapper or str
        self._git_path_unmapper = git_path_unmapper or str
        self._worktree_access = worktree_access


        if not self._is_git_repo():
            raise NotGitRepositoryError(f"Not a git repository: {repo_root}")

        logger.info(f"Initialized worktree manager for {repo_root}")

    def _run_git(self, *args: Any, index_file: Path | None = None, **kwargs: Any) -> Any:
        if self._git_runner is not None:
            return self._git_runner(*args, index_file=index_file, **kwargs)
        return subprocess.run(*args, **kwargs)

    def list_worktrees(self) -> list[WorktreeInfo]:
        """
        列出所有 worktree。

        Returns:
            Worktree 信息列表
        """
        try:
            result = self._run_git(
                ["git", "worktree", "list", "--porcelain", "-z"],
                cwd=self.repo_root,
                env=sanitized_git_env(),
                capture_output=True,
                text=True, encoding="utf-8",
                check=True,
                timeout=WORKTREE_GIT_TIMEOUT_SECONDS,
            )

            worktrees: list[WorktreeInfo] = []
            current_worktree: dict[str, str] = {}

            for line in result.stdout.split("\0"):
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
            raise

    def create_worktree(
        self,
        path: Path,
        branch: Optional[str] = None,
        new_branch: bool = False,
        commit: Optional[str] = None,
        run_environment: bool = True,
        checkout: bool = True,
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
        if not checkout:
            cmd.append("--no-checkout")

        try:
            from backend.services.worktree_settings_service import refresh_worktree_upstream
            refresh_worktree_upstream(self.repo_root)
            result = self._run_git(
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
            from backend.services.worktree_settings_service import register_managed_worktree
            owner_root = self.list_worktrees()[0].path if (self.repo_root / ".git").is_file() else self.repo_root
            register_managed_worktree(path, owner_root, branch=branch or "")
            if run_environment:
                self.run_environment(path, "setup", source_root=self.repo_root)
            return True

        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            logger.error(f"Failed to create worktree: {e.stderr}")
            return False

    def run_environment(self, path: Path, phase: str, *, source_root: Path | None = None) -> str:
        from backend.workspace.local_environment_lifecycle import run_worktree_environment

        return run_worktree_environment(source_root or self.list_worktrees()[0].path, path, phase, runner=self._environment_runner)

    def remove_worktree(self, path: Path, force: bool = False, *, run_environment: bool = True) -> bool:
        """
        删除 worktree。

        Args:
            path: Worktree 路径
            force: 是否强制删除（即使有未提交的更改）

        Returns:
            True 如果成功
        """
        path = path.resolve()

        if run_environment:
            self.run_environment(path, "cleanup")

        cmd = ["git", "worktree", "remove"]

        if force:
            cmd.append("--force")

        cmd.append(str(path))

        try:
            result = self._run_git(
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
            from backend.services.worktree_settings_service import unregister_managed_worktree
            unregister_managed_worktree(path)
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
            result = self._run_git(
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

    def has_local_changes(self, path: Path, *, include_ignored: bool = True) -> bool:
        """Read worktree changes, including untracked and ignored files."""
        if self._git_runner is None:
            # Cleanup still needs its readonly execution authority. Model
            # callers supply an already captured runner below; app metadata's
            # host authority must not become a worktree-removal grant.
            from backend.services.workspace_service import worktree_has_local_changes

            return worktree_has_local_changes(Path(path), timeout=WORKTREE_GIT_TIMEOUT_SECONDS, include_ignored=include_ignored)
        result = self._run_git(
            ["git", "--no-optional-locks", "status", "--porcelain=v1", *(["--ignored"] if include_ignored else []), "--untracked-files=all"],
            cwd=Path(path).resolve(), env=sanitized_git_env(), capture_output=True,
            text=True, encoding="utf-8", check=True, timeout=WORKTREE_GIT_TIMEOUT_SECONDS,
        )
        return bool(result.stdout.strip())

    def snapshot_worktree(
        self,
        path: Path,
        *,
        conversation_id: str = "",
        branch: str = "",
        label: str = "",
        include_ignored: bool = True,
        _inventory_checked: bool = False,
    ) -> "WorktreeSnapshotRecord | None":
        """删除前抓一份持久、完整(tracked + untracked + ignored)的 worktree 快照。

        通过临时索引和未经 Git 过滤器处理的 blob 固化工作状态，再将提交
        锚到 ``refs/minicode/wt-snapshots/<id>``。
        该 ref 存于 common git dir,worktree 删除后仍在、且不会被 GC。失败返回 None。
        """
        wt = Path(path).resolve()
        if not wt.exists():
            logger.warning("Cannot snapshot missing worktree: %s", wt)
            return None
        if not _inventory_checked:
            try:
                if self._has_in_progress_git_operation(wt):
                    logger.error("Cannot snapshot a worktree with an in-progress Git operation: %s", wt)
                    return None
                if include_ignored and self._has_embedded_git(wt, write=False):
                    logger.error("Cannot snapshot a worktree containing an embedded Git repository: %s", wt)
                    return None
            except OSError:
                logger.exception("Cannot inspect the complete worktree: %s", wt)
                return None

        snapshot_id = f"wtsnap_{uuid.uuid4().hex[:12]}"
        head = self._rev_parse(wt, "HEAD")

        # Build the snapshot tree with a temporary index. The real worktree
        # index (including the user's staging choices) must remain untouched.
        with tempfile.TemporaryDirectory(prefix="minicode-wt-snapshot-") as temp_dir:
            index_path = Path(temp_dir) / "index"
            env = sanitized_git_env()
            env["GIT_INDEX_FILE"] = str(index_path)

            def _snapshot_git(*args: str, original_index: bool = False) -> subprocess.CompletedProcess[str] | None:
                try:
                    return self._run_git(
                        ["git", *args],
                        index_file=None if original_index else index_path,
                        cwd=wt,
                        env=sanitized_git_env() if original_index else env,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        check=True,
                        timeout=WORKTREE_GIT_TIMEOUT_SECONDS,
                    )
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
                    return None

            original = _snapshot_git("ls-files", "--stage", "-z", original_index=True)
            filemode = _snapshot_git("config", "--bool", "--default=true", "--get", "core.filemode", original_index=True)
            symlinks = _snapshot_git("config", "--bool", "--default=true", "--get", "core.symlinks", original_index=True)
            if original is None or filemode is None or symlinks is None:
                logger.error("Snapshot failed while inspecting the index for %s", wt)
                return None
            previous = {name: mode for mode, _sha, name in index_entries(original.stdout)}
            if "160000" in previous.values():
                logger.error("Cannot snapshot a worktree containing submodules: %s", wt)
                return None
            index_location = _snapshot_git("rev-parse", "--path-format=absolute", "--git-path", "index", "--shared-index-path", original_index=True)
            if index_location is None:
                return None
            index_paths = [path for path in index_location.stdout.splitlines() if path.strip()]
            index_blob = _snapshot_git("hash-object", "-w", "--no-filters", "--", *index_paths)
            if index_blob is None or _snapshot_git("read-tree", "--empty") is None:
                return None
            index_entries_to_save = [("index" if position == 0 else name.replace("\\", "/").rsplit("/", 1)[-1], sha)
                                     for position, (name, sha) in enumerate(zip(index_paths, index_blob.stdout.splitlines(), strict=True))]
            if _snapshot_git("update-index", "--add", *(value for name, sha in index_entries_to_save
                              for value in ("--cacheinfo", f"100644,{sha},{name}"))) is None:
                return None
            original_tree = _snapshot_git("write-tree")
            if original_tree is None:
                logger.error("Cannot save the worktree index: %s", wt)
                return None
            if _snapshot_git("read-tree", "--empty") is None or _snapshot_git("add", "-N", "-A", *(["--force"] if include_ignored else [])) is None:
                logger.error("Snapshot failed while building temporary index for %s", wt)
                return None
            staged = _snapshot_git("ls-files", "--stage", "-z")
            if staged is None:
                return None
            entries = index_entries(staged.stdout)
            if not include_ignored:
                deleted = _snapshot_git("ls-files", "--deleted", "-z", original_index=True)
                if deleted is None:
                    return None
                present = {name for _mode, _sha, name in entries}
                deleted_names = set(deleted.stdout.split("\0"))
                missing = [name for name in previous if name not in present and name not in deleted_names]
                for start in range(0, len(missing), 64):
                    if _snapshot_git("--literal-pathspecs", "add", "-N", "--force", "--", *missing[start:start + 64]) is None:
                        return None
                if missing:
                    staged = _snapshot_git("ls-files", "--stage", "-z")
                    if staged is None:
                        return None
                    entries = index_entries(staged.stdout)
            if any(mode == "160000" for mode, _sha, _name in entries):
                logger.error("Cannot snapshot a worktree containing an embedded Git repository: %s", wt)
                return None
            symbolic_links = [name for mode, _sha, name in entries if mode == "120000"]
            for start in range(0, len(symbolic_links), 64):
                if _snapshot_git("--literal-pathspecs", "add", "--force", "--", *symbolic_links[start:start + 64]) is None:
                    return None
            regular = [(mode, name) for mode, _sha, name in entries if mode in {"100644", "100755"}]
            for start in range(0, len(regular), 64):
                group = regular[start:start + 64]
                hashes = _snapshot_git("hash-object", "-w", "--no-filters", "--", *(name for _mode, name in group))
                if hashes is None:
                    return None
                entries_with_hash = [
                    (recovery_mode(mode, previous.get(name), filemode=filemode.stdout.strip() == "true",
                                   symlinks=symlinks.stdout.strip() == "true"), sha, name)
                    for (mode, name), sha in zip(group, hashes.stdout.splitlines(), strict=True)
                ]
                if _snapshot_git("update-index", "--add", *(value for mode, sha, name in entries_with_hash
                                  for value in ("--cacheinfo", f"{mode},{sha},{name}"))) is None:
                    return None
            tree_result = _snapshot_git("write-tree")
            tree = (tree_result.stdout or "").strip() if tree_result is not None else ""
            if not tree:
                logger.error("Snapshot failed at 'git write-tree' for %s", wt)
                return None

        index_args = ["commit-tree", original_tree.stdout.strip()]
        if head:
            index_args += ["-p", head]
        index_args += ["-m", f"minicode worktree index: {snapshot_id}"]
        index_sha = self._capture_output(wt, *index_args)
        if not index_sha:
            logger.error("Snapshot failed while saving the original index for %s", wt)
            return None

        commit_args = ["commit-tree", tree]
        if head:
            commit_args += ["-p", head]
        commit_args += ["-p", index_sha]
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
            index_sha=index_sha,
            snapshot_sha=snapshot_sha,
            snapshot_ref=ref,
            label=label,
            raw_worktree=True,
            symbolic_links=tuple(symbolic_links),
        )
        logger.info("Captured worktree snapshot %s (%s) for %s", snapshot_id, snapshot_sha[:8], wt)
        saved = self._snapshots().save(record)
        self.prune_snapshots(exclude_ids={saved.id})
        return saved

    def _materialize_raw_snapshot(self, dest: Path, snapshot: "WorktreeSnapshotRecord", *, restore_index: bool,
                                  selected_paths: tuple[str, ...] | None = None) -> bool:
        """Populate a new checkout without running repository content filters."""
        common = self._capture_output(self.repo_root, "rev-parse", "--path-format=absolute", "--git-common-dir")
        if not common:
            return False
        try:
            with tempfile.TemporaryDirectory(prefix=".mcr-", dir=dest) as scratch_name:
                scratch = Path(scratch_name)
                if not self._git_ok(dest, "-c", "core.longpaths=true", "init", "--bare", "--template=", str(scratch)):
                    return False
                (scratch / "info").mkdir()
                (scratch / "info" / "attributes").write_bytes(RAW_ATTRIBUTES)
                (scratch / "objects" / "info" / "alternates").write_bytes(
                    object_alternate(self._git_path_mapper(Path(common) / "objects"))
                )
                git_dir = f"--git-dir={self._git_path_mapper(scratch)}"
                work_tree = f"--work-tree={self._git_path_mapper(dest)}"
                command = ("-c", "core.symlinks=false", git_dir, work_tree)
                if not self._git_ok(dest, *command, "read-tree", snapshot.snapshot_sha):
                    return False
                if selected_paths is None:
                    if not self._git_ok(dest, *command, "checkout-index", "--all", "--force"):
                        return False
                    links = snapshot.symbolic_links
                else:
                    for start in range(0, len(selected_paths), 64):
                        if not self._git_ok(dest, *command, "checkout-index", "--force", "--", *selected_paths[start:start + 64]):
                            return False
                    selected = set(selected_paths)
                    links = tuple(name for name in snapshot.symbolic_links if name in selected)
                for start in range(0, len(links), 64):
                    if not self._git_ok(dest, "-c", "core.symlinks=true", git_dir, work_tree,
                                        "checkout-index", "--force", "--", *links[start:start + 64]):
                        return False
                if restore_index and snapshot.index_sha:
                    index_path = self._capture_output(dest, "rev-parse", "--path-format=absolute", "--git-path", "index")
                    if not index_path or not self._git_ok(dest, *command, "read-tree", snapshot.index_sha):
                        return False
                    prefix = index_path[:-len("index")]
                    return self._git_ok(dest, *command, "checkout-index", "--all", "--force", "--prefix=" + prefix)
                return self._git_ok(dest, "read-tree", "HEAD" if restore_index else snapshot.snapshot_sha)
        except (OSError, subprocess.SubprocessError):
            logger.exception("Could not materialize snapshot %s at %s", snapshot.id, dest)
            return False

    def ignored_worktree_files(self, path: Path) -> tuple[str, ...]:
        result = self._run_git(
            ["git", "--no-optional-locks", "ls-files", "--others", "--ignored", "--exclude-standard", "-z"],
            cwd=path, env=sanitized_git_env(), capture_output=True, text=True, encoding="utf-8",
            check=True, timeout=WORKTREE_GIT_TIMEOUT_SECONDS,
        )
        return tuple(name for name in result.stdout.split("\0") if name)

    def worktree_root(self, path: Path) -> Path:
        root = self._capture_output(path, "rev-parse", "--path-format=absolute", "--show-toplevel")
        if not root:
            raise OSError(f"Could not locate the worktree for {path}")
        return Path(self._git_path_unmapper(root)).resolve()

    def restore_handoff_bytes(self, dest: Path, snapshot: "WorktreeSnapshotRecord", ignored: tuple[str, ...]) -> bool:
        """Restore source working bytes and index after Git moves its changes."""
        try:
            tree = self._run_git(
                ["git", "--no-optional-locks", "ls-tree", "-r", "--name-only", "-z", snapshot.snapshot_sha],
                cwd=dest, env=sanitized_git_env(), capture_output=True, text=True, encoding="utf-8",
                check=True, timeout=WORKTREE_GIT_TIMEOUT_SECONDS,
            )
            excluded = set(ignored)
            selected = tuple(name for name in tree.stdout.split("\0") if name and name not in excluded)
            return self._materialize_raw_snapshot(dest, snapshot, restore_index=True, selected_paths=selected)
        except (OSError, subprocess.SubprocessError):
            logger.exception("Could not restore handoff bytes from %s at %s", snapshot.id, dest)
            return False

    def restore_snapshot(
        self,
        snapshot_id: str,
        *,
        dest: Path | None = None,
    ) -> WorktreeRestore:
        """把快照恢复成一个 worktree(detached 在快照 commit 上)。

        默认恢复到原路径;若原路径已存在且非空,则改用 ``<name>-restored``。
        恢复出的 worktree 处于 detached HEAD,所有文件(tracked + 原 untracked/ignored)
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
        if dest_path.exists() and (not dest_path.is_dir() or any(dest_path.iterdir())):
            candidate = dest_path.parent / f"{dest_path.name}-restored"
            if candidate.exists():
                candidate = dest_path.parent / f"{dest_path.name}-restored-{record.id[-6:]}"
            dest_path = candidate

        raw = getattr(record, "raw_worktree", False)
        args = ("worktree", "add", "--detach", str(dest_path), record.snapshot_sha)
        if raw:
            args += ("--no-checkout",)
        if not self._git_ok(self.repo_root, *args):
            logger.error("Failed to restore snapshot %s to %s", snapshot_id, dest_path)
            return WorktreeRestore(restored=False, snapshot=record, error="git worktree add failed")

        if raw and not self._materialize_raw_snapshot(dest_path, record, restore_index=False):
            return WorktreeRestore(restored=False, path=dest_path, snapshot=record,
                                   error=f"Could not materialize the recovery snapshot; partial worktree retained at {dest_path}")

        logger.info("Restored worktree snapshot %s to %s", snapshot_id, dest_path)
        return WorktreeRestore(restored=True, path=dest_path, snapshot=record)

    def restore_removed_worktree(
        self,
        path: Path,
        *,
        branch: str,
        expected_head: str = "",
        snapshot_id: str = "",
    ) -> WorktreeRestore:
        """Recreate a removed branch worktree and reapply its recovery snapshot.

        Cleanup commits the Git removal before it can persist the conversation
        binding.  If that metadata commit fails, the original branch-bound
        worktree must be recreated at the same path.  A dirty worktree's
        snapshot is materialized into the recreated checkout while HEAD stays
        on the original branch, so the conversation binding and Git state do
        not disagree after compensation.
        """

        dest_path = Path(path).resolve()
        clean_branch = str(branch or "").strip()
        clean_expected_head = str(expected_head or "").strip()
        clean_snapshot_id = str(snapshot_id or "").strip()
        if not clean_branch:
            return WorktreeRestore(
                restored=False,
                error="The removed worktree branch could not be identified",
            )
        if dest_path.exists() and (not dest_path.is_dir() or any(dest_path.iterdir())):
            return WorktreeRestore(
                restored=False,
                error="The original worktree path is no longer empty",
            )

        snapshot = None
        if clean_snapshot_id:
            snapshot = self._snapshots().get(clean_snapshot_id)
            if snapshot is None or not snapshot.snapshot_sha:
                return WorktreeRestore(
                    restored=False,
                    error=f"Snapshot '{clean_snapshot_id}' was not found",
                )
            try:
                snapshot_repo = Path(snapshot.main_repo_path).resolve()
                snapshot_path = Path(snapshot.original_path).resolve()
            except OSError:
                return WorktreeRestore(
                    restored=False,
                    snapshot=snapshot,
                    error="The recovery snapshot contains invalid paths",
                )
            if snapshot_repo != self.repo_root:
                return WorktreeRestore(
                    restored=False,
                    snapshot=snapshot,
                    error="Snapshot belongs to a different repository",
                )
            if snapshot_path != dest_path:
                return WorktreeRestore(
                    restored=False,
                    snapshot=snapshot,
                    error="Snapshot belongs to a different worktree path",
                )
            if snapshot.branch and snapshot.branch != clean_branch:
                return WorktreeRestore(
                    restored=False,
                    snapshot=snapshot,
                    error="Snapshot belongs to a different worktree branch",
                )
            if snapshot.head:
                if clean_expected_head and snapshot.head != clean_expected_head:
                    return WorktreeRestore(
                        restored=False,
                        snapshot=snapshot,
                        error="Snapshot HEAD does not match the removed worktree",
                    )
                clean_expected_head = snapshot.head

        branch_head = self._rev_parse(self.repo_root, clean_branch) or ""
        if not branch_head:
            return WorktreeRestore(
                restored=False,
                snapshot=snapshot,
                error=f"Branch '{clean_branch}' no longer exists",
            )
        if clean_expected_head and branch_head != clean_expected_head:
            return WorktreeRestore(
                restored=False,
                snapshot=snapshot,
                error="The worktree branch moved after cleanup; refusing to rewrite it",
            )

        if not self.create_worktree(
            dest_path,
            branch=clean_branch,
            new_branch=False,
            run_environment=False,
            checkout=not (snapshot is not None and snapshot.raw_worktree),
        ):
            return WorktreeRestore(
                restored=False,
                snapshot=snapshot,
                error="git worktree add failed",
            )

        if snapshot is not None:
            if snapshot.raw_worktree:
                materialized = self._materialize_raw_snapshot(dest_path, snapshot, restore_index=True)
                unstaged = materialized
            else:
                materialized = self._git_ok(dest_path, "read-tree", "--reset", "-u", snapshot.snapshot_sha)
                unstaged = materialized and self._git_ok(dest_path, "reset", "--mixed", "HEAD")
            if not materialized or not unstaged:
                return WorktreeRestore(
                    restored=False,
                    path=dest_path,
                    snapshot=snapshot,
                    error=f"Could not materialize the recovery snapshot; partial worktree retained at {dest_path}",
                )

        logger.info("Recreated removed worktree at %s", dest_path)
        return WorktreeRestore(
            restored=True,
            path=dest_path,
            snapshot=snapshot,
        )

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
        head = self._rev_parse(wt, "HEAD") or ""
        from backend.sandbox.runner import SandboxUnavailableError

        try:
            dirty = self.has_local_changes(wt)
        except (OSError, subprocess.SubprocessError, SandboxUnavailableError) as exc:
            return WorktreeRemoval(removed=False, head=head, error=f"Could not inspect worktree: {exc}")

        if dirty and not force:
            return WorktreeRemoval(
                removed=False,
                head=head,
                needs_force=True,
                error="Worktree has local changes; confirm force cleanup to remove it",
            )

        try:
            if self._has_in_progress_git_operation(wt):
                return WorktreeRemoval(removed=False, head=head,
                    error="Worktrees with an in-progress Git operation cannot be removed")
            if self._has_embedded_git(wt, write=True):
                return WorktreeRemoval(removed=False, head=head,
                    error="Worktrees containing initialized submodules or embedded Git repositories cannot be removed")
        except OSError as exc:
            return WorktreeRemoval(removed=False, head=head, error=f"Could not inspect the complete worktree: {exc}")

        snap = None
        if dirty and snapshot:
            snap = self.snapshot_worktree(wt, conversation_id=conversation_id, branch=branch, _inventory_checked=True)
            if snap is None:
                return WorktreeRemoval(
                    removed=False,
                    head=head,
                    error="Worktree snapshot failed; refusing destructive removal",
                )

        from backend.workspace.local_environment_lifecycle import EnvironmentScriptError
        try:
            removed = self.remove_worktree(wt, force=force)
        except (EnvironmentScriptError, PermissionError, ValueError) as error:
            return WorktreeRemoval(removed=False, snapshot=snap, head=head, error=str(error))
        if not removed:
            return WorktreeRemoval(
                removed=False,
                snapshot=snap,
                head=head,
                error="git worktree remove failed",
            )
        return WorktreeRemoval(removed=True, snapshot=snap, head=head)

    def _has_in_progress_git_operation(self, root: Path) -> bool:
        git_dir = self._capture_output(root, "rev-parse", "--path-format=absolute", "--git-dir")
        if not git_dir:
            raise OSError("Could not locate worktree Git metadata")
        metadata = Path(self._git_path_unmapper(git_dir))
        if self._worktree_access is not None:
            self._worktree_access(metadata, False)
        with os.scandir(metadata) as entries:
            return any(entry.name in IN_PROGRESS_GIT_STATE for entry in entries)

    def _has_embedded_git(self, root: Path, *, write: bool) -> bool:
        pending = [root]
        while pending:
            directory = pending.pop()
            if self._worktree_access is not None:
                self._worktree_access(directory, write)
            with os.scandir(directory) as entries:
                for entry in entries:
                    if entry.name.lower() == ".git":
                        if directory != root:
                            return True
                        continue
                    path = Path(entry.path)
                    if self._worktree_access is not None:
                        self._worktree_access(path, write)
                    if entry.is_dir(follow_symlinks=False):
                        junction = (os.name == "nt" and entry.stat(follow_symlinks=False).st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
                        if not junction:
                            pending.append(path)
        return False

    def _snapshots(self) -> "WorktreeSnapshotStore":
        if self._snapshot_store is None:
            from backend.workspace.worktree_snapshots import WorktreeSnapshotStore

            self._snapshot_store = WorktreeSnapshotStore()
        return self._snapshot_store

    def _capture_output(self, cwd: Path, *args: str) -> str | None:
        try:
            result = self._run_git(
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
            self._run_git(
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
            self._run_git(
                ["git", "rev-parse", "--git-dir"],
                cwd=self.repo_root,
                env=sanitized_git_env(),
                capture_output=True,
                check=True,
                timeout=WORKTREE_GIT_TIMEOUT_SECONDS,
            )
            return True
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else str(exc.stderr or "")
            if "not a git repository" in stderr.lower():
                return False
            raise

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
        except NotGitRepositoryError:
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
    current_entry = max(
        (worktree for worktree in resolved_worktrees if resolved_current_path.is_relative_to(worktree.path)),
        key=lambda worktree: len(worktree.path.parts),
        default=None,
    )
    main_repo_entry = next(
        (worktree for worktree in resolved_worktrees if (worktree.path / ".git").is_dir()),
        resolved_worktrees[0] if resolved_worktrees else None,
    )
    main_repo_path = main_repo_entry.path if main_repo_entry else None

    return WorktreeStatus(
        is_worktree=bool(current_entry and main_repo_path and current_entry.path != main_repo_path),
        current_path=current_entry.path if current_entry else resolved_current_path,
        main_repo_path=main_repo_path,
        current_branch=current_entry.branch or None if current_entry else None,
        worktree_count=len(resolved_worktrees),
        worktrees=resolved_worktrees,
    )
