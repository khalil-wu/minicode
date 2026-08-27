from __future__ import annotations

from pathlib import Path
import subprocess
from typing import Any

from fastapi import HTTPException

from backend.runtime_env import sanitized_git_env


def search_workspace_directories(root: Path, query: str, limit: int) -> list[dict[str, Any]]:
    query_lower = query.strip().lower()
    if not query_lower:
        return []

    ignored = {
        ".git",
        ".idea",
        ".vscode",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        "dist",
        "build",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
    }
    query_chars = set(query_lower)
    results: list[dict[str, Any]] = []

    for path in root.rglob("*"):
        if not path.is_dir():
            continue
        try:
            rel_parts = path.relative_to(root).parts
        except ValueError:
            continue
        if not rel_parts:
            continue
        if any(part in ignored or part.startswith(".") for part in rel_parts):
            continue

        rel = path.relative_to(root).as_posix()
        rel_lower = rel.lower()
        if query_lower not in rel_lower and not query_chars.issubset(set(rel_lower)):
            continue

        name_lower = path.name.lower()
        score = 10.0
        if name_lower == query_lower:
            score += 100
        if name_lower.startswith(query_lower):
            score += 70
        if query_lower in name_lower:
            score += 50
        if query_lower in rel_lower:
            score += 20
        score -= min(len(rel_parts), 12) * 1.5

        results.append(
            {
                "path": rel,
                "name": path.name,
                "score": score,
                "matched_indices": [],
                "kind": "folder",
            }
        )

    results.sort(key=lambda item: (-item["score"], item["path"]))
    return results[:limit]


def workspace_search_payload(
    *,
    root: Path,
    query: str,
    limit: int,
    include_tests: bool,
    kind: str,
) -> dict[str, Any]:
    from backend.workspace.fuzzy_search import get_global_fuzzy_search

    results: list[dict[str, Any]] = []
    if kind in {"file", "all"}:
        engine = get_global_fuzzy_search(root)
        matches = engine.search(query=query, max_results=limit, include_tests=include_tests)
        results.extend(
            {
                "path": str(match.path.relative_to(root)).replace("\\", "/"),
                "name": match.path.name,
                "score": match.score,
                "matched_indices": match.matched_indices,
                "kind": "file",
            }
            for match in matches
        )
    if kind in {"folder", "all"}:
        results.extend(search_workspace_directories(root, query, limit))
    results.sort(key=lambda item: (-item["score"], item["path"]))
    return {
        "query": query,
        "results": results[:limit],
    }


async def import_project_payload(raw_path: str) -> dict[str, Any]:
    from backend.workspace.context import WorkspaceContext
    from backend.workspace.path_utils import normalize_project_import_path
    from backend.workspace.recent_projects import RecentProjectStore
    from backend.workspace.state import set_active_workspace_root

    normalized_path = normalize_project_import_path(raw_path)
    workspace_ctx = WorkspaceContext(normalized_path)
    metadata = await workspace_ctx.initialize()
    set_active_workspace_root(metadata.root_path)

    store = RecentProjectStore()
    store.add(
        path=str(metadata.root_path),
        name=metadata.name,
        project_type=metadata.project_type,
    )

    return {
        "success": True,
        "project": workspace_ctx.to_dict(),
        "summary": workspace_ctx.get_project_summary(),
        "file_count": metadata.file_count,
    }


def validate_project_path_payload(raw_path: str) -> dict[str, Any]:
    from backend.workspace.path_utils import build_missing_path_hint, normalize_project_import_path

    path = normalize_project_import_path(raw_path)
    if not path.exists():
        payload: dict[str, Any] = {
            "valid": False,
            "error": "路径不存在",
            "normalized_path": str(path),
        }
        hint = build_missing_path_hint(raw_path)
        if hint:
            payload["hint"] = hint
        return payload
    if not path.is_dir():
        return {
            "valid": False,
            "error": "不是目录",
            "normalized_path": str(path),
        }

    project_type = "unknown"
    if (path / "pyproject.toml").exists() or (path / "setup.py").exists():
        project_type = "python"
    elif (path / "package.json").exists():
        project_type = "node"
    elif (path / "Cargo.toml").exists():
        project_type = "rust"
    elif (path / "go.mod").exists():
        project_type = "go"

    return {
        "valid": True,
        "name": path.name,
        "project_type": project_type,
        "has_git": (path / ".git").is_dir(),
        "normalized_path": str(path),
    }


