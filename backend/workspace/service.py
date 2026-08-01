from __future__ import annotations

import shutil
import mimetypes
import hashlib
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from fastapi import HTTPException
from fastapi.responses import FileResponse

from backend.atomic_io import atomic_write_bytes
from backend.security.sensitive_files import is_protected_write_path, is_sensitive_file

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


class WorkspaceService:
    def __init__(
        self,
        get_workspace_root: Callable[[], Path],
        max_file_bytes: int = WORKSPACE_MAX_FILE_BYTES,
    ) -> None:
        self._get_workspace_root = get_workspace_root
        self._max_file_bytes = max_file_bytes
        self._write_lock = threading.Lock()

    def list_tree(self, path: str) -> WorkspaceTreeResponse:
        root = self.workspace_root_path()
        if not root.exists() or not root.is_dir():
            raise HTTPException(status_code=404, detail=f"Workspace folder is missing: {root}")
        target = self.resolve_workspace_path(path)
        if not target.exists():
            raise HTTPException(status_code=404, detail=f"Path not found: {path}")
        if not target.is_dir():
            raise HTTPException(status_code=400, detail="Path must point to a directory.")

        entries: list[WorkspaceTreeEntry] = []
        try:
            for item in sorted(
                target.iterdir(),
                key=lambda candidate: (
                    not (candidate.is_dir() and not candidate.is_symlink()),
                    candidate.name.lower(),
                ),
            ):
                if self.should_skip_entry(item):
                    continue
                stat = item.lstat()
                is_dir = item.is_dir() and not item.is_symlink()
                entries.append(
                    WorkspaceTreeEntry(
                        name=item.name,
                        path=self.to_workspace_relative(item, follow_symlinks=False),
                        is_dir=is_dir,
                        size_bytes=None if is_dir else stat.st_size,
                        modified_at=self.iso_timestamp(stat.st_mtime),
                        has_children=is_dir and self.directory_has_visible_children(item),
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

    def write_file(self, path: str, content: str) -> WorkspaceFileResponse:
        self.ensure_write_allowed(self.resolve_workspace_path(path, follow_final_symlink=False))
        target = self.resolve_workspace_path(path)
        self.ensure_write_allowed(target)
        if target.exists() and target.is_dir():
            raise HTTPException(status_code=400, detail="Cannot write content into a directory path.")

        try:
            atomic_write_bytes(target, content.encode("utf-8"))
            stat = target.stat()
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=f"Permission denied: {path}") from exc
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"Failed to write file: {exc}") from exc

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
        if target.exists() and target.is_dir():
            raise HTTPException(status_code=400, detail="Cannot write content into a directory path.")

        normalized_expected = (expected_hash or "").strip().lower()
        try:
            with self._write_lock:
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
        if target.exists():
            raise HTTPException(status_code=409, detail=f"Path already exists: {path}")

        try:
            target.mkdir(parents=True, exist_ok=False)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=f"Permission denied: {path}") from exc
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"Failed to create directory: {exc}") from exc

        return self.workspace_path_payload(target)

    def rename_path(self, path: str, new_path: str) -> WorkspacePathResponse:
        source = self.resolve_workspace_path(path, follow_final_symlink=False)
        destination = self.resolve_workspace_path(new_path, follow_final_symlink=False)

        self.ensure_write_allowed(source)
        self.ensure_write_allowed(destination)

        self.ensure_not_workspace_root(source)
        self.ensure_not_workspace_root(destination)

        if not source.exists() and not source.is_symlink():
            raise HTTPException(status_code=404, detail=f"Path not found: {path}")
        if destination.exists() or destination.is_symlink():
            raise HTTPException(status_code=409, detail=f"Target already exists: {new_path}")

        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            source.rename(destination)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=f"Permission denied: {path}") from exc
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"Failed to rename path: {exc}") from exc

        return self.workspace_path_payload(destination)

    def delete_path(self, path: str, recursive: bool) -> WorkspaceDeleteResponse:
        target = self.resolve_workspace_path(path, follow_final_symlink=False)
        self.ensure_not_workspace_root(target)
        self.ensure_write_allowed(target)

        if not target.exists() and not target.is_symlink():
            raise HTTPException(status_code=404, detail=f"Path not found: {path}")

        relative_path = self.to_workspace_relative(target, follow_symlinks=False)
        is_dir = target.is_dir() and not target.is_symlink()

        try:
            if target.is_symlink():
                target.unlink()
            elif is_dir:
                if recursive:
                    shutil.rmtree(target)
                else:
                    target.rmdir()
            else:
                target.unlink()
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=f"Permission denied: {path}") from exc
        except OSError as exc:
            if is_dir and not recursive:
                raise HTTPException(
                    status_code=409,
                    detail="Directory is not empty. Retry with recursive=true.",
                ) from exc
            raise HTTPException(status_code=500, detail=f"Failed to delete path: {exc}") from exc

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
    def iso_timestamp(epoch_seconds: float) -> str:
        return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).isoformat()

    def directory_has_visible_children(self, path: Path) -> bool:
        if path.is_symlink():
            return False
        try:
            for child in path.iterdir():
                if not self.should_skip_entry(child):
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
