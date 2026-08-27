from __future__ import annotations

import fnmatch
import logging
from pathlib import Path
from typing import Literal

from backend.security.sensitive_files import DANGEROUS_DIRECTORIES, DANGEROUS_FILES

logger = logging.getLogger(__name__)


class PermissionRuleMatcher:
    """Deterministic filesystem permission matcher."""

    def __init__(
        self,
        workspace_root: Path | None = None,
        allowed_paths: list[str] | None = None,
        denied_paths: list[str] | None = None,
    ) -> None:
        self.workspace_root = (workspace_root or Path.cwd()).resolve()
        self.allowed_paths = allowed_paths or []
        self.denied_paths = denied_paths or []

    def check_file_access(
        self,
        file_path: str | Path,
        operation: Literal["read", "write", "execute"],
    ) -> tuple[bool, str]:
        raw_path = str(file_path)
        if self._contains_path_traversal(raw_path):
            return False, "Path contains traversal markers"

        path = Path(file_path).resolve()
        if not self._is_within_workspace(path):
            return (
                False,
                f"Path is outside workspace: {path} (workspace: {self.workspace_root})",
            )

        if operation == "write" and self._is_dangerous_file(path):
            return False, f"Sensitive file cannot be edited automatically: {path.name}"

        if operation == "write" and self._is_in_dangerous_directory(path):
            return False, f"File is inside protected directory: {self._get_dangerous_dir(path)}"

        if self._matches_denied_paths(path):
            return False, "Path matches denylist rule"

        if self.allowed_paths and not self._matches_allowed_paths(path):
            return False, "Path is outside allowlist"

        return True, ""

    def _contains_path_traversal(self, path: str) -> bool:
        traversal_markers = [
            "../",
            "..\\",
            "/..",
            "\\..",
            "%2e%2e",
            "%2E%2E",
            "%2e%2E",
            "%2E%2e",
            "..%2f",
            "..%5c",
            "%2f..",
            "%5c..",
        ]
        return any(marker in path for marker in traversal_markers) or path.strip() == ".."

    def _is_within_workspace(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.workspace_root.resolve())
            return True
        except ValueError:
            return False

    def _is_dangerous_file(self, path: Path) -> bool:
        return path.name.lower() in {name.lower() for name in DANGEROUS_FILES}

    def _is_in_dangerous_directory(self, path: Path) -> bool:
        dangerous_dirs = {name.lower() for name in DANGEROUS_DIRECTORIES}
        return any(part.lower() in dangerous_dirs for part in path.parts)

    def _get_dangerous_dir(self, path: Path) -> str:
        dangerous_dirs = {name.lower() for name in DANGEROUS_DIRECTORIES}
        for part in path.parts:
            if part.lower() in dangerous_dirs:
                return part
        return ""

    def _relative_posix(self, path: Path) -> str | None:
        """Workspace-relative posix path, or None if it cannot be classified.

        Malformed inputs (e.g. Windows ``\\\\?\\`` device paths that resolve to
        drive-relative forms) make ``relative_to`` raise ValueError; returning
        None lets callers fail closed (deny) instead of crashing the run.
        """
        try:
            return path.relative_to(self.workspace_root).as_posix()
        except ValueError:
            return None

    def _matches_denied_paths(self, path: Path) -> bool:
        rel_path = self._relative_posix(path)
        if rel_path is None:
            return True  # unclassifiable -> deny
        return any(fnmatch.fnmatch(rel_path, pattern.replace("\\", "/")) for pattern in self.denied_paths)

    def _matches_allowed_paths(self, path: Path) -> bool:
        rel_path = self._relative_posix(path)
        if rel_path is None:
            return False  # unclassifiable -> not allowed
        return any(fnmatch.fnmatch(rel_path, pattern.replace("\\", "/")) for pattern in self.allowed_paths)


class SandboxValidator:
    """Workspace boundary validator for filesystem operations."""

    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root.resolve()
        self.matcher = PermissionRuleMatcher(workspace_root)

    def validate_file_operation(
        self,
        file_path: str | Path,
        operation: Literal["read", "write", "execute"],
        content: str | None = None,
    ) -> tuple[bool, str]:
        allowed, reason = self.matcher.check_file_access(file_path, operation)
        if not allowed:
            return False, reason

        return True, ""


def create_default_sandbox(workspace_root: Path | None = None) -> SandboxValidator:
    return SandboxValidator(workspace_root or Path.cwd())