def list_recent_projects_payload(limit: int) -> dict[str, Any]:
    from backend.workspace.recent_projects import RecentProjectStore

    store = RecentProjectStore()
    projects = store.list(limit=limit)
    return {"projects": [project.to_dict() for project in projects]}


def remove_recent_project_payload(path: str) -> dict[str, Any]:
    from backend.workspace.recent_projects import RecentProjectStore

    normalized_path = str(Path(path).resolve())
    store = RecentProjectStore()
    removed = store.remove(normalized_path)
    return {"removed": removed, "path": normalized_path}


def resolve_workspace_git_root(path: str, fallback_root: Path) -> Path:
    fallback_root = fallback_root.resolve()
    candidate = str(path or "").strip()
    if candidate:
        resolved: Path | None = Path(candidate).expanduser()
        try:
            resolved = resolved.resolve()
        except OSError:
            resolved = None
        if resolved is not None and resolved.is_dir():
            if resolved != fallback_root and fallback_root not in resolved.parents:
                raise HTTPException(status_code=400, detail="Git path is outside workspace root.")
            return resolved
    return fallback_root


def workspace_git_status_payload(root: Path) -> dict[str, Any]:
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain=v1", "--branch"],
            cwd=root,
            env=sanitized_git_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5,
        )
        return parse_workspace_git_status(result.stdout)
    except Exception as exc:
        return {"branch": "", "modified": [], "staged": [], "untracked": [], "error": str(exc)}


def parse_workspace_git_status(stdout: str) -> dict[str, Any]:
    branch = ""
    modified: list[str] = []
    staged: list[str] = []
    untracked: list[str] = []
    for line in stdout.splitlines():
        if line.startswith("## "):
            branch = line[3:].split("...")[0].strip()
        elif line.startswith("??"):
            untracked.append(line[3:].strip())
        elif len(line) >= 2:
            xy, file_path = line[:2], line[3:].strip()
            if xy[1] != " ":
                modified.append(file_path)
            if xy[0] != " " and xy[0] != "?":
                staged.append(file_path)
    return {"branch": branch, "modified": modified, "staged": staged, "untracked": untracked}


def workspace_git_diff_payload(root: Path, file: str) -> dict[str, Any]:
    try:
        cmd = ["git", "diff", "HEAD", "--", file] if file else ["git", "diff", "HEAD"]
        result = subprocess.run(
            cmd,
            cwd=root,
            env=sanitized_git_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
        )
        return {"diff": result.stdout}
    except Exception as exc:
        return {"diff": "", "error": str(exc)}


def workspace_git_worktree_payload(root: Path) -> dict[str, Any]:
    try:
        from backend.workspace.worktree import WorktreeManager, isolated_worktree_root, summarize_worktree_status

        manager = WorktreeManager(root)
        worktrees = manager.list_worktrees()
        status = summarize_worktree_status(root, worktrees)
        common_dir = resolve_git_common_dir(root)
        isolated_root = isolated_worktree_root(status.main_repo_path or root)

        entries: list[dict[str, Any]] = []
        for worktree in status.worktrees:
            is_isolated = is_path_within(worktree.path, isolated_root)
            entries.append(
                {
                    "path": str(worktree.path),
                    "branch": worktree.branch,
                    "commit": worktree.commit[:8],
                    "is_main": bool(status.main_repo_path and worktree.path == status.main_repo_path),
                    "is_current": worktree.path == status.current_path,
                    "is_detached": worktree.is_detached,
                    "is_isolated": is_isolated,
                    "can_remove": is_isolated and worktree.path != status.current_path,
                }
            )

        return {
            "is_worktree": status.is_worktree,
            "current_path": str(status.current_path),
            "main_repo_path": str(status.main_repo_path) if status.main_repo_path else None,
            "current_branch": status.current_branch,
            "common_git_dir": str(common_dir) if common_dir else None,
            "worktree_count": status.worktree_count,
            "worktrees": entries,
        }
    except Exception as exc:
        return {"current_path": str(root), "error": str(exc)}


def resolve_git_common_dir(root: Path) -> Path | None:
    result = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=root,
        env=sanitized_git_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=5,
        check=True,
    )
    common_dir_raw = result.stdout.strip()
    if not common_dir_raw:
        return None
    common_dir_path = Path(common_dir_raw)
    if common_dir_path.is_absolute():
        return common_dir_path.resolve()
    return (root / common_dir_path).resolve()


