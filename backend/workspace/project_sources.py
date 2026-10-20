"""Resolve selected project source folders in the current local executor domain."""
from pathlib import Path
from backend.atomic_io import canonical_file_path_key
from backend.workspace.recent_projects import RecentProjectStore
from backend.workspace.trust import is_workspace_trusted


def local_project_source_selection(workspace_root: Path | str) -> tuple[str | None, tuple[Path, ...]]:
    primary = Path(workspace_root).expanduser().resolve()
    project = RecentProjectStore().project_for_root(str(primary))
    if project is None or not project.named: return None, (primary,)
    folders = project.workspace_roots
    # Registration comes from explicit desktop folder choices. Revoked or
    # unavailable folders cannot silently remain executor grants.
    accepted = [primary]
    for folder in folders:
        path = Path(folder).expanduser().resolve()
        if canonical_file_path_key(path) != canonical_file_path_key(primary) and is_workspace_trusted(path): accepted.append(path)
    return project.path, tuple(accepted)


def local_project_source_roots(workspace_root: Path | str) -> tuple[Path, ...]:
    return local_project_source_selection(workspace_root)[1]
