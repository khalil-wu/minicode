"""Locate the conversations that belong to a workspace navigation entry."""

from pathlib import Path

from backend.atomic_io import canonical_file_path_key
from backend.conversations.repository import ConversationRepository
from backend.workspace.recent_projects import RecentProjectStore


def workspace_project_roots(path: str) -> tuple[str, ...]:
    identity = canonical_file_path_key(path)
    project = next((item for item in RecentProjectStore().list() if canonical_file_path_key(item.path) == identity), None)
    return project.workspace_roots if project is not None else (str(Path(path).resolve()),)


def workspace_conversation_ids(repository: ConversationRepository, path: str) -> list[str]:
    """Use explicit membership, or saved source roots for older records.

    Projectless membership is explicit; execution cwd does not assign a project.
    """
    return repository.project_conversation_ids(path, workspace_project_roots(path))
