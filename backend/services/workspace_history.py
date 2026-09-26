"""Locate the conversations that belong to a workspace navigation entry."""

from pathlib import Path

from backend.atomic_io import canonical_file_path_key
from backend.conversations.repository import ConversationRepository
from backend.services.workspace_service import conversation_workspace_path


def workspace_conversation_ids(repository: ConversationRepository, path: str) -> list[str]:
    """Return the ids of conversations bound to ``path``.

    Removing a recent-workspace entry only edits the navigation list. The
    conversations themselves stay in the app's own store keyed by workspace, so
    reopening the project restores them without copying any transcript (which
    can contain tool output with secrets) into the project directory.
    """
    identity = canonical_file_path_key(Path(path).resolve())
    return [item.id for item in repository.list_conversations()
            if conversation_workspace_path(item)
            and canonical_file_path_key(conversation_workspace_path(item)) == identity]
