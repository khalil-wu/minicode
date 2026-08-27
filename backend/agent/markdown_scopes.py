from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Literal
import unicodedata


MarkdownSettingSource = Literal[
    "policy",
    "user",
    "project",
]


@dataclass(frozen=True)
class MarkdownDirectory:
    path: Path
    source: MarkdownSettingSource


def _absolute(path: str | Path) -> Path:
    return Path(os.path.abspath(Path(path).expanduser()))


def _path_key(path: str | Path) -> str:
    normalized = unicodedata.normalize("NFC", os.path.normpath(str(path)))
    return os.path.normcase(normalized)


def get_minicode_config_home_dir() -> Path:
    configured = os.environ.get("MINICODE_CONFIG_DIR")
    return _absolute(configured if configured is not None else Path.home() / ".minicode")


def find_git_root(start: str | Path) -> Path | None:
    """Return the nearest directory containing a Git dir or worktree file."""

    current = _absolute(start)
    while True:
        if (current / ".git").exists():
            return current
        parent = current.parent
        if parent == current:
            return None
        current = parent


def find_canonical_git_root(start: str | Path) -> Path | None:
    """Resolve a normal Git worktree to its main repository working tree."""

    git_root = find_git_root(start)
    if git_root is None:
        return None
    dot_git = git_root / ".git"
    if not dot_git.is_file():
        return git_root
    try:
        marker = dot_git.read_text(encoding="utf-8").strip()
        if not marker.startswith("gitdir:"):
            return git_root
        worktree_git_dir = _absolute(git_root / marker[len("gitdir:") :].strip())
        common_dir = _absolute(
            worktree_git_dir
            / (worktree_git_dir / "commondir").read_text(encoding="utf-8").strip()
        )

        # Match the structure created by `git worktree add`; both files are
        # repository-controlled and must agree before the main tree is trusted.
        if worktree_git_dir.parent.resolve() != (common_dir / "worktrees").resolve():
            return git_root
        backlink_text = (worktree_git_dir / "gitdir").read_text(
            encoding="utf-8"
        ).strip()
        backlink = Path(backlink_text)
        if not backlink.is_absolute():
            backlink = worktree_git_dir / backlink
        if backlink.resolve() != dot_git.resolve():
            return git_root
        if common_dir.name != ".git":
            return common_dir
        return common_dir.parent
    except (OSError, UnicodeError):
        # A submodule also has a .git file but no commondir. It is its own repo.
        return git_root


def resolve_stop_boundary(
    cwd: str | Path,
    *,
    session_project_root: str | Path | None = None,
) -> Path | None:
    """Apply MiniCode's nested-repository boundary widening rule."""

    cwd_git_root = find_git_root(cwd)
    session_git_root = (
        find_git_root(session_project_root)
        if session_project_root is not None
        else cwd_git_root
    )
    if cwd_git_root is None or session_git_root is None:
        return cwd_git_root

    cwd_canonical = find_canonical_git_root(cwd)
    if cwd_canonical is not None and _path_key(cwd_canonical) == _path_key(
        session_git_root
    ):
        return cwd_git_root

    cwd_key = _path_key(cwd_git_root)
    session_key = _path_key(session_git_root)
    separator = os.sep
    if cwd_key != session_key and cwd_key.startswith(session_key + separator):
        return session_git_root
    return cwd_git_root


def get_project_dirs_up_to_home(
    subdir: str,
    cwd: str | Path,
    *,
    session_project_root: str | Path | None = None,
) -> list[Path]:
    """Return existing `.minicode/<subdir>` dirs toward the project boundary."""

    home = _absolute(Path.home())
    boundary = resolve_stop_boundary(
        cwd,
        session_project_root=session_project_root,
    )
    current = _absolute(cwd)
    directories: list[Path] = []
    while True:
        if _path_key(current) == _path_key(home):
            break
        directory = current / ".minicode" / subdir
        if directory.is_dir():
            directories.append(directory)
        if boundary is not None and _path_key(current) == _path_key(boundary):
            break
        parent = current.parent
        if parent == current:
            break
        current = parent
    return directories


def get_markdown_directories(
    subdir: str,
    cwd: str | Path | None,
    *,
    managed_root: str | Path,
    session_project_root: str | Path | None = None,
) -> list[MarkdownDirectory]:
    """Return MiniCode markdown scopes in managed, user, project order."""

    project_dirs: list[Path] = []
    if cwd is not None:
        project_dirs = get_project_dirs_up_to_home(
            subdir,
            cwd,
            session_project_root=session_project_root,
        )
        git_root = find_git_root(cwd)
        canonical_root = find_canonical_git_root(cwd)
        if (
            git_root is not None
            and canonical_root is not None
            and _path_key(git_root) != _path_key(canonical_root)
        ):
            worktree_subdir = git_root / ".minicode" / subdir
            if not any(
                _path_key(path) == _path_key(worktree_subdir)
                for path in project_dirs
            ):
                main_subdir = canonical_root / ".minicode" / subdir
                if not any(
                    _path_key(path) == _path_key(main_subdir)
                    for path in project_dirs
                ):
                    project_dirs.append(main_subdir)

    managed = _absolute(managed_root) / subdir
    user = get_minicode_config_home_dir() / subdir
    return [
        MarkdownDirectory(managed, "policy"),
        MarkdownDirectory(user, "user"),
        *(
            MarkdownDirectory(path, "project")
            for path in project_dirs
        ),
    ]


def file_identity(path: Path) -> tuple[int, int] | None:
    """Return a filesystem device/inode identity, or None when unavailable."""

    try:
        stat = path.lstat()
    except OSError:
        return None
    identity = (int(stat.st_dev), int(stat.st_ino))
    return None if identity == (0, 0) else identity
