from .api import create_workspace_router
from .context import WorkspaceContext, ProjectMetadata, FileIndexEntry
from .file_state_cache import FileStateCache, get_global_file_cache
from .fuzzy_search import FuzzySearchEngine, get_global_fuzzy_search
from .worktree import WorktreeManager, get_global_worktree_manager

try:
    from .file_watcher import WorkspaceFileWatcher
except ModuleNotFoundError:
    WorkspaceFileWatcher = None  # type: ignore[assignment]

__all__ = [
    "create_workspace_router",
    "WorkspaceContext",
    "ProjectMetadata",
    "FileIndexEntry",
    "FileStateCache",
    "get_global_file_cache",
    "FuzzySearchEngine",
    "get_global_fuzzy_search",
    "WorktreeManager",
    "get_global_worktree_manager",
]

if WorkspaceFileWatcher is not None:
    __all__.append("WorkspaceFileWatcher")
