from __future__ import annotations

import mimetypes
import hashlib
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from fastapi import HTTPException
from fastapi.responses import FileResponse

from backend.atomic_io import atomic_write_bytes, file_mutation_locks
from backend.security.sensitive_files import is_protected_write_path, is_sensitive_file
from backend.documents.service import parse_document_preview

from .models import (
    WorkspaceDeleteResponse,
    WorkspaceFileResponse,
    WorkspacePathResponse,
    WorkspaceTreeEntry,
    WorkspaceTreeResponse,
)

WORKSPACE_IGNORED_NAMES = {
    ".git",
    ".idea",
    ".vscode",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "dist",
    "build",
}
WORKSPACE_IGNORED_SUFFIXES = {".pyc", ".pyo", ".pyd"}
WORKSPACE_MAX_FILE_BYTES = 2 * 1024 * 1024
WORKSPACE_MAX_PREVIEW_BYTES = 50 * 1024 * 1024


class WorkspaceService:
    def __init__(
        self,
        get_workspace_root: Callable[[], Path],
        max_file_bytes: int = WORKSPACE_MAX_FILE_BYTES,
    ) -> None:
        self._get_workspace_root = get_workspace_root
        self._max_file_bytes = max_file_bytes

    @staticmethod
    def _invalidate_derived_caches(*, file_tree_changed: bool) -> None:
        """Keep model-side workspace views coherent after API mutations.

        The HTTP workspace editor and the agent file tools share the same
        process, but they do not share an execution path.  The file watcher
        eventually catches most changes; invalidating synchronously here makes
        an immediate read/list/search after a UI save deterministic as well.
        Cache state is derived and must never turn a successful disk mutation
        into a failed API response.
        """
        try:
            from backend.tools.file_tools_common import invalidate_workspace_file_caches

            invalidate_workspace_file_caches(
                file_tree_changed=file_tree_changed,
                clear_file_state=True,
            )
        except Exception:
            # Cache invalidation is best-effort.  The filesystem write already
            # succeeded and the watcher remains a second-line invalidation.
            pass

    def list_tree(self, path: str) -> WorkspaceTreeResponse:
        root = self.workspace_root_path()
        if not root.exists() or not root.is_dir():
            raise HTTPException(status_code=404, detail=f"Workspace folder is missing: {root}")
        target = self.resolve_workspace_path(path)
        if not target.exists():
            raise HTTPException(status_code=404, detail=f"Path not found: {path}")
        if not target.is_dir():
            raise HTTPException(status_code=400, detail="Path must point to a directory.")

        # os.scandir caches is_dir/is_file from the directory entry itself, so
        # a full listing costs one dirent walk instead of two stat calls per
        # child (iterdir + is_dir + lstat). On Windows this halves the syscalls
        # for large workspaces, which dominates the /tree latency.
        entries: list[WorkspaceTreeEntry] = []
        try:
            scanned = sorted(
                os.scandir(target),
                key=lambda candidate: (
                    not candidate.is_dir(follow_symlinks=False),
                    candidate.name.lower(),
                ),
            )
            for item in scanned:
                if self.should_skip_scandir_entry(item):
                    continue
                stat = item.stat(follow_symlinks=False)
                is_dir = item.is_dir(follow_symlinks=False)
                entries.append(
                    WorkspaceTreeEntry(
                        name=item.name,
                        path=self.to_workspace_relative(
                            Path(item.path), follow_symlinks=False
                        ),
                        is_dir=is_dir,
                        size_bytes=None if is_dir else stat.st_size,
                        modified_at=self.iso_timestamp(stat.st_mtime),
                        has_children=is_dir and self.directory_has_visible_children(Path(item.path)),
                    )
                )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=f"Permission denied: {path}") from exc

        return WorkspaceTreeResponse(
            workspace_root=str(self.workspace_root_path()),
            requested_path=self.to_workspace_relative(target),
            entries=entries,
        )

    def read_file(self, path: str) -> WorkspaceFileResponse:
        self.ensure_not_sensitive_file(
            self.resolve_workspace_path(path, follow_final_symlink=False)
        )
        target = self.resolve_workspace_path(path)
        if not target.exists():
            raise HTTPException(status_code=404, detail=f"Path not found: {path}")
        if not target.is_file():
            raise HTTPException(status_code=400, detail="Path must point to a file.")
        self.ensure_not_sensitive_file(target)

        try:
            stat = target.stat()
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=f"Permission denied: {path}") from exc

        if stat.st_size > self._max_file_bytes:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"File is too large ({stat.st_size} bytes). "
                    f"Max supported size is {self._max_file_bytes} bytes."
                ),
            )

        try:
            content = target.read_bytes().decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=400, detail="Only UTF-8 text files are supported.") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=f"Permission denied: {path}") from exc

        return WorkspaceFileResponse(
            workspace_root=str(self.workspace_root_path()),
            path=self.to_workspace_relative(target),
            name=target.name,
            content=content,
            content_hash=self.content_hash(content),
            size_bytes=stat.st_size,
            modified_at=self.iso_timestamp(stat.st_mtime),
            language_hint=self.infer_language_hint(target),
        )

    def raw_file_response(self, path: str) -> FileResponse:
        self.ensure_not_sensitive_file(
            self.resolve_workspace_path(path, follow_final_symlink=False)
        )
        target = self.resolve_workspace_path(path)
        if not target.exists():
            raise HTTPException(status_code=404, detail=f"Path not found: {path}")
        if not target.is_file():
            raise HTTPException(status_code=400, detail="Path must point to a file.")
        self.ensure_not_sensitive_file(target)
        media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        ascii_name = target.name.encode("ascii", errors="ignore").decode("ascii").replace('"', "") or "file"
        disposition = f"inline; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(target.name)}"
        return FileResponse(target, media_type=media_type, headers={"Content-Disposition": disposition})

    def preview_file(self, path: str) -> dict[str, object]:
        """Build the same bounded preview payload used for uploaded files.

        This keeps generated workspace deliverables in the application preview
        surface as well; the raw endpoint remains the native image/PDF stream.
        """
        self.ensure_not_sensitive_file(
            self.resolve_workspace_path(path, follow_final_symlink=False)
        )
        target = self.resolve_workspace_path(path)
        if not target.exists():
            raise HTTPException(status_code=404, detail=f"Path not found: {path}")
        if not target.is_file():
            raise HTTPException(status_code=400, detail="Path must point to a file.")
        self.ensure_not_sensitive_file(target)

        try:
            stat = target.stat()
            if stat.st_size > WORKSPACE_MAX_PREVIEW_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=(
                        f"File is too large ({stat.st_size} bytes). "
                        "Max supported preview size is 50 MB."
                    ),
                )
            raw_content = target.read_bytes()
        except HTTPException:
            raise
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=f"Permission denied: {path}") from exc
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"Failed to read file: {exc}") from exc

        parsed = parse_document_preview(target.name, raw_content)
        content = str(parsed.get("full_text") or "")
        # Match uploaded-attachment preview bounds so a large workspace file
        # cannot flood the right rail or the clipboard.
        visible_content = content[: 2 * 1024 * 1024]
        media_type = str(parsed.get("media_type") or mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        kind = str(parsed.get("kind") or "document")
        return {
            "file_name": target.name,
            "path": self.to_workspace_relative(target),
            "media_type": media_type,
            "kind": kind,
            "size_bytes": int(stat.st_size),
            "summary": str(parsed.get("summary") or ""),
            "parse_error": str(parsed.get("parse_error") or ""),
            "content": visible_content,
            "content_chars": len(content),
            "truncated": len(visible_content) < len(content),
            "has_native": kind == "image" or media_type == "application/pdf",
        }

    def write_file(self, path: str, content: str) -> WorkspaceFileResponse:
        self.ensure_write_allowed(self.resolve_workspace_path(path, follow_final_symlink=False))
        target = self.resolve_workspace_path(path)
        self.ensure_write_allowed(target)
        try:
            with file_mutation_locks([target]):
                if target.exists() and target.is_dir():
                    raise HTTPException(status_code=400, detail="Cannot write content into a directory path.")
                if target.exists():
                    raise HTTPException(
                        status_code=409,
                        detail="File already exists. Use compare-write with the latest content hash.",
                    )
                atomic_write_bytes(target, content.encode("utf-8"), overwrite=False)
                stat = target.stat()
        except HTTPException:
            raise
        except FileExistsError as exc:
            raise HTTPException(
                status_code=409,
                detail="File was created concurrently. Use compare-write with the latest content hash.",
            ) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=f"Permission denied: {path}") from exc
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"Failed to write file: {exc}") from exc

        self._invalidate_derived_caches(file_tree_changed=True)

        return WorkspaceFileResponse(
            workspace_root=str(self.workspace_root_path()),
            path=self.to_workspace_relative(target),
            name=target.name,
            content=content,
            content_hash=self.content_hash(content),
            size_bytes=stat.st_size,
            modified_at=self.iso_timestamp(stat.st_mtime),
            language_hint=self.infer_language_hint(target),
        )

    def compare_and_write_file(self, path: str, expected_hash: str, content: str) -> WorkspaceFileResponse:
        self.ensure_write_allowed(self.resolve_workspace_path(path, follow_final_symlink=False))
        target = self.resolve_workspace_path(path)
        self.ensure_write_allowed(target)
        normalized_expected = (expected_hash or "").strip().lower()
        try:
            with file_mutation_locks([target]):
                if target.exists() and target.is_dir():
                    raise HTTPException(status_code=400, detail="Cannot write content into a directory path.")
                file_existed_before_write = target.exists()
                if target.exists():
                    raw = target.read_bytes()
                    try:
                        current_content = raw.decode("utf-8")
                    except UnicodeDecodeError as exc:
                        raise HTTPException(status_code=400, detail="Only UTF-8 text files are supported.") from exc
                    current_hash = self.content_hash(current_content)
                else:
                    current_hash = ""

                if current_hash != normalized_expected:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "message": "File has changed on disk.",
                            "path": self.to_workspace_relative(target),
                            "expected_hash": normalized_expected,
                            "actual_hash": current_hash,
                        },
                    )

                atomic_write_bytes(target, content.encode("utf-8"))
                stat = target.stat()
        except HTTPException:
            raise
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=f"Permission denied: {path}") from exc
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"Failed to write file: {exc}") from exc

        self._invalidate_derived_caches(file_tree_changed=not file_existed_before_write)

        return WorkspaceFileResponse(
            workspace_root=str(self.workspace_root_path()),
            path=self.to_workspace_relative(target),
            name=target.name,
            content=content,
            content_hash=self.content_hash(content),
            size_bytes=stat.st_size,
            modified_at=self.iso_timestamp(stat.st_mtime),
            language_hint=self.infer_language_hint(target),
        )

    def create_directory(self, path: str) -> WorkspacePathResponse:
        self.ensure_write_allowed(self.resolve_workspace_path(path, follow_final_symlink=False))
        target = self.resolve_workspace_path(path)
        try:
            with file_mutation_locks([target]):
                if target.exists():
                    raise HTTPException(status_code=409, detail=f"Path already exists: {path}")
                target.mkdir(parents=True, exist_ok=False)
        except HTTPException:
            raise
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=f"Permission denied: {path}") from exc
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"Failed to create directory: {exc}") from exc

        self._invalidate_derived_caches(file_tree_changed=True)

        return self.workspace_path_payload(target)

    def rename_path(self, path: str, new_path: str) -> WorkspacePathResponse:
        source = self.resolve_workspace_path(path, follow_final_symlink=False)
        destination = self.resolve_workspace_path(new_path, follow_final_symlink=False)

        self.ensure_write_allowed(source)
        self.ensure_write_allowed(destination)

        self.ensure_not_workspace_root(source)
        self.ensure_not_workspace_root(destination)

        try:
            with file_mutation_locks([source, destination]):
                if not source.exists() and not source.is_symlink():
                    raise HTTPException(status_code=404, detail=f"Path not found: {path}")
                if destination.exists() or destination.is_symlink():
                    raise HTTPException(status_code=409, detail=f"Target already exists: {new_path}")
                destination.parent.mkdir(parents=True, exist_ok=True)
                source.rename(destination)
        except HTTPException:
            raise
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=f"Permission denied: {path}") from exc
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"Failed to rename path: {exc}") from exc

        self._invalidate_derived_caches(file_tree_changed=True)

        return self.workspace_path_payload(destination)

    def delete_path(self, path: str, recursive: bool) -> WorkspaceDeleteResponse:
        target = self.resolve_workspace_path(path, follow_final_symlink=False)
        self.ensure_not_workspace_root(target)
        self.ensure_write_allowed(target)

        is_dir = False
        try:
            with file_mutation_locks([target]):
                if not target.exists() and not target.is_symlink():
                    raise HTTPException(status_code=404, detail=f"Path not found: {path}")

                relative_path = self.to_workspace_relative(target, follow_symlinks=False)
                is_dir = target.is_dir() and not target.is_symlink()
                if target.is_symlink():
                    target.unlink()
                elif is_dir:
                    if recursive:
                        shutil.rmtree(target)
                    else:
                        target.rmdir()
                else:
                    target.unlink()
        except HTTPException:
            raise
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=f"Permission denied: {path}") from exc
        except OSError as exc:
            if is_dir and not recursive:
                raise HTTPException(
                    status_code=409,
                    detail="Directory is not empty. Retry with recursive=true.",
                ) from exc
            raise HTTPException(status_code=500, detail=f"Failed to delete path: {exc}") from exc

        self._invalidate_derived_caches(file_tree_changed=True)

        return WorkspaceDeleteResponse(
            workspace_root=str(self.workspace_root_path()),
            path=relative_path,
            deleted=True,
            is_dir=is_dir,
        )

    def workspace_root_path(self) -> Path:
        return self._get_workspace_root().resolve()

    @staticmethod
    def normalize_workspace_relative(path_value: str) -> str:
        raw = (path_value or ".").strip().replace("\\", "/")
        return raw or "."

    def resolve_workspace_path(
        self,
        path_value: str,
        *,
        follow_final_symlink: bool = True,
    ) -> Path:
        root = self.workspace_root_path()
        relative = self.normalize_workspace_relative(path_value)
        lexical = root / relative
        if follow_final_symlink or lexical == root:
            candidate = lexical.resolve()
        else:
            candidate = lexical.parent.resolve() / lexical.name
        if candidate != root and root not in candidate.parents:
            raise HTTPException(status_code=400, detail="Path is outside workspace root.")
        return candidate

    def to_workspace_relative(self, path: Path, *, follow_symlinks: bool = True) -> str:
        root = self.workspace_root_path()
        resolved = path.resolve() if follow_symlinks else path.absolute()
        if resolved == root:
            return "."
        try:
            return resolved.relative_to(root).as_posix()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Path is outside workspace root.") from exc

    @staticmethod
    def should_skip_entry(path: Path) -> bool:
        name = path.name
        if name in WORKSPACE_IGNORED_NAMES:
            return True
        if name.startswith(".") and name not in {".env.example"}:
            return True
        if path.is_file() and path.suffix.lower() in WORKSPACE_IGNORED_SUFFIXES:
            return True
        return False

    @staticmethod
    def should_skip_scandir_entry(entry: os.DirEntry[str]) -> bool:
        """Name-first twin of :meth:`should_skip_entry` for scandir walks.

        Uses the DirEntry's cached ``is_file`` so the compiled-suffix rule does
        not cost an extra stat per child on Windows.
        """
        name = entry.name
        if name in WORKSPACE_IGNORED_NAMES:
            return True
        if name.startswith(".") and name not in {".env.example"}:
            return True
        try:
            if entry.is_file(follow_symlinks=False) and os.path.splitext(name)[1].lower() in WORKSPACE_IGNORED_SUFFIXES:
                return True
        except OSError:
            return False
        return False

    @staticmethod
    def iso_timestamp(epoch_seconds: float) -> str:
        return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).isoformat()

    def directory_has_visible_children(self, path: Path) -> bool:
        if path.is_symlink():
            return False
        try:
            # Early-exit scandir walk: stops at the first visible child and
            # reuses cached dirent flags instead of statting every entry.
            with os.scandir(path) as it:
                for entry in it:
                    if not self.should_skip_scandir_entry(entry):
                        return True
        except Exception:
            return False
        return False

    @staticmethod
    def infer_language_hint(path: Path) -> str:
        ext = path.suffix.lower()
        mapping = {
            ".py": "python",
            ".ts": "typescript",
            ".tsx": "typescriptreact",
            ".js": "javascript",
            ".jsx": "javascriptreact",
            ".json": "json",
            ".md": "markdown",
            ".css": "css",
            ".scss": "scss",
            ".html": "html",
            ".yml": "yaml",
            ".yaml": "yaml",
            ".toml": "toml",
            ".sh": "shellscript",
            ".ps1": "powershell",
            ".go": "go",
            ".rs": "rust",
            ".java": "java",
            ".c": "c",
            ".cpp": "cpp",
        }
        return mapping.get(ext, "plaintext")

    @staticmethod
    def content_hash(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def ensure_not_workspace_root(self, path: Path) -> None:
        if path.resolve() == self.workspace_root_path():
            raise HTTPException(status_code=400, detail="Operation on workspace root is not allowed.")

    @staticmethod
    def ensure_not_sensitive_file(path: Path, *, operation: str = "read") -> None:
        if is_sensitive_file(path):
            raise HTTPException(
                status_code=403,
                detail=f"Refusing to {operation} sensitive credential file.",
            )

    @staticmethod
    def ensure_write_allowed(path: Path) -> None:
        """Apply the same protected-path chokepoint as agent file tools."""
        WorkspaceService.ensure_not_sensitive_file(path, operation="modify")
        if is_protected_write_path(path):
            raise HTTPException(status_code=403, detail="Refusing to modify protected path.")

    def workspace_path_payload(self, path: Path) -> WorkspacePathResponse:
        is_symlink = path.is_symlink()
        stat = path.lstat() if is_symlink else path.stat()
        is_dir = path.is_dir() and not is_symlink
        return WorkspacePathResponse(
            workspace_root=str(self.workspace_root_path()),
            path=self.to_workspace_relative(path, follow_symlinks=not is_symlink),
            name=path.name,
            is_dir=is_dir,
            size_bytes=None if is_dir else stat.st_size,
            modified_at=self.iso_timestamp(stat.st_mtime),
        )