async def switch_workspace_git_worktree_payload(current_root: Path, target_path: str) -> dict[str, Any]:
    from backend.workspace.context import WorkspaceContext
    from backend.workspace.recent_projects import RecentProjectStore
    from backend.workspace.state import set_active_workspace_root
    from backend.workspace.worktree import WorktreeManager

    target = Path(target_path).resolve()
    if not target.exists() or not target.is_dir():
        return _project_import_failure()

    try:
        manager = WorktreeManager(current_root)
        allowed_paths = {item.path.resolve() for item in manager.list_worktrees()}
        if target not in allowed_paths:
            return _project_import_failure()

        workspace_ctx = WorkspaceContext(target)
        metadata = await workspace_ctx.initialize()
        set_active_workspace_root(metadata.root_path)

        store = RecentProjectStore()
        store.add(
            path=str(metadata.root_path),
            name=metadata.name,
            project_type=metadata.project_type,
        )

        return {
            "success": True,
            "project": workspace_ctx.to_dict(),
            "summary": workspace_ctx.get_project_summary(),
            "file_count": metadata.file_count,
        }
    except Exception:
        return _project_import_failure()


def _project_import_failure() -> dict[str, Any]:
    return {"success": False, "project": {}, "summary": "", "file_count": 0}


def remove_workspace_git_worktree_payload(*, root: Path, path: str, force: bool) -> dict[str, Any]:
    from backend.workspace.worktree import WorktreeManager, isolated_worktree_root, summarize_worktree_status

    target = Path(path).resolve()
    try:
        manager = WorktreeManager(root)
        worktrees = manager.list_worktrees()
        status = summarize_worktree_status(root, worktrees)
        isolated_root = isolated_worktree_root(status.main_repo_path or root)
        target_entry = next((item for item in worktrees if item.path.resolve() == target), None)
        if target_entry is None:
            return {"removed": False, "path": str(target), "error": "Worktree not found"}
        if target == status.current_path:
            return {"removed": False, "path": str(target), "error": "Cannot remove current worktree"}
        if not is_path_within(target, isolated_root):
            return {
                "removed": False,
                "path": str(target),
                "branch": target_entry.branch,
                "error": "Only isolated worktrees under .minicode/worktrees can be removed",
            }

        removal = manager.safe_remove_worktree(target, force=force)
        if removal.needs_force:
            return {
                "removed": False,
                "path": str(target),
                "branch": target_entry.branch,
                "error": removal.error or "Worktree has local changes; force is required",
            }
        return {
            "removed": removal.removed,
            "path": str(target),
            "branch": target_entry.branch,
            "snapshot_id": removal.snapshot.id if removal.snapshot else None,
            "snapshot_ref": removal.snapshot.snapshot_ref if removal.snapshot else None,
            "error": None if removal.removed else (removal.error or "git worktree remove failed"),
        }
    except Exception as exc:
        return {"removed": False, "path": str(target), "error": str(exc)}


def workspace_git_worktree_snapshots_payload(root: Path, conversation_id: str | None) -> dict[str, Any]:
    try:
        from backend.workspace.worktree import WorktreeManager

        manager = WorktreeManager(root)
        records = manager.list_snapshots(conversation_id)
        return {
            "snapshots": [
                {
                    "id": record.id,
                    "conversation_id": record.conversation_id,
                    "branch": record.branch,
                    "original_path": record.original_path,
                    "snapshot_sha": record.snapshot_sha,
                    "snapshot_ref": record.snapshot_ref,
                    "label": record.label,
                    "created_at": record.created_at,
                }
                for record in records
            ]
        }
    except Exception as exc:
        return {"error": str(exc)}


def restore_workspace_git_worktree_snapshot_payload(
    *,
    root: Path,
    snapshot_id: str,
    dest: str | None,
) -> dict[str, Any]:
    try:
        from backend.workspace.worktree import WorktreeManager

        manager = WorktreeManager(root)
        result = manager.restore_snapshot(snapshot_id, dest=Path(dest) if dest else None)
        return {
            "restored": result.restored,
            "snapshot_id": snapshot_id,
            "path": str(result.path) if result.path else "",
            "branch": result.snapshot.branch if result.snapshot else "",
            "error": result.error,
        }
    except Exception as exc:
        return {"restored": False, "snapshot_id": snapshot_id, "error": str(exc)}


def is_path_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False
