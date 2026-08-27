"""
MiniCode file-backed memory workspace.

The generated read path is memory_summary.md -> MEMORY.md -> optional skills
and rollout summaries. User-requested updates are append-only notes under
extensions/ad_hoc/notes and are consolidated by the Phase 2 worker.
"""

from __future__ import annotations

import logging
import hashlib
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from filelock import FileLock, Timeout as FileLockTimeout

from backend.config import DATA_ROOT
from backend.atomic_io import atomic_write_text

logger = logging.getLogger(__name__)

MEMORY_DIR = DATA_ROOT / "memory"
DEFAULT_MEMORY_INDEX = ""


@dataclass(frozen=True)
class MemoryResetResult:
    files_removed: int
    directories_removed: int
    cleanup_pending: bool = False


class FileMemory:
    """
    Own the project-scoped memory workspace and its reset lock.
    """

    def __init__(self, memory_dir: Path | None = None) -> None:
        self._dir = memory_dir or MEMORY_DIR
        self._index_file = self._dir / "MEMORY.md"
        self._reset_lock = FileLock(self._lock_path_for(self._dir))
        with self._reset_lock.acquire(timeout=5.0):
            self._ensure_initialized()

    @staticmethod
    def _lock_path_for(memory_dir: Path) -> Path:
        """Use one reset domain for global and all project-scoped memory."""

        resolved = memory_dir.absolute()
        projects_root = (MEMORY_DIR / "projects").absolute()
        if resolved == MEMORY_DIR.absolute() or resolved.parent == projects_root:
            return DATA_ROOT / ".memory.reset.lock"
        return resolved.parent / f".{resolved.name}.reset.lock"

    @classmethod
    def for_workspace(cls, workspace_root: Path | str | None) -> "FileMemory":
        """Return the project-scoped memory store for a workspace.

        MiniCode keys auto-memory by the canonical project rather than by
        the desktop process.  Resolve the nearest Git root and use a stable,
        opaque directory key so two projects never share project memory.
        """
        return cls(cls.workspace_memory_dir(workspace_root))

    @classmethod
    def workspace_memory_dir(cls, workspace_root: Path | str | None) -> Path:
        """Resolve the stable memory root shared by all worktrees of a repo."""

        if not workspace_root:
            return MEMORY_DIR
        root = Path(workspace_root).expanduser().resolve()
        identity_path = root
        for candidate in (root, *root.parents):
            marker = candidate / ".git"
            if not marker.exists():
                continue
            identity_path = cls._git_common_identity(marker, candidate)
            break
        identity = str(identity_path)
        if os.name == "nt":
            identity = identity.casefold()
        project_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
        return MEMORY_DIR / "projects" / project_key

    @staticmethod
    def _git_common_identity(marker: Path, checkout_root: Path) -> Path:
        if marker.is_dir():
            return checkout_root.resolve()
        try:
            first_line = marker.read_text(encoding="utf-8").splitlines()[0].strip()
        except (OSError, UnicodeDecodeError, IndexError):
            return checkout_root.resolve()
        if not first_line.lower().startswith("gitdir:"):
            return checkout_root.resolve()
        raw_git_dir = first_line.split(":", 1)[1].strip()
        git_dir = Path(raw_git_dir)
        if not git_dir.is_absolute():
            git_dir = (checkout_root / git_dir).resolve()
        common_file = git_dir / "commondir"
        try:
            raw_common = common_file.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            raw_common = ""
        if raw_common:
            common = Path(raw_common)
            if not common.is_absolute():
                common = (git_dir / common).resolve()
            return common.parent.resolve() if common.name == ".git" else common
        if git_dir.parent.name == "worktrees":
            common = git_dir.parent.parent.resolve()
            return common.parent.resolve() if common.name == ".git" else common
        return checkout_root.resolve()

    @property
    def memory_dir(self) -> Path:
        return self._dir

    @property
    def reset_lock_path(self) -> Path:
        return Path(self._reset_lock.lock_file)

    @property
    def reset_lock(self) -> FileLock:
        """Shared lock object used by memory workers and destructive reset."""

        return self._reset_lock

    def _ensure_initialized(self) -> None:
        """确保记忆目录和索引文件存在。"""
        self._dir.mkdir(parents=True, exist_ok=True)

        if not self._index_file.exists():
            atomic_write_text(self._index_file, DEFAULT_MEMORY_INDEX)
            logger.info("Created empty MEMORY.md")

        from backend.memory.prompts import AD_HOC_INSTRUCTIONS

        extension_root = self._dir / "extensions" / "ad_hoc"
        extension_root.mkdir(parents=True, exist_ok=True)
        instructions_path = extension_root / "instructions.md"
        if not instructions_path.exists():
            atomic_write_text(instructions_path, AD_HOC_INSTRUCTIONS)

    def get_context(self) -> str:
        from backend.memory.prompts import build_memory_read_prompt

        summary_path = self._dir / "memory_summary.md"
        try:
            summary = summary_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return ""
        if summary.splitlines()[:1] != ["v1"]:
            return ""
        return build_memory_read_prompt(self._dir, summary)

    def read_file(self, filename: str) -> str | None:
        """
        读取具体记忆文件内容。

        Args:
            filename: 文件名（如 user_profile.md），不含路径

        Returns:
            文件内容，不存在返回 None
        """
        # 安全检查：防止路径遍历
        safe_name = Path(filename).name
        filepath = self._dir / safe_name

        if not filepath.exists():
            return None

        try:
            return filepath.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning("读取记忆文件 %s 失败: %s", filename, exc)
            return None

    def list_files(self) -> list[str]:
        if not self._dir.exists():
            return []
        return [
            path.name
            for path in sorted(self._dir.iterdir())
            if path.is_file() and path.suffix == ".md"
        ]

    def reset(self) -> MemoryResetResult:
        """Clear generated memory artifacts and restore the empty layout.

        MiniCode exposes this as ``memory/reset`` and deliberately preserves
        thread memory-mode settings. Conversation metadata is reset by the
        repository layer; this method owns only the file-backed memory root.
        The directory swap keeps readers on either the old complete tree or
        the new initialized tree instead of exposing a partially deleted one.
        """

        root = self._dir.absolute()
        if self._dir.is_symlink():
            raise ValueError("Refusing to reset a symlinked memory directory")
        if root == Path(root.anchor) or root == DATA_ROOT.absolute():
            raise ValueError(f"Refusing to reset unsafe memory directory: {root}")

        try:
            with self._reset_lock.acquire(timeout=5.0):
                files_removed, directories_removed = self._tree_counts(root)
                tombstone = root.with_name(
                    f".{root.name}.reset-{uuid.uuid4().hex}"
                )
                if root.exists():
                    root.replace(tombstone)
                try:
                    self._ensure_initialized()
                except Exception:
                    if root.exists():
                        shutil.rmtree(root, ignore_errors=True)
                    if tombstone.exists():
                        tombstone.replace(root)
                    raise

                cleanup_pending = False
                if tombstone.exists():
                    try:
                        shutil.rmtree(tombstone)
                    except OSError as exc:
                        cleanup_pending = True
                        logger.warning(
                            "记忆重置完成，但旧目录清理失败 %s: %s",
                            tombstone,
                            exc,
                        )
                return MemoryResetResult(
                    files_removed=files_removed,
                    directories_removed=directories_removed,
                    cleanup_pending=cleanup_pending,
                )
        except FileLockTimeout as exc:
            raise TimeoutError(
                f"Timed out waiting for memory reset lock: {self._reset_lock.lock_file}"
            ) from exc

    @staticmethod
    def _tree_counts(root: Path) -> tuple[int, int]:
        if not root.exists():
            return 0, 0
        files = 0
        directories = 0
        for _current, dir_names, file_names in os.walk(root, followlinks=False):
            directories += len(dir_names)
            files += len(file_names)
        return files, directories
